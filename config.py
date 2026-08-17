"""
config.py
Хранение и загрузка настроек приложения.

Файл конфигурации лежит в %APPDATA%\\WB_PVZ_Printer\\config.json
(если запущено не из exe — можно переопределить переменной окружения
WB_PVZ_CONFIG_DIR, это удобно для разработки/тестов на не-Windows машине).

ДОПУЩЕНИЕ: используем %APPDATA%, а не папку рядом с exe, чтобы настройки
переживали обновление/переустановку программы (перезапись exe новым
PyInstaller-сборкой). Экспорт/импорт (раздел 3.6 ТЗ) как раз и предназначен
для переноса конфигурации между машинами/переустановками.
"""

import json
import os
import copy
import shutil
import threading
from pathlib import Path

APP_DIR_NAME = "WB_PVZ_Printer"

DEFAULT_CONFIG = {
    "printer": {
        "name": "XP-370B",
        "label_width_mm": 40,
        "label_height_mm": 30,
        "gap_mm": 2,
        "dpi": 203,
        "speed": 4,
        "density": 8,
        "direction": 1,
        # ВАЖНО: на реальном Xprinter XP-370B (подтверждено пользователем) BITMAP без
        # инверсии печатался в негативе — чёрный фон/белый текст вместо белого фона/
        # чёрного текста. Поэтому по умолчанию инверсия ВКЛЮЧЕНА. Если на каком-то
        # другом экземпляре принтера окажется наоборот — выключите галку "Инвертировать
        # битмап" на вкладке "Принтер" в UI, перепечатывать код не нужно.
        "bitmap_invert": True,
        # Горизонтальный/вертикальный сдвиг всей печати, мм. Положительное значение —
        # сдвиг вправо/вниз, отрицательное — влево/вверх. Нужен, когда рулон физически
        # смещён относительно печатающей головки/датчика и содержимое ложится не по
        # центру этикетки.
        # ВАЖНО: на некоторых моделях/при некоторых физических заправках рулона оси X/Y
        # в командах печати оказываются "перепутаны" относительно того, что видно на
        # честном превью в редакторе (превью всегда рисуется в системе координат
        # "как задумано" — x_mm вправо, y_mm вниз) — тогда offset_x_mm на бумаге
        # реально двигает по вертикали, а offset_y_mm — по горизонтали. Если так —
        # просто используйте то поле в UI, которое реально даёт нужный эффект на
        # вашем принтере, оба входят в силу одинаково для всех элементов.
        "offset_x_mm": 0,
        "offset_y_mm": 0,
        "roll_total_labels": 500,
        "roll_remaining_labels": 500,
    },
    "capture": {
        "window_title": "Мой ПВЗ",
        # по умолчанию ищем окно по вхождению подстроки в заголовок (надёжнее),
        # можно переключить на точное совпадение в настройках
        "exact_title_match": False,
        # координаты области распознавания ОТНОСИТЕЛЬНО клиентской области окна, в пикселях экрана
        "region_relative": {"x": 0, "y": 0, "width": 0, "height": 0},
        "ocr_digits_only": True,
        "min_digits": 1,
        "max_digits": 4,
        # --- тюнинг качества OCR (см. ocr.py) ---
        "ocr_upscale_factor": 3,          # во сколько раз увеличивать вырезанную область перед OCR
        "ocr_threshold_mode": "auto",     # "auto" (Оцу) | "manual"
        "ocr_threshold_value": 150,       # используется только при ocr_threshold_mode == "manual"
        "ocr_invert_mode": "auto",        # "auto" (определять по средней яркости) | "always" | "never"
        "ocr_min_confidence": 40,         # 0-100, средняя уверенность tesseract; ниже — попытка считается неудачной
        # --- доп. улучшения точности (без тяжёлых зависимостей и ручной калибровки) ---
        "ocr_autocrop": True,             # обрезать по фактической границе цифр перед OCR (убирает случайные поля)
        "ocr_stability_check_enabled": True,  # дождаться, пока кадр перестанет меняться, перед захватом для OCR
        "ocr_stability_max_wait_ms": 150,     # максимум ожидания стабилизации кадра, мс
        "plausible_min_number": 0,        # 0 = не ограничено. Если номера ячеек известны заранее (напр. 1-120) —
        "plausible_max_number": 0,        # результаты вне диапазона отбрасываются ещё до печати
        # --- ансамбль распознавания: доп. независимые "голоса" (см. ocr.py) ---
        "ocr_segmentation_enabled": True,      # посимвольное распознавание как доп. голос
        "ocr_opencv_enabled": True,            # адаптивная бинаризация OpenCV как доп. голос (если установлен)
        "digit_templates_enabled": True,       # самообучающиеся эталоны цифр как доп. голос
        "digit_templates_min_samples": 5,          # сколько образцов нужно накопить, чтобы эталон "созрел"
        "digit_templates_max_samples_per_digit": 5,  # сколько последних образцов хранить на цифру
        "digit_templates_min_match_score": 75,     # порог схожести (0-100) для доверия совпадению
        "digit_templates_harvest_enabled": True,   # разрешить самообучение (сбор новых образцов)
        "digit_templates_harvest_min_confidence": 90,  # минимальная уверенность результата для харвестинга
        "digit_templates_harvest_min_agreement": 2,    # минимум согласившихся голосов в ансамбле для харвестинга
        # --- цветовые точки-триггеры (см. ТЗ п.3, доп. функция) ---
        "color_triggers": [],
        # пример элемента: {"id": "t1", "enabled": True, "x": 0, "y": 0,
        #                    "color_rgb": [0, 200, 0], "tolerance_percent": 12}
        "color_triggers_logic": "AND",  # "AND" — все включённые точки должны совпасть, "OR" — хотя бы одна
        "color_trigger_log_skips": True,  # писать ли в журнал нейтральную запись при отмене печати по цвету
        # --- способ печати номера ячейки ---
        # "ocr" — как раньше: распознать цифры (tesseract + доп. голоса) и напечатать
        #         их заново отрисованным текстом по шаблону (автоцентровка/автошрифт).
        # "screenshot" — НЕ распознавать вообще: обрезать захваченную область по
        #         фактической границе содержимого (см. autocrop_number_screenshot в
        #         printer_tspl.py — чтобы в печать не лез случайный мусор по краям
        #         области калибровки) и напечатать этот кусок скриншота КАК КАРТИНКУ,
        #         вписав его в рамку элемента "cell_number" шаблона. Полностью убирает
        #         ошибки распознавания (печатается буквально то, что было на экране) и
        #         не тратит время на запуск tesseract — но теряется проверка "похоже ли
        #         вообще на номер" (см. plausible_min/max_number — в этом режиме не
        #         работает) и поиск по номеру в журнале печати.
        "print_mode": "ocr",
        "screenshot_autocrop_padding": 0.15,  # отступ вокруг найденного содержимого, доля от размера bbox
        # Утолщение тёмных штрихов на куске скриншота перед печатью (см.
        # printer_tspl.boldify_image) — на тонких шрифтах номера после перевода в
        # 1-бит для термопечати штрихи могут бледнеть/рассыпаться, это компенсирует.
        # Не влияет на режим "ocr" (там текст перерисовывается заново своим шрифтом,
        # см. "bold" в самом шаблоне этикетки).
        "screenshot_bold": False,
        "screenshot_bold_strength": 1,  # 1-5, сколько раз применить утолщение (крупнее — жирнее)
        "screenshot_conversion": {
            # тот же набор полей и методов, что и у image_conversion картинки в редакторе
            # этикетки (см. printer_tspl.render_image_bw) — по умолчанию авто-порог (Отсу),
            # т.к. он не требует ручной подстройки под конкретную яркость скриншота
            "method": "otsu",
            "threshold_value": 160,
            "edge_threshold": 60,
            "halftone_cell_px": 6,
            "contrast": 1.0,
            "brightness": 1.0,
            "gamma": 1.0,
            "black_point": 0,
            "white_point": 255,
            "adaptive_block_px": 25,
            "adaptive_bias": 10,
            "sharpen": False,
            "invert": False,
        },
    },
    "scanner_detection": {
        "max_interkey_ms": 40,
        "min_code_length": 3,
        "post_scan_delay_ms": 400,
        "duplicate_debounce_ms": 1500,
        # --- привязка к конкретному физическому устройству, см. raw_input_listener.py ---
        # Если bound_device_path задан — программа реагирует ТОЛЬКО на ввод с этого
        # устройства (через Windows Raw Input API, различает физические HID-устройства
        # по хендлу) и полностью игнорирует остальную клавиатуру и другие устройства.
        # В этом случае эвристика по времени между символами (max_interkey_ms) не
        # применяется вообще — раз устройство точно определено, не нужно гадать по
        # скорости набора, и ложные срабатывания от быстро печатающего человека на
        # обычной клавиатуре становятся невозможны. Привязывается на вкладке "Сканер":
        # там показывается QR-код, его нужно один раз отсканировать НУЖНЫМ сканером.
        # Если None — работает старое поведение (эвристика по времени), как раньше.
        "bound_device_path": None,
        "bound_device_name": None,
    },
    "recognition": {
        "retry_count": 3,
        "retry_delay_ms": 300,
    },
    "sounds": {
        "on_capture_enabled": True,
        "on_success_enabled": True,
        "on_error_enabled": True,
    },
    "active_template": "default",
    "templates": {
        "default": {
            "elements": {
                # cell_number и static_text теперь задаются ПРЯМОУГОЛЬНИКОМ (как bar/image):
                # текст автоматически центрируется внутри рамки и автоматически подбирается
                # максимальный размер шрифта, вписывающийся в рамку (см. printer_tspl.py:
                # render_autofit_text_bitmap). Поля font_size_pt/font больше не используются.
                "cell_number": {"x_mm": 4, "y_mm": 2, "width_mm": 32, "height_mm": 12, "bold": True},
                "bar": {"x_mm": 2, "y_mm": 15, "width_mm": 36, "height_mm": 2},
                "static_text": {
                    "content": "",
                    "x_mm": 2,
                    "y_mm": 19,
                    "width_mm": 36,
                    "height_mm": 6,
                    "bold": False,
                },
                "image": {
                    "path": "",
                    "x_mm": 2,
                    "y_mm": 23,
                    "width_mm": 10,
                    "height_mm": 6,
                    # Как эта КОНКРЕТНАЯ картинка переводится в чёрно-белую для печати —
                    # раньше было общей настройкой на вкладке "Принтер" на всё приложение,
                    # теперь у каждой картинки в каждом шаблоне свои настройки (редактор
                    # этикетки). Термопринтер физически умеет только "точка есть / нет".
                    "image_conversion": {
                        "method": "dither",   # "dither" (Флойд-Стейнберг) | "threshold" (жёсткий порог) |
                                               # "ordered" (упорядоченный дизеринг, матрица Байера) |
                                               # "halftone" (полутоновый растр кружками) | "edge" (только контуры)
                        "threshold_value": 160,   # только для method == "threshold" (0-255)
                        "edge_threshold": 60,     # только для method == "edge" (0-255)
                        "halftone_cell_px": 6,    # только для method == "halftone" — размер ячейки растра
                        "contrast": 1.0,          # 1.0 — без изменений
                        "brightness": 1.0,        # 1.0 — без изменений
                        "gamma": 1.0,             # 1.0 — без изменений, гамма-коррекция перед конвертацией
                        "sharpen": False,         # лёгкая резкость (UnsharpMask) перед конвертацией
                        "invert": False,          # инвертировать цвет картинки (негатив) — НЕ то же самое,
                                                   # что printer.bitmap_invert (полярность конкретного принтера)
                    },
                },
            }
        }
    },
    "app": {
        "autostart": True,
        "start_paused": False,
        "web_ui_port": 8765,
        # --- глобальные горячие клавиши (библиотека keyboard, см. hotkeys.py) ---
        "hotkeys_enabled": True,
        "hotkey_pause": "ctrl+alt+p",
        "hotkey_repeat_print": "ctrl+alt+r",
        "hotkey_open_settings": "ctrl+alt+o",
    },
}

_lock = threading.Lock()


def get_app_data_dir() -> Path:
    override = os.environ.get("WB_PVZ_CONFIG_DIR")
    if override:
        p = Path(override)
    else:
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / APP_DIR_NAME
        else:
            # не-Windows / нет APPDATA — резервный вариант для разработки
            p = Path.home() / f".{APP_DIR_NAME.lower()}"
    p.mkdir(parents=True, exist_ok=True)
    (p / "logs").mkdir(exist_ok=True)
    (p / "images").mkdir(exist_ok=True)
    return p


def get_config_path() -> Path:
    return get_app_data_dir() / "config.json"


def get_print_log_path() -> Path:
    return get_app_data_dir() / "print_log.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно накладывает override поверх base, не теряя новые ключи по умолчанию
    при обновлении программы (старый config.json может не содержать новых полей)."""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> dict:
    path = get_config_path()
    if not path.exists():
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        save_config(cfg)
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        # повреждённый файл — не роняем приложение, откатываемся на дефолт,
        # повреждённый файл сохраняем рядом для диагностики
        backup = path.with_suffix(".json.broken")
        try:
            shutil.copy(path, backup)
        except OSError:
            pass
        raw = {}
    return _deep_merge(DEFAULT_CONFIG, raw)


def save_config(cfg: dict) -> None:
    path = get_config_path()
    with _lock:
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        tmp.replace(path)  # атомарная замена — не оставит битый файл при сбое середины записи


def update_config(patch: dict) -> dict:
    cfg = load_config()
    cfg = _deep_merge(cfg, patch)
    save_config(cfg)
    return cfg


def export_config(dest_path: str) -> None:
    cfg = load_config()
    with open(dest_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def import_config(src_path: str) -> dict:
    with open(src_path, "r", encoding="utf-8") as f:
        incoming = json.load(f)
    cfg = _deep_merge(DEFAULT_CONFIG, incoming)
    save_config(cfg)
    return cfg
