# -*- coding: utf-8 -*-
"""
engine.py
Центральный "движок" приложения — связывает воедино детектор сканера, захват
экрана, цветовые триггеры, OCR и печать.

Используется и из фонового потока сканера (автоматическая печать по скану),
и из веб-UI (тестовая печать, тестовый скан, проверка области, калибровка,
повтор печати, пауза) — поэтому вся логика централизована здесь.
"""

import base64
import io
import logging
import threading
import time
from typing import Callable, Optional

from PIL import Image, ImageDraw

import color_trigger
import config as config_module
import digit_templates
import health_check
import ocr as ocr_module
import print_log
import printer_tspl
import screen_capture
import sound
from scanner_listener import ScanEvent, ScannerListener

try:
    from raw_input_listener import DeviceScannerListener
except ImportError:  # pywin32 недоступен (например, разработка не на Windows)
    DeviceScannerListener = None

log = logging.getLogger("engine")


class Engine:
    def __init__(self):
        self.cfg = config_module.load_config()
        self._cfg_lock = threading.Lock()

        self.status = "ready"  # ready | busy | error | paused
        self.status_text = "Готов"
        self.last_cell_number: Optional[str] = None
        self.last_scan_time: Optional[float] = None
        self.last_error: Optional[str] = None
        self.last_confidence: Optional[float] = None
        self.last_preview_png: Optional[bytes] = None  # для "Проверить область" в UI
        # --- режим печати "screenshot" (см. cfg["capture"]["print_mode"]) ---
        # last_print_mode + _last_screenshot_crop нужны отдельно от last_cell_number,
        # т.к. в этом режиме нет распознанной строки цифр, которую можно было бы
        # заново подставить в шаблон при повторе печати (см. repeat_last_print) —
        # вместо этого сохраняем саму обрезанную картинку.
        self.last_print_mode: Optional[str] = None
        self._last_screenshot_crop: Optional[Image.Image] = None

        self._status_listeners: "list[Callable[[str, str], None]]" = []
        self.on_hotkeys_changed: "Optional[Callable[[dict], None]]" = None

        self.scanner = ScannerListener(cfg_provider=self._get_scanner_cfg, on_scan=self._handle_scan_event)
        # Слушатель, различающий физические USB-HID устройства через Windows Raw
        # Input API (см. raw_input_listener.py) — активен ТОЛЬКО когда в настройках
        # привязано конкретное устройство (scanner_detection.bound_device_path),
        # см. _sync_scanner_mode(). До привязки работает как раньше — self.scanner
        # по эвристике времени между символами.
        self.device_scanner = (
            DeviceScannerListener(cfg_provider=self._get_scanner_cfg, on_scan=self._handle_scan_event)
            if DeviceScannerListener is not None else None
        )
        self._user_paused = bool(self.cfg["app"].get("start_paused"))
        self._sync_scanner_mode()
        if self._user_paused:
            self._set_status("paused", "Пауза")

    # ---------- конфигурация (потокобезопасный доступ, т.к. читается из
    # потока сканера, потока хоткеев и из потока Flask одновременно) ----------

    def _get_scanner_cfg(self) -> dict:
        with self._cfg_lock:
            return dict(self.cfg["scanner_detection"])

    @property
    def active_scanner(self):
        """Слушатель, который сейчас реально слушает сканы: DeviceScannerListener
        (Raw Input, фильтр по конкретному устройству), если оно привязано в
        настройках, иначе обычный ScannerListener (эвристика по времени между
        символами) — как было раньше. См. _sync_scanner_mode()."""
        if self.device_scanner is not None and self.cfg["scanner_detection"].get("bound_device_path"):
            return self.device_scanner
        return self.scanner

    def _sync_scanner_mode(self):
        """Держит РОВНО ОДИН из двух слушателей активным — если бы оба слушали
        одновременно, один физический скан мог бы напечататься дважды (сначала
        сработает эвристика по времени, потом — фильтр по устройству, или
        наоборот). Неактивный слушатель всегда на паузе, независимо от
        self._user_paused; активный — на паузе, только если пользователь сам
        поставил на паузу (кнопка/трей/хоткей)."""
        bound = self.cfg["scanner_detection"].get("bound_device_path")
        if self.device_scanner is not None:
            self.device_scanner.set_bound_device(bound)
        active = self.device_scanner if (bound and self.device_scanner is not None) else self.scanner
        inactive = self.scanner if active is self.device_scanner else self.device_scanner
        if inactive is not None:
            inactive.pause()
        if self._user_paused:
            active.pause()
        else:
            active.resume()

    def reload_config(self):
        with self._cfg_lock:
            self.cfg = config_module.load_config()
        self._sync_scanner_mode()

    def update_config(self, patch: dict) -> dict:
        with self._cfg_lock:
            self.cfg = config_module.update_config(patch)
            cfg_copy = self.cfg
        self._sync_scanner_mode()
        if "app" in patch and self.on_hotkeys_changed:
            try:
                self.on_hotkeys_changed(cfg_copy)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка при перерегистрации горячих клавиш")
        return cfg_copy

    def get_config(self) -> dict:
        with self._cfg_lock:
            return self.cfg

    # ---------- статус (для трея и для GET /api/status) ----------

    def add_status_listener(self, fn: Callable[[str, str], None]):
        self._status_listeners.append(fn)

    def _set_status(self, status: str, text: str):
        self.status = status
        self.status_text = text
        for fn in self._status_listeners:
            try:
                fn(status, text)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в обработчике статуса")

    # ---------- основной пайплайн: скан -> задержка -> [цвет] -> захват -> OCR -> печать ----------

    def _handle_scan_event(self, event: ScanEvent):
        cfg = self.get_config()
        self._set_status("busy", "Распознавание...")
        sound.play_capture(cfg["sounds"])

        delay_s = cfg["scanner_detection"]["post_scan_delay_ms"] / 1000.0
        time.sleep(delay_s)

        state = screen_capture.get_window_state(
            cfg["capture"]["window_title"], exact=cfg["capture"].get("exact_title_match", False)
        )
        if not state.found:
            self._fail("Окно «Мой ПВЗ» не найдено", play_error_sound=False, cfg=cfg)
            return
        if not state.visible:
            self._fail("Окно «Мой ПВЗ» свёрнуто или неактивно", play_error_sound=False, cfg=cfg)
            return

        # --- доп. функция: печать только если в заданных точках нужный цвет ---
        trigger_result = color_trigger.evaluate_triggers(cfg["capture"], state)
        if not trigger_result["passed"]:
            log.info("Печать отменена: цветовой триггер не совпал (%s)", trigger_result["details"])
            if cfg["capture"].get("color_trigger_log_skips", True):
                print_log.add_entry(None, success=None, note="Пропущено — не совпал цветовой триггер")
            # ТИХАЯ отмена по требованию: без звука ошибки, без статуса "ошибка"
            self._set_status("paused" if self._user_paused else "ready",
                              "Пауза" if self._user_paused else "Готов")
            return

        def capture_fn():
            if cfg["capture"].get("ocr_stability_check_enabled", True):
                img, st = screen_capture.capture_calibrated_region_stable(
                    cfg["capture"], max_wait_ms=cfg["capture"].get("ocr_stability_max_wait_ms", 150)
                )
            else:
                img, st = screen_capture.capture_calibrated_region(cfg["capture"])
            return img

        if cfg["capture"].get("print_mode", "ocr") == "screenshot":
            self._handle_scan_event_screenshot(capture_fn, cfg)
            return

        cell_number, last_img, confidence = ocr_module.recognize_with_retries(
            capture_fn,
            cfg["capture"],
            retry_count=cfg["recognition"]["retry_count"],
            retry_delay_ms=cfg["recognition"]["retry_delay_ms"],
        )
        if last_img is not None:
            self._store_preview(last_img)
        self.last_confidence = confidence

        if not cell_number:
            self._fail("Не удалось распознать номер ячейки", play_error_sound=True, cfg=cfg)
            print_log.add_entry(None, success=False, note="OCR не распознал номер")
            return

        ok, err = printer_tspl.print_cell_number(cfg, cell_number)
        if not ok:
            self._fail(f"Ошибка печати: {err}", play_error_sound=True, cfg=cfg)
            print_log.add_entry(cell_number, success=False, note=err)
            return

        self.last_print_mode = "ocr"
        self.last_cell_number = cell_number
        self.last_scan_time = time.time()
        self._decrement_roll()
        print_log.add_entry(cell_number, success=True, note=f"уверенность OCR: {confidence:.0f}%")
        sound.play_success(cfg["sounds"])
        self._set_status("ready", "Готов")

    def _handle_scan_event_screenshot(self, capture_fn, cfg: dict):
        """Ветка пайплайна для cfg["capture"]["print_mode"] == "screenshot": без OCR —
        берём захваченную область как есть, обрезаем по фактическому содержимому
        (чтобы в печать не лез мусор по краям области калибровки) и печатаем этот
        кусок скриншота КАК КАРТИНКУ. См. printer_tspl.autocrop_number_screenshot /
        build_label_from_screenshot."""
        img = capture_fn()
        if img is None:
            self._fail("Не удалось захватить область — проверьте калибровку", play_error_sound=True, cfg=cfg)
            print_log.add_entry(None, success=False, note="Область не задана/недоступна")
            return

        padding = cfg["capture"].get("screenshot_autocrop_padding", 0.15)
        cropped = printer_tspl.autocrop_number_screenshot(img, padding_ratio=padding)
        if cropped is None:
            self._fail("Область пуста — нечего печатать (проверьте калибровку)", play_error_sound=True, cfg=cfg)
            print_log.add_entry(None, success=False, note="Скриншот пуст — содержимое не найдено")
            return

        if cfg["capture"].get("screenshot_bold"):
            cropped = printer_tspl.boldify_image(cropped, strength=cfg["capture"].get("screenshot_bold_strength", 1))

        self._store_preview(cropped)

        ok, err = printer_tspl.print_number_screenshot(cfg, cropped)
        if not ok:
            self._fail(f"Ошибка печати: {err}", play_error_sound=True, cfg=cfg)
            print_log.add_entry(None, success=False, note=err)
            return

        self.last_print_mode = "screenshot"
        self._last_screenshot_crop = cropped
        # цифрового номера в этом режиме нет — но last_cell_number используется в
        # дашборде/логах как человекочитаемая метка последней печати
        self.last_cell_number = "(скриншот, без OCR)"
        self.last_confidence = None
        self.last_scan_time = time.time()
        self._decrement_roll()
        print_log.add_entry(None, success=True, note="Печать со скриншота (без OCR)")
        sound.play_success(cfg["sounds"])
        self._set_status("ready", "Готов")

    def _fail(self, message: str, play_error_sound: bool, cfg: dict):
        log.warning(message)
        self.last_error = message
        if play_error_sound:
            sound.play_error(cfg["sounds"])
        self._set_status("error", message)

        # автоматически вернуться в "готов" через несколько секунд, чтобы трей
        # не застревал в красном статусе навсегда после разовой проблемы
        def _reset_later():
            time.sleep(5)
            if self.status == "error" and self.last_error == message:
                self._set_status("paused" if self._user_paused else "ready",
                                  "Пауза" if self._user_paused else "Готов")

        threading.Thread(target=_reset_later, daemon=True).start()

    def _decrement_roll(self):
        with self._cfg_lock:
            remaining = self.cfg["printer"].get("roll_remaining_labels", 0)
            self.cfg["printer"]["roll_remaining_labels"] = max(0, remaining - 1)
            config_module.save_config(self.cfg)

    def _store_preview(self, img: Image.Image):
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        self.last_preview_png = buf.getvalue()

    # ---------- действия из UI / трея / хоткеев ----------

    def toggle_pause(self) -> bool:
        self._user_paused = not self._user_paused
        self._sync_scanner_mode()
        self._set_status("paused" if self._user_paused else "ready",
                          "Пауза" if self._user_paused else "Готов")
        return self._user_paused

    def repeat_last_print(self) -> "tuple[bool, str]":
        cfg = self.get_config()
        if self.last_print_mode == "screenshot" and self._last_screenshot_crop is not None:
            ok, err = printer_tspl.print_number_screenshot(cfg, self._last_screenshot_crop)
            print_log.add_entry(None, success=ok, note="повтор (скриншот)" + (f": {err}" if err else ""))
        elif self.last_cell_number and self.last_print_mode == "ocr":
            ok, err = printer_tspl.print_cell_number(cfg, self.last_cell_number)
            print_log.add_entry(self.last_cell_number, success=ok, note="повтор" + (f": {err}" if err else ""))
        else:
            return False, "Нет предыдущего успешного результата для повтора"
        if ok:
            sound.play_success(cfg["sounds"])
        else:
            sound.play_error(cfg["sounds"])
        return ok, err

    def test_print(self, cell_number: str) -> "tuple[bool, str]":
        cfg = self.get_config()
        ok, err = printer_tspl.print_cell_number(cfg, cell_number)
        print_log.add_entry(cell_number, success=ok, note="тестовая печать" + (f": {err}" if err else ""))
        return ok, err

    def test_scan(self) -> dict:
        """Кнопка «Тестовый скан» в UI — прогоняет ВЕСЬ пайплайн (задержка, цветовой
        триггер, захват, OCR, печать) так, будто сработал физический сканер, но
        без проверки debounce/длины кода — удобно для проверки настроек целиком."""
        if self.status == "busy":
            return {"ok": False, "error": "Уже идёт распознавание, подождите"}
        fake_event = ScanEvent(code="TEST-SCAN", timestamp=time.time(), is_duplicate=False)
        threading.Thread(target=self._handle_scan_event, args=(fake_event,), daemon=True).start()
        return {"ok": True}

    def test_region(self) -> dict:
        """Захват + OCR без печати, для кнопки «Проверить область»."""
        cfg = self.get_config()
        if cfg["capture"].get("ocr_stability_check_enabled", True):
            img, state = screen_capture.capture_calibrated_region_stable(
                cfg["capture"], max_wait_ms=cfg["capture"].get("ocr_stability_max_wait_ms", 150)
            )
        else:
            img, state = screen_capture.capture_calibrated_region(cfg["capture"])
        if not state.found:
            return {"ok": False, "error": "Окно «Мой ПВЗ» не найдено"}
        if not state.visible:
            return {"ok": False, "error": "Окно свёрнуто или неактивно"}
        if img is None:
            return {"ok": False, "error": "Область не задана — выполните калибровку"}

        self._store_preview(img)
        result, confidence, processed = ocr_module.recognize_digits(
            img,
            digits_only=cfg["capture"].get("ocr_digits_only", True),
            min_digits=cfg["capture"].get("min_digits", 1),
            max_digits=cfg["capture"].get("max_digits", 4),
            scale=cfg["capture"].get("ocr_upscale_factor", 3),
            threshold_mode=cfg["capture"].get("ocr_threshold_mode", "auto"),
            threshold_value=cfg["capture"].get("ocr_threshold_value", 150),
            invert_mode=cfg["capture"].get("ocr_invert_mode", "auto"),
            autocrop=cfg["capture"].get("ocr_autocrop", True),
        )
        if result is not None and not ocr_module.in_plausible_range(result, cfg["capture"]):
            result = None  # прошло по формату, но вне настроенного разумного диапазона номеров

        color_result = color_trigger.evaluate_triggers(cfg["capture"], state)

        # Отдельно от OCR — показываем, что ИМЕННО уйдёт на печать в режиме
        # "screenshot" (обрезка по содержимому +, если включено, утолщение текста,
        # см. boldify_image) — чтобы можно было проверить настройку заранее, не
        # переключая реальный режим печати и не делая физический скан.
        screenshot_preview_base64 = None
        padding = cfg["capture"].get("screenshot_autocrop_padding", 0.15)
        screenshot_crop = printer_tspl.autocrop_number_screenshot(img, padding_ratio=padding)
        if screenshot_crop is not None:
            if cfg["capture"].get("screenshot_bold"):
                screenshot_crop = printer_tspl.boldify_image(
                    screenshot_crop, strength=cfg["capture"].get("screenshot_bold_strength", 1)
                )
            buf_shot = io.BytesIO()
            screenshot_crop.convert("RGB").save(buf_shot, format="PNG")
            screenshot_preview_base64 = base64.b64encode(buf_shot.getvalue()).decode("ascii")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf_processed = io.BytesIO()
        processed.convert("RGB").save(buf_processed, format="PNG")
        return {
            "ok": True,
            "recognized": result,
            "confidence": round(confidence, 1),
            "min_confidence": cfg["capture"].get("ocr_min_confidence", 40),
            "image_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "processed_image_base64": base64.b64encode(buf_processed.getvalue()).decode("ascii"),
            "screenshot_preview_base64": screenshot_preview_base64,
            "color_triggers": color_result,
        }

    def calibrate_start(self, seconds: int = 4) -> dict:
        cfg = self.get_config()
        img, state = screen_capture.countdown_then_capture_window(
            cfg["capture"]["window_title"], seconds=seconds, exact=cfg["capture"].get("exact_title_match", False)
        )
        if not state.found:
            return {"ok": False, "error": "Окно «Мой ПВЗ» не найдено — откройте его и повторите"}
        if not state.visible or img is None:
            return {"ok": False, "error": "Окно свёрнуто — разверните его и повторите"}

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {
            "ok": True,
            "image_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "window_width": state.client_width,
            "window_height": state.client_height,
        }

    def calibrate_save(self, x: int, y: int, width: int, height: int) -> dict:
        cfg = self.update_config(
            {"capture": {"region_relative": {"x": x, "y": y, "width": width, "height": height}}}
        )
        return {"ok": True, "region": cfg["capture"]["region_relative"]}

    # ---------- цветовые точки-триггеры (CRUD из того же шага калибровки) ----------

    def color_trigger_add(self, x: int, y: int, color_rgb: "list[int]", tolerance_percent: float = 12) -> dict:
        import uuid
        cfg = self.get_config()
        triggers = list(cfg["capture"].get("color_triggers", []))
        new_trigger = {
            "id": uuid.uuid4().hex[:8],
            "enabled": True,
            "x": x, "y": y,
            "color_rgb": color_rgb,
            "tolerance_percent": tolerance_percent,
        }
        triggers.append(new_trigger)
        cfg = self.update_config({"capture": {"color_triggers": triggers}})
        return {"ok": True, "color_triggers": cfg["capture"]["color_triggers"]}

    def color_trigger_update(self, trigger_id: str, patch: dict) -> dict:
        cfg = self.get_config()
        triggers = list(cfg["capture"].get("color_triggers", []))
        for t in triggers:
            if t["id"] == trigger_id:
                t.update(patch)
        cfg = self.update_config({"capture": {"color_triggers": triggers}})
        return {"ok": True, "color_triggers": cfg["capture"]["color_triggers"]}

    def color_trigger_delete(self, trigger_id: str) -> dict:
        cfg = self.get_config()
        triggers = [t for t in cfg["capture"].get("color_triggers", []) if t["id"] != trigger_id]
        cfg = self.update_config({"capture": {"color_triggers": triggers}})
        return {"ok": True, "color_triggers": cfg["capture"]["color_triggers"]}

    def preview_regions(self) -> dict:
        """Для дашборда: свежий скриншот окна «Мой ПВЗ» с наложенными рамкой области
        номера (красная) и метками цветовых точек-триггеров (кружки их целевого цвета)."""
        cfg = self.get_config()
        img, state = screen_capture.capture_full_window(
            cfg["capture"]["window_title"], exact=cfg["capture"].get("exact_title_match", False)
        )
        if not state.found:
            return {"ok": False, "error": "Окно «Мой ПВЗ» не найдено"}
        if not state.visible or img is None:
            return {"ok": False, "error": "Окно свёрнуто или неактивно"}

        annotated = img.convert("RGB").copy()
        draw = ImageDraw.Draw(annotated)
        rel = cfg["capture"]["region_relative"]
        if rel.get("width", 0) > 0 and rel.get("height", 0) > 0:
            draw.rectangle(
                [rel["x"], rel["y"], rel["x"] + rel["width"], rel["y"] + rel["height"]],
                outline=(220, 20, 60), width=3,
            )
        for t in cfg["capture"].get("color_triggers", []):
            x, y = t["x"], t["y"]
            color = tuple(t.get("color_rgb", [0, 120, 255]))
            r = 8
            draw.ellipse([x - r, y - r, x + r, y + r], outline=(0, 0, 0), width=2)
            draw.ellipse([x - r + 2, y - r + 2, x + r - 2, y + r - 2], fill=color)

        buf = io.BytesIO()
        annotated.save(buf, format="PNG")
        return {"ok": True, "image_base64": base64.b64encode(buf.getvalue()).decode("ascii")}

    def render_template_preview(self, elements: dict, cell_number: str) -> dict:
        """WYSIWYG-превью этикетки для редактора макета — рендерит ЖИВОЕ (возможно ещё
        не сохранённое) состояние elements теми же функциями, что и реальная печать,
        с ПРОИЗВОЛЬНЫМ тестовым номером — удобно проверять автоцентровку/автомасштаб
        на разном количестве цифр, не сканируя товар."""
        cfg = self.get_config()
        try:
            img = printer_tspl.render_label_preview_image(cfg, elements or {}, cell_number or "")
        except Exception as e:  # noqa: BLE001 — например, битый путь к картинке шаблона
            log.exception("Ошибка рендера превью макета")
            return {"ok": False, "error": str(e)}
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {"ok": True, "image_base64": base64.b64encode(buf.getvalue()).decode("ascii")}

    def run_health_checks(self) -> dict:
        cfg = self.get_config()
        checks = health_check.run_health_checks(cfg)
        return {"checks": checks, "has_blocking_issues": health_check.has_blocking_issues(checks)}

    def digit_templates_status(self) -> dict:
        return {"sample_counts": digit_templates.sample_counts()}

    def digit_templates_reset(self) -> dict:
        digit_templates.reset_templates()
        return {"ok": True, "sample_counts": digit_templates.sample_counts()}

    def get_status_snapshot(self) -> dict:
        cfg = self.get_config()
        printer_status = printer_tspl.get_printer_status(cfg["printer"]["name"])
        return {
            "status": self.status,
            "status_text": self.status_text,
            "paused": self._user_paused,
            "scanner_device_bound": bool(cfg["scanner_detection"].get("bound_device_path")),
            "scanner_device_name": cfg["scanner_detection"].get("bound_device_name"),
            "last_cell_number": self.last_cell_number,
            "last_print_mode": self.last_print_mode,
            "print_mode": cfg["capture"].get("print_mode", "ocr"),
            "last_scan_time": self.last_scan_time,
            "last_error": self.last_error,
            "last_confidence": self.last_confidence,
            "roll_remaining": cfg["printer"].get("roll_remaining_labels"),
            "printer_online": printer_status["online"],
            "printer_status_text": printer_status["status_text"],
        }

    def start(self):
        self.scanner.start()
        if self.device_scanner is not None:
            self.device_scanner.start()
        self._sync_scanner_mode()

    def stop(self):
        self.scanner.stop()
        if self.device_scanner is not None:
            self.device_scanner.stop()

    # ---------- калибровка привязки сканера к устройству (см. raw_input_listener.py) ----------

    def scanner_device_detect_start(self) -> "tuple[bool, str]":
        if self.device_scanner is None:
            return False, "Определение устройства недоступно (pywin32 не установлен или платформа не Windows)"
        self.device_scanner.begin_detect()
        return True, ""

    def scanner_device_detect_cancel(self):
        if self.device_scanner is not None:
            self.device_scanner.cancel_detect()

    def scanner_device_detect_poll(self) -> Optional[dict]:
        if self.device_scanner is None:
            return None
        return self.device_scanner.get_detected_device()

    def scanner_device_bind(self, device_path: str, device_name: str):
        self.update_config({"scanner_detection": {"bound_device_path": device_path,
                                                    "bound_device_name": device_name}})
        if self.device_scanner is not None:
            self.device_scanner.cancel_detect()

    def scanner_device_unbind(self):
        self.update_config({"scanner_detection": {"bound_device_path": None, "bound_device_name": None}})
