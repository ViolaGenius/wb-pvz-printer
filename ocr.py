# -*- coding: utf-8 -*-
"""
ocr.py
Распознавание номера ячейки на вырезанном фрагменте экрана через pytesseract.

ОБНОВЛЕНО (по фидбэку "распознавание не всегда корректно, выдаёт не тот номер"):
  - настраиваемая предобработка: апскейл, порог бинаризации (авто по Отсу ИЛИ ручной),
    инверсия (авто-определение тёмного фона ИЛИ принудительно вкл/выкл);
  - берём уверенность распознавания у tesseract (image_to_data, среднее по conf);
    результат с уверенностью ниже ocr_min_confidence считается неудачной попыткой;
  - повторные попытки (retry) теперь не идентичны: варьируем PSM и порог между
    попытками, что снижает шанс словить одну и ту же ошибку раз за разом,
    и в конце выбираем ЛУЧШИЙ кандидат по голосованию (совпадающие результаты
    среди попыток побеждают) и уверенности, а не просто "первый успех".

Если после подбора параметров (см. вкладку "Область распознавания" -> "Проверить
область", там теперь показывается % уверенности) точность всё ещё недостаточна —
следующий шаг эскалации: распознавание по шаблонам цифр вместо общего OCR
(шрифт в "Мой ПВЗ" фиксированный, такой подход может дать точность, близкую к 100%,
но требует один раз откалибровать эталонные изображения цифр 0-9). Не реализовано
в этой версии, чтобы не усложнять — это кандидат на следующий шаг, скажите если нужно.
"""

import logging
import os
import re
import sys
from collections import Counter
from typing import List, Optional, Tuple

from PIL import Image, ImageOps

import segmentation

log = logging.getLogger("ocr")


def _bundled_tesseract_dir() -> Optional[str]:
    """Папка с портативным Tesseract, если она включена в сборку
    (assets/tesseract/tesseract.exe) — как при обычном запуске из исходников,
    так и из PyInstaller --onefile exe (там assets/ распаковывается во
    временную sys._MEIPASS при каждом старте программы)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(base, "assets", "tesseract")
    return candidate if os.path.isdir(candidate) else None


try:
    import pytesseract

    _bundled_dir = _bundled_tesseract_dir()
    if _bundled_dir:
        _bundled_exe = os.path.join(_bundled_dir, "tesseract.exe")
        if os.path.isfile(_bundled_exe):
            # Портативная копия найдена рядом (или внутри onefile-exe) —
            # используем её вместо системного PATH, чтобы программа работала
            # на машине без отдельно установленного Tesseract.
            pytesseract.pytesseract.tesseract_cmd = _bundled_exe
            _bundled_tessdata = os.path.join(_bundled_dir, "tessdata")
            if os.path.isdir(_bundled_tessdata):
                os.environ["TESSDATA_PREFIX"] = _bundled_tessdata
            log.info("Используется встроенный портативный Tesseract: %s", _bundled_exe)
        else:
            log.warning(
                "Папка assets/tesseract есть, но tesseract.exe в ней не найден — "
                "сборка неполная, будет использован системный Tesseract (если установлен)."
            )
except ImportError:
    pytesseract = None

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

_DIGITS_RE = re.compile(r"\d+")


def _otsu_threshold(gray: Image.Image) -> int:
    """Простая реализация метода Отсу поверх гистограммы PIL — без numpy/opencv,
    чтобы не тянуть лишние зависимости только ради одного порога."""
    hist = gray.histogram()
    total = sum(hist)
    if total == 0:
        return 127
    sum_all = sum(i * hist[i] for i in range(256))
    sum_b = 0.0
    w_b = 0
    max_var = -1.0
    threshold = 127
    for i in range(256):
        w_b += hist[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * hist[i]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var_between = w_b * w_f * (m_b - m_f) ** 2
        if var_between > max_var:
            max_var = var_between
            threshold = i
    return threshold


def preprocess(
    img: Image.Image,
    scale: int = 3,
    threshold_mode: str = "auto",
    threshold_value: int = 150,
    invert_mode: str = "auto",
    autocrop: bool = True,
    autocrop_padding_ratio: float = 0.15,
) -> Image.Image:
    """Возвращает чёрно-белое изображение (чёрный текст на белом фоне — это
    предпочитает tesseract), готовое к распознаванию."""
    gray = ImageOps.grayscale(img)
    w, h = gray.size
    scale = max(1, int(scale))
    gray = gray.resize((max(1, w * scale), max(1, h * scale)), Image.LANCZOS)

    hist = gray.histogram()
    total = sum(hist) or 1
    mean_brightness = sum(i * hist[i] for i in range(256)) / total

    if invert_mode == "always":
        invert = True
    elif invert_mode == "never":
        invert = False
    else:  # auto — если область в среднем тёмная, значит текст скорее всего светлый на тёмном фоне
        invert = mean_brightness < 127

    if invert:
        gray = ImageOps.invert(gray)

    if threshold_mode == "auto":
        thr = _otsu_threshold(gray)
    else:
        thr = int(threshold_value)

    bw = gray.point(lambda p: 255 if p > thr else 0)

    if autocrop:
        bw = _autocrop_to_content(bw, padding_ratio=autocrop_padding_ratio)

    return bw


def _autocrop_to_content(bw: Image.Image, padding_ratio: float = 0.15) -> Image.Image:
    """Обрезает Ч/Б изображение (0=чёрный текст, 255=белый фон) по фактической
    границе тёмных пикселей с небольшим отступом. Область калибровки пользователь
    обычно выделяет "с запасом" — лишние поля вокруг цифр (особенно если ширина
    поля в "Мой ПВЗ" не совпадает точно с длиной номера) сбивают tesseract на
    коротких PSM-режимах чаще, чем кажется. Если содержимого не найдено (пустая
    область/белый лист) — возвращает исходное изображение без изменений."""
    # invert: в результате инверсии бывший чёрный текст (0) становится 255 —
    # getbbox() ищет bbox именно ненулевых пикселей
    mask = ImageOps.invert(bw)
    bbox = mask.getbbox()
    if not bbox:
        return bw
    x0, y0, x1, y1 = bbox
    w, h = bw.size
    pad_x = max(1, int((x1 - x0) * padding_ratio))
    pad_y = max(1, int((y1 - y0) * padding_ratio))
    x0 = max(0, x0 - pad_x)
    y0 = max(0, y0 - pad_y)
    x1 = min(w, x1 + pad_x)
    y1 = min(h, y1 + pad_y)
    return bw.crop((x0, y0, x1, y1))


def _preprocess_cv2(
    img: Image.Image,
    scale: int = 3,
    invert_mode: str = "auto",
    autocrop: bool = True,
    autocrop_padding_ratio: float = 0.15,
) -> Optional[Image.Image]:
    """Альтернативная предобработка через OpenCV: адаптивная бинаризация по
    локальным окнам (cv2.adaptiveThreshold) + морфологическое "открытие" для
    чистки мелкого шума по краям символов. В отличие от глобального порога
    Отсу (см. preprocess()), адаптивный порог устойчивее к неравномерной
    подсветке/градиенту фона внутри самой области — например, если у поля с
    номером ячейки есть лёгкая тень или неравномерная заливка. Используется
    как ЕЩЁ ОДИН независимый "голос" в ансамбле (см. recognize_with_retries),
    не заменяет обычный preprocess() полностью.

    Возвращает None, если OpenCV не установлен (см. cv2_available) — вызывающий
    код должен пропустить этот вариант и продолжить с обычным preprocess()."""
    if cv2 is None or np is None:
        return None
    arr = np.array(ImageOps.grayscale(img))
    scale = max(1, int(scale))
    if scale != 1:
        arr = cv2.resize(arr, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)

    mean_brightness = float(arr.mean())
    if invert_mode == "always":
        invert = True
    elif invert_mode == "never":
        invert = False
    else:
        invert = mean_brightness < 127
    if invert:
        arr = 255 - arr

    block_size = 31 if min(arr.shape) > 40 else 15  # нечётное, должно быть меньше меньшей стороны картинки
    if block_size % 2 == 0:
        block_size += 1
    try:
        bw_arr = cv2.adaptiveThreshold(
            arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, 10,
        )
    except cv2.error:
        log.debug("cv2.adaptiveThreshold не применился (слишком маленькая область) — пропуск варианта")
        return None
    kernel = np.ones((2, 2), np.uint8)
    bw_arr = cv2.morphologyEx(bw_arr, cv2.MORPH_OPEN, kernel)

    bw = Image.fromarray(bw_arr).convert("L")
    if autocrop:
        bw = _autocrop_to_content(bw, padding_ratio=autocrop_padding_ratio)
    return bw


def _recognize_segmented(char_images: List[Image.Image], digits_only: bool) -> Tuple[Optional[str], float]:
    """Распознаёт КАЖДЫЙ символ ПО ОТДЕЛЬНОСТИ (--psm 10, один символ) и
    склеивает результат. Одиночный символ tesseract обычно узнаёт надёжнее,
    чем короткую строку целиком — используется как ещё один "голос" в
    ансамбле (см. recognize_with_retries), а не замена основному распознаванию.
    Если хоть один символ не распознался — вся попытка считается неудачной
    (не подставляем угадайки в середину номера)."""
    if pytesseract is None or not char_images:
        return None, 0.0
    config_parts = ["--psm 10"]
    if digits_only:
        config_parts.append("-c tessedit_char_whitelist=0123456789")
    config = " ".join(config_parts)

    digits = []
    confs = []
    for char_img in char_images:
        try:
            data = pytesseract.image_to_data(char_img, config=config, output_type=pytesseract.Output.DICT)
        except Exception:  # noqa: BLE001
            return None, 0.0
        text = "".join(t for t in data.get("text", []) if t).strip()
        match = _DIGITS_RE.search(text)
        if not match or not match.group(0):
            return None, 0.0
        digits.append(match.group(0)[0])
        c = [float(cv) for cv in data.get("conf", []) if str(cv) not in ("-1", "-1.0")]
        confs.append(sum(c) / len(c) if c else 0.0)
    return "".join(digits), (sum(confs) / len(confs) if confs else 0.0)


def cv2_available() -> bool:
    return cv2 is not None


def _run_tesseract(processed: Image.Image, digits_only: bool, psm: int) -> Tuple[Optional[str], float]:
    """Один прогон tesseract. Возвращает (строка_цифр_или_None, средняя_уверенность_0-100)."""
    if pytesseract is None:
        log.error("pytesseract не установлен")
        return None, 0.0

    config_parts = [f"--psm {psm}"]
    if digits_only:
        config_parts.append("-c tessedit_char_whitelist=0123456789")
    config = " ".join(config_parts)

    try:
        data = pytesseract.image_to_data(processed, config=config, output_type=pytesseract.Output.DICT)
    except Exception:  # noqa: BLE001 — tesseract может быть не установлен / не в PATH
        log.exception("Ошибка вызова tesseract")
        return None, 0.0

    combined = "".join(t for t in data.get("text", []) if t)
    match = _DIGITS_RE.search(combined)
    if not match:
        return None, 0.0
    digits = match.group(0)

    confs = [float(c) for c in data.get("conf", []) if str(c) not in ("-1", "-1.0")]
    confidence = sum(confs) / len(confs) if confs else 0.0
    return digits, confidence


def recognize_digits(
    img: Image.Image,
    digits_only: bool = True,
    min_digits: int = 1,
    max_digits: int = 4,
    psm: int = 7,
    scale: int = 3,
    threshold_mode: str = "auto",
    threshold_value: int = 150,
    invert_mode: str = "auto",
    autocrop: bool = True,
    preprocessing_method: str = "pil",
) -> Tuple[Optional[str], float, Image.Image]:
    """Один прогон с предобработкой + валидацией длины.
    Возвращает (результат_или_None, уверенность_0-100, обработанное_изображение)
    — обработанное изображение полезно для превью в "Проверить область".

    preprocessing_method: "pil" (глобальный порог Отсу/ручной, см. preprocess())
    или "cv2_adaptive" (адаптивная бинаризация OpenCV, см. _preprocess_cv2()) —
    если OpenCV не установлен, автоматически откатывается на "pil"."""
    processed = None
    if preprocessing_method == "cv2_adaptive":
        processed = _preprocess_cv2(img, scale=scale, invert_mode=invert_mode,
                                     autocrop=autocrop, autocrop_padding_ratio=0.15)
    if processed is None:
        processed = preprocess(img, scale=scale, threshold_mode=threshold_mode,
                                threshold_value=threshold_value, invert_mode=invert_mode,
                                autocrop=autocrop)
    digits, confidence = _run_tesseract(processed, digits_only, psm)
    if digits is None:
        return None, 0.0, processed
    if not (min_digits <= len(digits) <= max_digits):
        log.debug("OCR результат %r не проходит проверку длины [%s..%s]", digits, min_digits, max_digits)
        return None, confidence, processed
    return digits, confidence, processed


def in_plausible_range(digits: str, cfg_capture: dict) -> bool:
    """Доп. фильтр (опциональный, выключен по умолчанию — оба поля 0): если
    известно, что номера ячеек в ПВЗ лежат в определённом диапазоне (например,
    1-120), результаты вне диапазона отбрасываются ещё до печати, даже если по
    формату (кол-во цифр) прошли. Дёшево и сильно снижает шанс напечатать
    случайно распознанное число, которого физически не может быть на стеллаже."""
    lo = cfg_capture.get("plausible_min_number", 0)
    hi = cfg_capture.get("plausible_max_number", 0)
    if not lo and not hi:
        return True  # диапазон не настроен — фильтр выключен
    try:
        value = int(digits)
    except ValueError:
        return True  # не число (не должно происходить при digits_only) — не мешаем
    if lo and value < lo:
        return False
    if hi and value > hi:
        return False
    return True


def recognize_with_retries(
    capture_fn,
    cfg_capture: dict,
    retry_count: int,
    retry_delay_ms: int,
    sleep_fn=None,
) -> Tuple[Optional[str], Optional[Image.Image], float]:
    """capture_fn() должна возвращать PIL.Image области (или None, если окно недоступно).

    Ансамбль из НЕСКОЛЬКИХ независимых "голосов" на каждую попытку:
      1. tesseract на всей строке целиком (PIL-предобработка, варьируются PSM/порог) —
         как и раньше, основной голос.
      2. на последней попытке (если доступен OpenCV) — тот же tesseract, но на
         картинке после адаптивной бинаризации OpenCV вместо глобального порога
         Отсу — лучше держит неровную подсветку.
      3. посимвольное распознавание (см. _recognize_segmented) — каждая цифра
         отдельно, обычно надёжнее строки целиком.
      4. самообучающиеся эталоны цифр (см. digit_templates.py) — сравнение
         формы символа с накопленными "правильными" примерами; голосует, только
         если для ВСЕХ цифр номера эталон уже "созрел" (иначе честно молчит).

    Все голоса складываются в один список кандидатов и голосуют между собой —
    самый частый результат побеждает, при равенстве — с более высокой средней
    уверенностью. Кандидаты с уверенностью ниже cfg_capture['ocr_min_confidence']
    или вне "разумного диапазона" номеров (см. in_plausible_range) отбрасываются.

    После голосования — если результат оказался очень уверенным (см.
    digit_templates_harvest_*) — символы уходят на самообучение эталонов.

    Возвращает (номер_или_None, последнее_обработанное_изображение, уверенность_победителя)."""
    import time as _time
    sleep_fn = sleep_fn or _time.sleep

    min_confidence = cfg_capture.get("ocr_min_confidence", 40)
    scale = cfg_capture.get("ocr_upscale_factor", 3)
    threshold_mode = cfg_capture.get("ocr_threshold_mode", "auto")
    threshold_value = cfg_capture.get("ocr_threshold_value", 150)
    invert_mode = cfg_capture.get("ocr_invert_mode", "auto")
    digits_only = cfg_capture.get("ocr_digits_only", True)
    min_digits = cfg_capture.get("min_digits", 1)
    max_digits = cfg_capture.get("max_digits", 4)
    autocrop = cfg_capture.get("ocr_autocrop", True)
    segmentation_enabled = cfg_capture.get("ocr_segmentation_enabled", True)
    opencv_enabled = cfg_capture.get("ocr_opencv_enabled", True) and cv2_available()
    templates_enabled = cfg_capture.get("digit_templates_enabled", True)
    templates_min_samples = cfg_capture.get("digit_templates_min_samples", 5)
    templates_min_score = cfg_capture.get("digit_templates_min_match_score", 75)
    harvest_enabled = cfg_capture.get("digit_templates_harvest_enabled", True)
    harvest_min_confidence = cfg_capture.get("digit_templates_harvest_min_confidence", 90)
    harvest_min_agreement = cfg_capture.get("digit_templates_harvest_min_agreement", 2)
    templates_max_samples = cfg_capture.get("digit_templates_max_samples_per_digit", 5)

    psm_variants = [7, 8]  # 7 = одна строка текста, 8 = одно "слово" — оба уместны для короткого номера
    threshold_deltas = [0, -15, 15]  # лёгкая вариация порога между попытками при ручном режиме

    attempts = max(1, retry_count)
    candidates = []  # список (digits, confidence, processed_image) — картинка нужна для харвестинга эталонов
    last_processed = None

    for attempt in range(attempts):
        img = capture_fn()
        if img is None:
            return None, last_processed, 0.0

        psm = psm_variants[attempt % len(psm_variants)]
        thr_value = threshold_value + threshold_deltas[attempt % len(threshold_deltas)]
        # OpenCV-вариант — дороже по вычислениям, поэтому пробуем только на
        # последней попытке, а не на каждой (баланс точности и скорости на
        # слабом ПК, см. обсуждение с пользователем)
        use_cv2 = opencv_enabled and attempt == attempts - 1

        digits, confidence, processed = recognize_digits(
            img,
            digits_only=digits_only,
            min_digits=min_digits,
            max_digits=max_digits,
            psm=psm,
            scale=scale,
            threshold_mode=threshold_mode,
            threshold_value=thr_value,
            invert_mode=invert_mode,
            autocrop=autocrop,
            preprocessing_method="cv2_adaptive" if use_cv2 else "pil",
        )
        last_processed = processed
        if digits is not None and confidence >= min_confidence and in_plausible_range(digits, cfg_capture):
            candidates.append((digits, confidence, processed))

        # доп. голоса на основе той же обработанной картинки — сегментация и эталоны
        if segmentation_enabled and processed is not None:
            try:
                char_images = segmentation.segment_characters(processed)
            except Exception:  # noqa: BLE001
                char_images = []
                log.exception("Ошибка сегментации на символы")

            if min_digits <= len(char_images) <= max_digits:
                seg_digits, seg_conf = _recognize_segmented(char_images, digits_only)
                if seg_digits and seg_conf >= min_confidence and in_plausible_range(seg_digits, cfg_capture):
                    candidates.append((seg_digits, seg_conf, processed))

                if templates_enabled:
                    try:
                        import digit_templates
                        tmpl_digits, tmpl_score = digit_templates.match_number(
                            char_images,
                            min_samples_to_trust=templates_min_samples,
                            min_score=templates_min_score,
                        )
                        if tmpl_digits and in_plausible_range(tmpl_digits, cfg_capture):
                            candidates.append((tmpl_digits, tmpl_score, processed))
                    except Exception:  # noqa: BLE001
                        log.exception("Ошибка сравнения с эталонами цифр")

        if attempt < attempts - 1:
            sleep_fn(retry_delay_ms / 1000.0)

    if not candidates:
        return None, last_processed, 0.0

    # голосование: чаще встречающийся результат побеждает, при равенстве — выше уверенность
    counts = Counter(d for d, _, _ in candidates)
    best_count = max(counts.values())
    tied = [d for d, c in counts.items() if c == best_count]
    best_digits = max(
        tied,
        key=lambda d: max(conf for dd, conf, _ in candidates if dd == d),
    )
    matching = [(conf, proc) for dd, conf, proc in candidates if dd == best_digits]
    best_conf = max(conf for conf, _ in matching)

    # самообучение эталонов: только если результат очень уверенный И минимум
    # harvest_min_agreement голосов из ансамбля с ним согласились (см. docstring
    # digit_templates.py про риски "выучить" ошибочную цифру)
    if harvest_enabled and templates_enabled and best_conf >= harvest_min_confidence and best_count >= harvest_min_agreement:
        try:
            import digit_templates
            _, best_processed = matching[0]
            char_images = segmentation.segment_characters(best_processed) if best_processed is not None else []
            if len(char_images) == len(best_digits):
                saved = digit_templates.harvest_from_result(
                    best_digits, char_images, max_samples_per_digit=templates_max_samples
                )
                if saved:
                    log.debug("Эталоны цифр: сохранено %d образцов из результата %r", saved, best_digits)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка самообучения эталонов цифр")

    return best_digits, last_processed, best_conf
