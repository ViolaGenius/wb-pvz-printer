"""
printer_tspl.py
Формирование TSPL-команд и печать "сырыми" данными на Xprinter XP-370B через win32print.

Синтаксис команд проверен по официальной документации TSPL/TSPL2 (TSC/Xprinter
используют общий диалект языка):
  SIZE w mm, h mm      — обязателен пробел перед "mm"
  GAP  g mm, 0 mm
  DIRECTION 0|1
  CLS
  TEXT x,y,"font",rotation,x-mult,y-mult,"content"
  BAR  x,y,width,height
  BITMAP x,y,width_bytes,height_dots,mode,<raw bitmap data>
  PRINT copies,sets

Координаты у всех команд — в точках (dots), не в мм. Перевод мм -> dots делаем
через DPI: dots = round(mm / 25.4 * dpi).

ВАЖНО (пометка допущения): полярность бит в BITMAP (какой бит соответствует
запечатанной чёрной точке) исторически отличается между прошивками. Реализовано
так, что бит=1 означает "печатать чёрную точку" (mode=0, OVERWRITE). Если на
реальном принтере картинка печатается в негативе — переключите
printer.bitmap_invert в true в настройках, перепечатывать код не нужно.
"""

import os
import sys
import logging
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance, ImageFilter

log = logging.getLogger("printer_tspl")

try:
    import win32print
except ImportError:  # для разработки/тестов вне Windows
    win32print = None

try:
    import numpy as np
except ImportError:  # numpy опционален — ниже везде есть чистый Pillow-фолбэк
    np = None

# ---- кэш исходных картинок для превью/печати ----
# Раньше build_label_from_template/render_label_preview_image открывали файл с
# диска (Image.open) КАЖДЫЙ раз — а превью в редакторе дёргает рендер на каждое
# перетаскивание мышью, т.е. один и тот же файл перечитывался с диска десятки
# раз в секунду. Кэшируем декодированную картинку по (путь, mtime, размер) —
# если файл не менялся с прошлого раза, диск и повторное JPEG/PNG-декодирование
# не трогаем вообще.
_SOURCE_IMAGE_CACHE: dict = {}
_SOURCE_IMAGE_CACHE_MAX = 8


def _load_source_image_cached(path: str) -> Image.Image:
    """Возвращает КОПИЮ декодированной картинки (безопасно мутировать у вызывающего
    кода) — при этом сама декодировка из файла происходит не чаще, чем меняется файл."""
    st = os.stat(path)
    cache_key = (path, st.st_mtime_ns, st.st_size)
    cached = _SOURCE_IMAGE_CACHE.get(path)
    if cached is not None and cached[0] == cache_key:
        return cached[1].copy()
    with Image.open(path) as im:
        im.load()
        loaded = im.copy()
    if len(_SOURCE_IMAGE_CACHE) >= _SOURCE_IMAGE_CACHE_MAX and path not in _SOURCE_IMAGE_CACHE:
        _SOURCE_IMAGE_CACHE.pop(next(iter(_SOURCE_IMAGE_CACHE)))
    _SOURCE_IMAGE_CACHE[path] = (cache_key, loaded)
    return loaded.copy()


def _assets_dir() -> str:
    """Путь к папке assets/ — работает и при обычном запуске, и из PyInstaller
    --onefile сборки (там ресурсы распаковываются во временную sys._MEIPASS)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "assets")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


FONT_REGULAR_PATH = os.path.join(_assets_dir(), "fonts", "DejaVuSans.ttf")
FONT_BOLD_PATH = os.path.join(_assets_dir(), "fonts", "DejaVuSans-Bold.ttf")


def mm_to_dots(mm: float, dpi: int) -> int:
    return int(round(mm / 25.4 * dpi))


def render_autofit_text_bitmap(text: str, width_dots: int, height_dots: int,
                                bold: bool = True, padding_ratio: float = 0.88,
                                line_spacing_ratio: float = 0.25) -> Image.Image:
    """Рендерит текст в монохромное изображение размером ровно width_dots x height_dots
    (белый фон, чёрный текст), подбирая МАКСИМАЛЬНЫЙ размер шрифта, который вписывается
    в рамку (с отступом padding_ratio), и центрируя текст по обеим осям.

    Поддерживает МНОГОСТРОЧНЫЙ текст: перенос строки — символ '\\n' (в UI это
    Enter в поле "Текст", которое теперь textarea, а не однострочный input).
    Каждая строка центрируется по горизонтали, а весь блок строк — по вертикали
    внутри рамки, как единое целое (через draw.multiline_textbbox/multiline_text
    с align="center").

    Это заменяет прежний подход через TSPL TEXT + дискретные встроенные шрифты 1-8:
    там размер печати был почти нечувствителен к настройке "размер шрифта" (отсюда
    жалоба "печатает всегда просто маленький номер") — координатная сетка TSPL позволяет
    только целочисленные множители x_mult/y_mult поверх фиксированных битмап-шрифтов
    принтера. Рендер через PIL с произвольным TTF даёт точный контроль размера и центровку
    для ЛЮБОГО размера рамки, которую пользователь задаёт в редакторе макета.
    """
    width_dots = max(1, int(width_dots))
    height_dots = max(1, int(height_dots))
    canvas = Image.new("L", (width_dots, height_dots), color=255)
    draw = ImageDraw.Draw(canvas)
    # нормализуем переводы строк (textarea в браузере может прислать \r\n) и убираем
    # только КРАЙНИЕ пустые строки, сохраняя пустые строки-разделители внутри текста
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        return canvas

    font_path = FONT_BOLD_PATH if bold else FONT_REGULAR_PATH
    max_w = width_dots * padding_ratio
    max_h = height_dots * padding_ratio

    lo, hi = 4, max(8, height_dots * 3)  # верхняя граница с запасом — бинарный поиск сам сузит
    best_size = lo
    best_font = None
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            font = ImageFont.truetype(font_path, mid)
        except OSError:
            log.warning("Не найден шрифт %s — используется системный по умолчанию", font_path)
            font = ImageFont.load_default()
            best_font = font
            break
        spacing = max(0, int(mid * line_spacing_ratio))
        bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="center")
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= max_w and h <= max_h:
            best_size = mid
            best_font = font
            lo = mid + 1
        else:
            hi = mid - 1

    if best_font is None:
        best_font = ImageFont.truetype(font_path, max(best_size, 4))

    spacing = max(0, int(best_size * line_spacing_ratio))
    bbox = draw.multiline_textbbox((0, 0), text, font=best_font, spacing=spacing, align="center")
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width_dots - w) // 2 - bbox[0]
    y = (height_dots - h) // 2 - bbox[1]
    draw.multiline_text((x, y), text, font=best_font, fill=0, spacing=spacing, align="center")
    return canvas


def list_printers() -> List[str]:
    if win32print is None:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    printers = win32print.EnumPrinters(flags)
    return [p[2] for p in printers]


def get_printer_status(printer_name: str) -> dict:
    """Возвращает {'online': bool, 'status_text': str}.
    Точное сопоставление кодов статуса win32print у разных драйверов отличается,
    поэтому дополнительно считаем принтер офлайн, если его вообще нет в списке
    системных принтеров — это самый надёжный сигнал "отключили USB"."""
    if win32print is None:
        return {"online": False, "status_text": "win32print недоступен (не Windows)"}
    names = list_printers()
    if printer_name not in names:
        return {"online": False, "status_text": "Принтер не найден в системе"}
    try:
        handle = win32print.OpenPrinter(printer_name)
        try:
            info = win32print.GetPrinter(handle, 2)
        finally:
            win32print.ClosePrinter(handle)
        status = info.get("Status", 0)
        if status == 0:
            return {"online": True, "status_text": "Готов"}
        return {"online": False, "status_text": f"Ошибка принтера (код {status})"}
    except Exception as e:  # noqa: BLE001 — сюда стекаются разные win32-исключения
        return {"online": False, "status_text": f"Ошибка доступа к принтеру: {e}"}


def _flatten_to_white(img: Image.Image) -> Image.Image:
    """Убирает прозрачность, накладывая изображение на белый фон (иначе прозрачные
    области после конвертации в Ч/Б могут стать чёрными точками на этикетке)."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    return img.convert("RGB")


def boldify_image(img: Image.Image, strength: int = 1) -> Image.Image:
    """Утолщает тёмные штрихи (текст) на светлом фоне — актуально для режима печати
    "screenshot" (см. cfg["capture"]["screenshot_bold"]/engine.py), где на этикетку
    идёт кусок скриншота КАК ЕСТЬ: у большинства сайтов номер набран тонким шрифтом,
    и после перевода в 1-бит для термопечати (см. render_image_bw) тонкие штрихи
    легко "рассыпаются"/бледнеют. MinFilter берёт МИНИМУМ яркости по окрестности —
    для тёмного текста на светлом фоне это расширяет тёмные области наружу (ровно
    "жирный" эффект), в отличие от gamma/contrast, которые лишь резче делят по
    порогу, но не меняют толщину самих штрихов.

    strength — сколько раз применить фильтр 3x3 (1 = чуть жирнее, 3 = заметно
    жирнее — на мелком шрифте может начать "заливать" промежутки между цифрами,
    поэтому дефолт консервативный)."""
    if strength <= 0:
        return img
    out = img.convert("L") if img.mode != "L" else img.copy()
    for _ in range(min(5, max(1, strength))):
        out = out.filter(ImageFilter.MinFilter(3))
    return out.convert(img.mode) if img.mode != "L" else out


def autocrop_number_screenshot(img: Image.Image, padding_ratio: float = 0.15) -> Optional[Image.Image]:
    """Обрезает СЫРОЙ (ещё цветной, не бинаризованный) скриншот области калибровки по
    фактической границе содержимого — используется в режиме печати "screenshot"
    (см. render_image_bw/цикл сканирования в engine.py), чтобы в печать не попадали
    случайные поля вокруг цифр, если область калибровки выделена "с запасом".

    В отличие от ocr._autocrop_to_content (которая работает с УЖЕ бинаризованной
    картинкой в конкретной полярности), эта функция сама определяет, что тут "текст",
    а что "фон": строит Ч/Б-маску по автоматическому порогу Отсу, а затем считает
    "содержимым" тот из двух классов (тёмный/светлый), которого на картинке МЕНЬШЕ —
    независимо от того, тёмный текст на светлом фоне или наоборот (типичная ситуация
    для сайтов со светлой/тёмной темой). Возвращает None, если содержимого не нашлось
    (пустая/однородная область — например, окно не успело прогрузиться)."""
    gray = img.convert("L")
    thr = _otsu_threshold(gray)
    if np is not None:
        arr = np.asarray(gray)
        dark_mask = arr <= thr
        dark_ratio = float(dark_mask.mean())
        mask_arr = dark_mask if dark_ratio <= 0.5 else ~dark_mask
        if not mask_arr.any():
            return None
        ys, xs = np.where(mask_arr)
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    else:
        dark = gray.point(lambda p: 255 if p <= thr else 0).convert("L")
        dark_bbox = dark.getbbox()
        dark_count = sum(dark.histogram()[128:])  # приблизительно, без numpy — точнее не требуется
        total = gray.width * gray.height
        if dark_count / max(1, total) <= 0.5:
            bbox = dark_bbox
        else:
            light = ImageOps.invert(dark)
            bbox = light.getbbox()
        if not bbox:
            return None
        x0, y0, x1, y1 = bbox

    w, h = img.size
    pad_x = max(1, int((x1 - x0) * padding_ratio))
    pad_y = max(1, int((y1 - y0) * padding_ratio))
    x0 = max(0, x0 - pad_x)
    y0 = max(0, y0 - pad_y)
    x1 = min(w, x1 + pad_x)
    y1 = min(h, y1 + pad_y)
    if x1 <= x0 or y1 <= y0:
        return None
    return img.crop((x0, y0, x1, y1))


def render_image_bw(pil_image: Image.Image, target_w_dots: Optional[int] = None,
                     method: str = "dither", threshold_value: int = 160,
                     contrast: float = 1.0, brightness: float = 1.0,
                     gamma: float = 1.0, sharpen: bool = False,
                     halftone_cell_px: int = 6, edge_threshold: int = 60,
                     invert: bool = False, black_point: int = 0, white_point: int = 255,
                     adaptive_block_px: int = 25, adaptive_bias: int = 10) -> Image.Image:
    """Приводит ПРОИЗВОЛЬНУЮ (в т.ч. цветную) картинку к виду, в котором она реально
    напечатается на термопринтере: убирает прозрачность (на белый фон), масштабирует
    под нужную ширину в точках, применяет предобработку и переводит в чёрно-белое.
    Настройки живут в конкретном элементе шаблона (редактор этикетки, картинка) —
    у каждой картинки в шаблоне может быть свой способ конвертации.

    method — 5 разных способов получить 1-битное изображение (термопринтер физически
    умеет печатать только "точка есть / точки нет", без оттенков серого):
      - "dither" — дизеринг Флойда-Стейнберга (диффузия ошибки). Лучше для фото,
        градиентов, логотипов с полутонами — узнаваемый узор точек вместо заливки.
      - "threshold" — жёсткий порог яркости, без дизеринга. Лучше для простых чётких
        изображений (штриховые логотипы, иконки) — дизеринг там добавляет лишний шум.
      - "ordered" — упорядоченный дизеринг по матрице Байера (4x4). В отличие от
        Флойда-Стейнберга даёт регулярный, "механический" узор точек — на некоторых
        термопринтерах печатается чуть чётче/предсказуемее, чем диффузия ошибки.
      - "halftone" — полутоновый растр (как в газетной печати): картинка режется на
        квадратные ячейки, в каждой рисуется кружок, чей размер зависит от яркости
        участка. Хорошо подходит для фотографий с крупными деталями.
      - "edge" — контурный режим: печатаются только границы объектов (линии), заливки
        игнорируются. Подходит для логотипов, где важен just силуэт, и экономит чернила/
        меньше нагревает печатающую головку на больших тёмных областях.
      - "otsu" — автоматический порог (метод Отсу): сам подбирает оптимальное значение
        порога по гистограмме картинки — не нужно вручную крутить "Порог", особенно
        удобно, если картинки в шаблоне часто меняются и у каждой своя яркость.
      - "adaptive" — локальный адаптивный порог: у каждого пикселя порог считается
        от среднего по его окрестности (adaptive_block_px), а не от одного глобального
        числа на всё изображение. Хорошо держит неровное освещение/тени на фото и
        сканах, где обычный порог заливает половину картинки чёрным.

    contrast/brightness/gamma — корректировки ДО перевода в Ч/Б (через PIL ImageEnhance
    и гамма-LUT). sharpen — лёгкая резкость (UnsharpMask) перед конвертацией: иногда
    помогает мелким деталям пережить дизеринг/порог, не "размазавшись".

    black_point/white_point — уровни (как в Photoshop/Lightroom): всё темнее black_point
    уходит в чистый чёрный, всё светлее white_point — в чистый белый, между ними —
    линейная растяжка контраста. Даёт более точный контроль, чем один общий "Контраст",
    особенно для фото с блёклым/сероватым фоном перед dither/ordered/halftone.

    adaptive_block_px/adaptive_bias — только для method="adaptive": размер окрестности
    в пикселях (нечётное, крупнее — мягче/медленнее) и сдвиг порога (больше — темнее
    результат, меньше шума на однородном фоне).

    invert — инвертирует ЦВЕТ СОДЕРЖИМОГО картинки (негатив). Это НЕ то же самое, что
    printer.bitmap_invert — та настройка компенсирует аппаратную полярность конкретного
    принтера и применяется уже после этой функции, на этапе упаковки в биты.

    Возвращает изображение в режиме "L" (0=чёрный, 255=белый) — уже без полутонов."""
    flat = _flatten_to_white(pil_image)
    if target_w_dots and flat.width > 0:
        ratio = max(1, target_w_dots) / flat.width
        target_h_dots = max(1, int(round(flat.height * ratio)))
        flat = flat.resize((max(1, target_w_dots), target_h_dots), Image.LANCZOS)

    if contrast and contrast != 1.0:
        flat = ImageEnhance.Contrast(flat).enhance(contrast)
    if brightness and brightness != 1.0:
        flat = ImageEnhance.Brightness(flat).enhance(brightness)
    if sharpen:
        flat = flat.filter(ImageFilter.UnsharpMask(radius=2, percent=120, threshold=2))

    gray = flat.convert("L")
    if gamma and gamma != 1.0:
        inv_gamma = 1.0 / max(0.05, gamma)
        lut = [min(255, max(0, int((i / 255.0) ** inv_gamma * 255))) for i in range(256)]
        gray = gray.point(lut)
    if (black_point or 0) > 0 or (white_point or 255) < 255:
        bp = max(0, min(254, int(black_point or 0)))
        wp = max(bp + 1, min(255, int(white_point or 255)))
        span = max(1, wp - bp)
        lut = [min(255, max(0, int((i - bp) / span * 255))) for i in range(256)]
        gray = gray.point(lut)
    if invert:
        gray = ImageOps.invert(gray)

    if method == "threshold":
        bw = gray.point(lambda p: 255 if p > threshold_value else 0).convert("1")
    elif method == "otsu":
        auto_thr = _otsu_threshold(gray)
        bw = gray.point(lambda p: 255 if p > auto_thr else 0).convert("1")
    elif method == "adaptive":
        return _adaptive_threshold(gray, block_px=max(3, adaptive_block_px | 1), bias=adaptive_bias)
    elif method == "ordered":
        bw = _ordered_dither(gray)
    elif method == "halftone":
        return _halftone(gray, cell_px=max(2, halftone_cell_px))  # уже в режиме "L", без .convert("1")
    elif method == "edge":
        edges = gray.filter(ImageFilter.FIND_EDGES)
        bw = edges.point(lambda p: 0 if p > edge_threshold else 255).convert("1")
    else:  # "dither" (по умолчанию)
        bw = gray.convert("1")  # Floyd-Steinberg дизеринг — поведение Pillow по умолчанию
    return bw.convert("L")


def _otsu_threshold(gray: Image.Image) -> int:
    """Порог Отсу по гистограмме — O(256), не зависит от размера картинки, поэтому
    дешёвый даже для больших превью (в отличие от per-pixel методов ниже)."""
    hist = gray.histogram()
    total = sum(hist)
    if total == 0:
        return 127
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b, w_b, max_var, threshold = 0.0, 0, -1.0, 127
    for i, h in enumerate(hist):
        w_b += h
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * h
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        var_between = w_b * w_f * (m_b - m_f) ** 2
        if var_between > max_var:
            max_var, threshold = var_between, i
    return threshold


def _adaptive_threshold(gray: Image.Image, block_px: int = 25, bias: int = 10) -> Image.Image:
    """Локальный адаптивный порог: каждый пиксель сравнивается со средним по своей
    окрестности (box blur), а не с одним числом на всю картинку — блок light: bias
    больше -> темнее результат (меньше шума на ровном фоне)."""
    if np is not None:
        arr = np.asarray(gray, dtype=np.float32)
        local_mean = np.asarray(gray.filter(ImageFilter.BoxBlur(block_px // 2)), dtype=np.float32)
        out = np.where(arr > (local_mean - bias), 255, 0).astype(np.uint8)
        return Image.fromarray(out, mode="L")
    # без numpy — тот же алгоритм через Pillow point-по-двум-изображениям
    blurred = gray.filter(ImageFilter.BoxBlur(block_px // 2))
    px, bpx = gray.load(), blurred.load()
    w, h = gray.size
    out = Image.new("L", (w, h), 255)
    opx = out.load()
    for y in range(h):
        for x in range(w):
            opx[x, y] = 255 if px[x, y] > (bpx[x, y] - bias) else 0
    return out


_BAYER_4X4 = [
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
]


def _ordered_dither(gray: Image.Image) -> Image.Image:
    """Упорядоченный дизеринг по матрице Байера 4x4 — регулярный "механический" узор
    точек, в отличие от диффузии ошибки у Флойда-Стейнберга (см. render_image_bw).

    ОПТИМИЗАЦИЯ: раньше это был двойной python-цикл по каждому пикселю (w*h итераций
    чистого Python) — на больших картинках в редакторе шаблона это была одна из
    заметных причин подтормаживания live-превью при перетаскивании. С numpy — то же
    самое сравнение "яркость > порог из тайла Байера", но одной векторной операцией."""
    w, h = gray.size
    if np is not None:
        arr = np.asarray(gray, dtype=np.float32)
        bayer = (np.array(_BAYER_4X4, dtype=np.float32) + 0.5) / 16.0 * 255.0
        tiled = np.tile(bayer, ((h + 3) // 4, (w + 3) // 4))[:h, :w]
        out = np.where(arr > tiled, 255, 0).astype(np.uint8)
        return Image.fromarray(out, mode="L").convert("1")
    # фолбэк без numpy — прежняя чистая реализация на Pillow
    src = gray.load()
    out = Image.new("1", (w, h))
    dst = out.load()
    for y in range(h):
        row = _BAYER_4X4[y % 4]
        for x in range(w):
            threshold = (row[x % 4] + 0.5) / 16.0 * 255
            dst[x, y] = 1 if src[x, y] > threshold else 0  # в режиме "1": 1=белый, 0=чёрный
    return out


def _halftone(gray: Image.Image, cell_px: int = 6) -> Image.Image:
    """Полутоновый растр (AM screening) — картинка режется на квадратные ячейки
    cell_px x cell_px, в каждой рисуется кружок, чей радиус зависит от яркости
    участка (темнее — крупнее точка). Классический вид газетной печати.

    ОПТИМИЗАЦИЯ: раньше средняя яркость ячейки считалась ЧЕТЫРЬМЯ вложенными python-
    циклами (фактически перебор каждого пикселя картинки) — самое дорогое место во
    всём модуле на больших изображениях. С numpy среднее по каждой ячейке считается
    одним срезом массива; цикл остаётся только по ЧИСЛУ ЯЧЕЕК (их на порядки меньше,
    чем пикселей), там рисуем кружки через Pillow как и раньше."""
    w, h = gray.size
    draw_out = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(draw_out)
    max_radius = cell_px * 0.62

    if np is not None:
        arr = np.asarray(gray, dtype=np.float32)
        for cy in range(0, h, cell_px):
            y1 = min(cy + cell_px, h)
            for cx in range(0, w, cell_px):
                x1 = min(cx + cell_px, w)
                if x1 <= cx or y1 <= cy:
                    continue
                avg = float(arr[cy:y1, cx:x1].mean())
                darkness = 1.0 - (avg / 255.0)
                radius = darkness * max_radius
                if radius >= 0.5:
                    ccx, ccy = cx + (x1 - cx) / 2.0, cy + (y1 - cy) / 2.0
                    draw.ellipse([ccx - radius, ccy - radius, ccx + radius, ccy + radius], fill=0)
        return draw_out

    # фолбэк без numpy — прежняя чистая реализация на Pillow
    src = gray.load()
    for cy in range(0, h, cell_px):
        for cx in range(0, w, cell_px):
            x1, y1 = min(cx + cell_px, w), min(cy + cell_px, h)
            bw_, bh_ = x1 - cx, y1 - cy
            if bw_ <= 0 or bh_ <= 0:
                continue
            total = 0
            count = 0
            for yy in range(cy, y1):
                for xx in range(cx, x1):
                    total += src[xx, yy]
                    count += 1
            avg = total / count if count else 255
            darkness = 1.0 - (avg / 255.0)
            radius = darkness * max_radius
            if radius >= 0.5:
                ccx, ccy = cx + bw_ / 2.0, cy + bh_ / 2.0
                draw.ellipse([ccx - radius, ccy - radius, ccx + radius, ccy + radius], fill=0)
    return draw_out


def _image_bw_from_conv(pil_image: Image.Image, target_w_dots: int, conv: dict) -> Image.Image:
    """Общий помощник: читает словарь настроек конвертации картинки (теперь хранится
    в самом элементе шаблона image, редактор этикетки — а не глобально на вкладке
    "Принтер", как было раньше) и вызывает render_image_bw. Используется и реальной
    печатью, и WYSIWYG-превью в редакторе — чтобы оба места ЧИТАЛИ ровно одни и те
    же поля одинаковым образом и не расходились."""
    conv = conv or {}
    return render_image_bw(
        pil_image, target_w_dots=target_w_dots,
        method=conv.get("method", "dither"),
        threshold_value=conv.get("threshold_value", 160),
        contrast=conv.get("contrast", 1.0),
        brightness=conv.get("brightness", 1.0),
        gamma=conv.get("gamma", 1.0),
        sharpen=conv.get("sharpen", False),
        halftone_cell_px=conv.get("halftone_cell_px", 6),
        edge_threshold=conv.get("edge_threshold", 60),
        invert=conv.get("invert", False),
        black_point=conv.get("black_point", 0),
        white_point=conv.get("white_point", 255),
        adaptive_block_px=conv.get("adaptive_block_px", 25),
        adaptive_bias=conv.get("adaptive_bias", 10),
    )


def image_to_tspl_bitmap(img: Image.Image, invert: bool = False, dither: bool = False,
                          threshold: int = 160) -> Tuple[bytes, int, int]:
    """Конвертирует изображение в монохромный битовый массив для команды BITMAP.
    Возвращает (raw_bytes, width_in_bytes, height_in_dots).

    dither=True — используется для ПРОИЗВОЛЬНЫХ цветных/полутоновых картинок
    (см. render_image_bw). dither=False (по умолчанию) — для уже строго
    чёрно-белых битмапов (отрендеренный текст, полоса) — там дизеринг не нужен
    и лишняя обработка ни к чему, простой порог даёт идентичный результат
    быстрее.

    Пакуем вручную (а не через Image.tobytes у режима '1'), чтобы явно
    контролировать полярность битов и не зависеть от внутренних деталей Pillow."""
    if dither:
        bw_source = img.convert("L").convert("1")
        px = bw_source.load()
        width, height = bw_source.size
        is_black_fn = lambda x, y: px[x, y] == 0  # noqa: E731 — в режиме "1": 0=чёрный
    else:
        gray = img.convert("L")
        px = gray.load()
        width, height = gray.size
        is_black_fn = lambda x, y: px[x, y] <= threshold  # noqa: E731

    width_bytes = (width + 7) // 8
    padding_bits = width_bytes * 8 - width  # сколько "лишних" бит в конце каждой строки до границы байта (0-7)
    data = bytearray(width_bytes * height)
    for y in range(height):
        row_base = y * width_bytes
        for x in range(width):
            is_black = is_black_fn(x, y)
            if invert:
                is_black = not is_black
            if is_black:
                data[row_base + (x // 8)] |= (1 << (7 - (x % 8)))
        if invert and padding_bits:
            # БАГ, который чинит этот блок: паддинг-биты за пределами реальной ширины
            # картинки (до кратности 8) раньше всегда оставались 0 — и это верно для
            # ОБЫЧНОЙ полярности (0 = не печатать), но при invert=True "не печатать"
            # кодируется ЕДИНИЦЕЙ, а не нулём (см. полярность self.bitmap_invert в
            # шапке TsplLabel/render_image_bw — это калибровка под КОНКРЕТНЫЙ принтер,
            # у которого биты физически "перевёрнуты"). Раз мы для контента строки уже
            # инвертируем is_black выше, те же правила обязаны применяться и к области
            # ЗА пределами контента — иначе на такой полярности эти нетронутые нулевые
            # биты печатаются сплошным чёрным "хвостом" в конце каждой строки данных.
            # После разворота DIRECTION 1 (стандартно для этих принтеров, печать
            # "вверх ногами") этот хвост физически оказывается на ЛЕВОМ краю этикетки —
            # это и есть чёрная полоса у края номера/текста, о которой сообщил пользователь.
            # Проявляется только когда ширина элемента в точках НЕ кратна 8 (иначе
            # паддинга просто нет) — отсюда "то есть, то нет" в разных элементах.
            last_byte_idx = row_base + width_bytes - 1
            data[last_byte_idx] |= (1 << padding_bits) - 1
    return bytes(data), width_bytes, height


@dataclass
class TsplLabel:
    width_mm: float
    height_mm: float
    dpi: int
    gap_mm: float = 2.0
    direction: int = 1
    speed: Optional[float] = 4
    density: Optional[int] = 8
    bitmap_invert: bool = False
    offset_x_mm: float = 0.0  # сдвиг ВСЕЙ печати вправо (+) или влево (-), см. printer.offset_x_mm
    offset_y_mm: float = 0.0  # то же самое по вертикали, см. printer.offset_y_mm
    _commands: List[bytes] = field(default_factory=list)

    def _line(self, text: str) -> bytes:
        return (text + "\r\n").encode("ascii", errors="replace")

    @staticmethod
    def _fmt(n: float) -> str:
        """Форматирует мм без лишнего '.0' (SIZE 40 mm выглядит аккуратнее, чем SIZE 40.0 mm),
        но сохраняет дробную часть, если она значима (например, 2.5 mm)."""
        if float(n) == int(n):
            return str(int(n))
        return str(round(float(n), 2))

    def header(self) -> "TsplLabel":
        self._commands.append(self._line(f"SIZE {self._fmt(self.width_mm)} mm, {self._fmt(self.height_mm)} mm"))
        self._commands.append(self._line(f"GAP {self._fmt(self.gap_mm)} mm, 0 mm"))
        self._commands.append(self._line(f"DIRECTION {self.direction}"))
        if self.speed is not None:
            self._commands.append(self._line(f"SPEED {self.speed}"))
        if self.density is not None:
            self._commands.append(self._line(f"DENSITY {self.density}"))
        self._commands.append(self._line("CLS"))
        return self

    def _x_dots(self, x_mm: float) -> int:
        """Переводит X из мм в точки принтера, ПРИМЕНЯЯ общий сдвиг печати
        (printer.offset_x_mm) — так калибровка положения на носителе делается
        один раз в настройках принтера, а не переносом всех элементов в редакторе."""
        return max(0, mm_to_dots(x_mm + self.offset_x_mm, self.dpi))

    def _y_dots(self, y_mm: float) -> int:
        """Аналог _x_dots для вертикали (printer.offset_y_mm) — на некоторых принтерах/
        заправках рулона именно этот сдвиг физически двигает печать влево-вправо, а
        offset_x_mm — вверх-вниз (оси в командах печати оказываются "перепутаны"
        относительно честного превью в редакторе) — см. комментарий в config.py."""
        return max(0, mm_to_dots(y_mm + self.offset_y_mm, self.dpi))

    def add_text(self, x_mm: float, y_mm: float, content: str, font: str = "3",
                 rotation: int = 0, x_mult: int = 1, y_mult: int = 1) -> "TsplLabel":
        x = self._x_dots(x_mm)
        y = self._y_dots(y_mm)
        safe = content.replace('"', "'")
        self._commands.append(
            self._line(f'TEXT {x},{y},"{font}",{rotation},{x_mult},{y_mult},"{safe}"')
        )
        return self

    def add_bar(self, x_mm: float, y_mm: float, width_mm: float, height_mm: float) -> "TsplLabel":
        x = self._x_dots(x_mm)
        y = self._y_dots(y_mm)
        w = mm_to_dots(width_mm, self.dpi)
        h = mm_to_dots(height_mm, self.dpi)
        self._commands.append(self._line(f"BAR {x},{y},{w},{h}"))
        return self

    def add_text_autofit(self, x_mm: float, y_mm: float, width_mm: float, height_mm: float,
                          text: str, bold: bool = True) -> "TsplLabel":
        """Печатает текст, автоматически подобрав максимальный размер шрифта под рамку
        width_mm x height_mm и отцентровав его внутри неё (см. render_autofit_text_bitmap)."""
        x = self._x_dots(x_mm)
        y = self._y_dots(y_mm)
        w_dots = max(1, mm_to_dots(width_mm, self.dpi))
        h_dots = max(1, mm_to_dots(height_mm, self.dpi))
        img = render_autofit_text_bitmap(text, w_dots, h_dots, bold=bold)
        raw, width_bytes, height_dots = image_to_tspl_bitmap(img, invert=self.bitmap_invert)
        prefix = f"BITMAP {x},{y},{width_bytes},{height_dots},0,".encode("ascii")
        self._commands.append(prefix + raw + b"\r\n")
        return self

    def add_image(self, x_mm: float, y_mm: float, width_mm: float, pil_image: Image.Image,
                   image_conversion: Optional[dict] = None) -> "TsplLabel":
        """Масштабирует картинку под width_mm (сохраняя пропорции) и переводит в Ч/Б
        по настройкам image_conversion — теперь хранится В САМОМ элементе шаблона
        image (редактор этикетки), не глобально на вкладке "Принтер" (см. render_image_bw
        для описания всех методов конвертации)."""
        x = self._x_dots(x_mm)
        y = self._y_dots(y_mm)
        target_w_dots = max(1, mm_to_dots(width_mm, self.dpi))
        bw_image = _image_bw_from_conv(pil_image, target_w_dots, image_conversion)
        raw, width_bytes, height_dots = image_to_tspl_bitmap(bw_image, invert=self.bitmap_invert, dither=False)
        prefix = f"BITMAP {x},{y},{width_bytes},{height_dots},0,".encode("ascii")
        self._commands.append(prefix + raw + b"\r\n")
        return self

    def add_image_fit(self, x_mm: float, y_mm: float, width_mm: float, height_mm: float,
                       pil_image: Image.Image, image_conversion: Optional[dict] = None) -> "TsplLabel":
        """Как add_image, но вписывает картинку ЦЕЛИКОМ в прямоугольник width_mm x
        height_mm (сохраняя пропорции, по короткой стороне) и центрирует внутри него —
        аналог add_text_autofit, только для произвольного изображения. Используется
        режимом печати "screenshot" (см. engine.py): туда вписывается обрезанный кусок
        скриншота вместо перерисованного текста номера."""
        box_w_dots = max(1, mm_to_dots(width_mm, self.dpi))
        box_h_dots = max(1, mm_to_dots(height_mm, self.dpi))
        src_w, src_h = pil_image.size
        if src_w <= 0 or src_h <= 0:
            return self
        scale = min(box_w_dots / src_w, box_h_dots / src_h)
        target_w_dots = max(1, int(round(src_w * scale)))
        bw_image = _image_bw_from_conv(pil_image, target_w_dots, image_conversion)
        img_w, img_h = bw_image.size
        x = self._x_dots(x_mm) + max(0, (box_w_dots - img_w) // 2)
        y = self._y_dots(y_mm) + max(0, (box_h_dots - img_h) // 2)
        raw, width_bytes, height_dots = image_to_tspl_bitmap(bw_image, invert=self.bitmap_invert, dither=False)
        prefix = f"BITMAP {x},{y},{width_bytes},{height_dots},0,".encode("ascii")
        self._commands.append(prefix + raw + b"\r\n")
        return self

    def build(self, copies: int = 1) -> bytes:
        body = b"".join(self._commands)
        footer = self._line(f"PRINT {copies},1")
        return body + footer


def send_raw(printer_name: str, data: bytes, doc_name: str = "WB PVZ Label") -> None:
    """Отправляет сырые байты на принтер в обход графического рендеринга Windows —
    надёжнее для термопринтеров с TSPL, т.к. не зависит от корректности GDI-драйвера."""
    if win32print is None:
        raise RuntimeError("win32print недоступен — печать возможна только на Windows")
    handle = win32print.OpenPrinter(printer_name)
    try:
        job = win32print.StartDocPrinter(handle, 1, (doc_name, None, "RAW"))
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, data)
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
        return job
    finally:
        win32print.ClosePrinter(handle)


def build_label_from_template(cfg: dict, cell_number: str) -> bytes:
    """Собирает TSPL-документ по активному шаблону из конфига и распознанному номеру."""
    p = cfg["printer"]
    tmpl_name = cfg["active_template"]
    tmpl = cfg["templates"][tmpl_name]["elements"]
    log.info("Сборка этикетки (OCR): номер=%s, offset_x_mm=%s, offset_y_mm=%s, bitmap_invert=%s",
             cell_number, p.get("offset_x_mm", 0), p.get("offset_y_mm", 0), p.get("bitmap_invert", False))

    label = TsplLabel(
        width_mm=p["label_width_mm"],
        height_mm=p["label_height_mm"],
        dpi=p["dpi"],
        gap_mm=p.get("gap_mm", 2),
        direction=p.get("direction", 1),
        speed=p.get("speed"),
        density=p.get("density"),
        bitmap_invert=p.get("bitmap_invert", False),
        offset_x_mm=p.get("offset_x_mm", 0),
        offset_y_mm=p.get("offset_y_mm", 0),
    ).header()

    cn = tmpl.get("cell_number")
    if cn and cn.get("width_mm", 0) > 0 and cn.get("height_mm", 0) > 0:
        label.add_text_autofit(
            cn["x_mm"], cn["y_mm"], cn["width_mm"], cn["height_mm"],
            cell_number, bold=cn.get("bold", True),
        )

    bar = tmpl.get("bar")
    if bar and bar.get("width_mm", 0) > 0 and bar.get("height_mm", 0) > 0:
        label.add_bar(bar["x_mm"], bar["y_mm"], bar["width_mm"], bar["height_mm"])

    st = tmpl.get("static_text")
    if st and st.get("content") and st.get("width_mm", 0) > 0 and st.get("height_mm", 0) > 0:
        label.add_text_autofit(
            st["x_mm"], st["y_mm"], st["width_mm"], st["height_mm"],
            st["content"], bold=st.get("bold", False),
        )

    img = tmpl.get("image")
    if img and img.get("path"):
        try:
            label.add_image(
                img["x_mm"], img["y_mm"], img["width_mm"], _load_source_image_cached(img["path"]),
                image_conversion=img.get("image_conversion"),
            )
        except (OSError, ValueError) as e:
            log.warning("Не удалось загрузить картинку шаблона %s: %s", img["path"], e)

    return label.build(copies=1)


def render_label_preview_image(cfg: dict, elements: dict, cell_number: str) -> Image.Image:
    """Рендерит ВСЮ этикетку в растровое RGB-изображение теми же функциями, что и
    реальная печать (render_autofit_text_bitmap, render_image_bw) — используется для
    честного превью в редакторе макета, чтобы то, что видит оператор на экране,
    ТОЧНО совпадало с тем, что уйдёт на принтер (включая реальный автоподбор размера
    шрифта под номер ячейки — с любым тестовым значением, не только тем, что придёт
    от сканера).

    ВАЖНО: elements передаются СНАРУЖИ (текущее, возможно ещё не сохранённое
    состояние редактора), а не читаются из cfg — так превью отражает live-правки
    до нажатия "Сохранить шаблон". Размер/DPI этикетки при этом берутся из
    сохранённого cfg["printer"] (эти поля редактируются на отдельной вкладке).

    Полярность (bitmap_invert) сознательно ИГНОРИРУЕТСЯ здесь: это компенсация
    аппаратной особенности конкретного принтера, а не часть макета — превью должно
    показывать "чёрный текст на белом", как задумано визуально, а не сырой сигнал,
    который в итоге переворачивается принтером обратно в то же самое.

    ДОПУЩЕНИЕ: логика размещения элементов здесь продублирована из
    build_label_from_template (а не переиспользована напрямую), т.к. там строится
    поток TSPL-команд, а тут — растровое изображение. При изменении расположения
    элементов в build_label_from_template не забыть поправить и здесь."""
    p = cfg["printer"]
    dpi = p["dpi"]
    offset_x_mm = p.get("offset_x_mm", 0)
    offset_y_mm = p.get("offset_y_mm", 0)
    w_dots = max(1, mm_to_dots(p["label_width_mm"], dpi))
    h_dots = max(1, mm_to_dots(p["label_height_mm"], dpi))
    canvas = Image.new("RGB", (w_dots, h_dots), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    def x_dots(x_mm: float) -> int:
        return max(0, mm_to_dots(x_mm + offset_x_mm, dpi))

    def y_dots(y_mm: float) -> int:
        return max(0, mm_to_dots(y_mm + offset_y_mm, dpi))

    cn = elements.get("cell_number") or {}
    if cn.get("width_mm", 0) > 0 and cn.get("height_mm", 0) > 0:
        x, y = x_dots(cn["x_mm"]), y_dots(cn["y_mm"])
        w, h = mm_to_dots(cn["width_mm"], dpi), mm_to_dots(cn["height_mm"], dpi)
        img = render_autofit_text_bitmap(cell_number or "", w, h, bold=cn.get("bold", True))
        canvas.paste(img.convert("RGB"), (x, y))

    bar = elements.get("bar") or {}
    if bar.get("width_mm", 0) > 0 and bar.get("height_mm", 0) > 0:
        x, y = x_dots(bar["x_mm"]), y_dots(bar["y_mm"])
        w, h = mm_to_dots(bar["width_mm"], dpi), mm_to_dots(bar["height_mm"], dpi)
        draw.rectangle([x, y, x + w, y + h], fill=(0, 0, 0))

    st = elements.get("static_text") or {}
    if st.get("content") and st.get("width_mm", 0) > 0 and st.get("height_mm", 0) > 0:
        x, y = x_dots(st["x_mm"]), y_dots(st["y_mm"])
        w, h = mm_to_dots(st["width_mm"], dpi), mm_to_dots(st["height_mm"], dpi)
        img = render_autofit_text_bitmap(st["content"], w, h, bold=st.get("bold", False))
        canvas.paste(img.convert("RGB"), (x, y))

    img_el = elements.get("image") or {}
    if img_el.get("path"):
        try:
            x, y = x_dots(img_el["x_mm"]), y_dots(img_el["y_mm"])
            w = max(1, mm_to_dots(img_el.get("width_mm", 10), dpi))
            src_img = _load_source_image_cached(img_el["path"])
            bw_img = _image_bw_from_conv(src_img, w, img_el.get("image_conversion"))
            canvas.paste(bw_img.convert("RGB"), (x, y))
        except (OSError, ValueError) as e:
            log.warning("Не удалось загрузить картинку для превью %s: %s", img_el["path"], e)

    return canvas


def build_label_from_screenshot(cfg: dict, cropped_image: Image.Image) -> bytes:
    """Как build_label_from_template, но вместо перерисованного текста номера в
    рамку элемента cell_number вписывается уже обрезанный (autocrop_number_screenshot)
    кусок скриншота — режим печати "screenshot", см. cfg["capture"]["print_mode"]
    и engine.py."""
    p = cfg["printer"]
    tmpl_name = cfg["active_template"]
    tmpl = cfg["templates"][tmpl_name]["elements"]
    log.info("Сборка этикетки (screenshot): offset_x_mm=%s, offset_y_mm=%s, bitmap_invert=%s",
             p.get("offset_x_mm", 0), p.get("offset_y_mm", 0), p.get("bitmap_invert", False))

    label = TsplLabel(
        width_mm=p["label_width_mm"],
        height_mm=p["label_height_mm"],
        dpi=p["dpi"],
        gap_mm=p.get("gap_mm", 2),
        direction=p.get("direction", 1),
        speed=p.get("speed"),
        density=p.get("density"),
        bitmap_invert=p.get("bitmap_invert", False),
        offset_x_mm=p.get("offset_x_mm", 0),
        offset_y_mm=p.get("offset_y_mm", 0),
    ).header()

    cn = tmpl.get("cell_number")
    if cn and cn.get("width_mm", 0) > 0 and cn.get("height_mm", 0) > 0:
        label.add_image_fit(
            cn["x_mm"], cn["y_mm"], cn["width_mm"], cn["height_mm"],
            cropped_image, image_conversion=cfg["capture"].get("screenshot_conversion"),
        )

    bar = tmpl.get("bar")
    if bar and bar.get("width_mm", 0) > 0 and bar.get("height_mm", 0) > 0:
        label.add_bar(bar["x_mm"], bar["y_mm"], bar["width_mm"], bar["height_mm"])

    st = tmpl.get("static_text")
    if st and st.get("content") and st.get("width_mm", 0) > 0 and st.get("height_mm", 0) > 0:
        label.add_text_autofit(
            st["x_mm"], st["y_mm"], st["width_mm"], st["height_mm"],
            st["content"], bold=st.get("bold", False),
        )

    img = tmpl.get("image")
    if img and img.get("path"):
        try:
            label.add_image(
                img["x_mm"], img["y_mm"], img["width_mm"], _load_source_image_cached(img["path"]),
                image_conversion=img.get("image_conversion"),
            )
        except (OSError, ValueError) as e:
            log.warning("Не удалось загрузить картинку шаблона %s: %s", img["path"], e)

    return label.build(copies=1)


def print_number_screenshot(cfg: dict, cropped_image: Image.Image) -> Tuple[bool, str]:
    """Аналог print_cell_number для режима "screenshot": печатает УЖЕ обрезанный кусок
    скриншота вместо распознанного текста. Возвращает (успех, сообщение_об_ошибке)."""
    printer_name = cfg["printer"]["name"]
    status = get_printer_status(printer_name)
    if not status["online"]:
        return False, status["status_text"]
    try:
        data = build_label_from_screenshot(cfg, cropped_image)
        send_raw(printer_name, data)
        return True, ""
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка печати (режим screenshot)")
        return False, str(e)


def print_cell_number(cfg: dict, cell_number: str) -> Tuple[bool, str]:
    """Высокоуровневая функция: собрать этикетку по шаблону и напечатать.
    Возвращает (успех, сообщение_об_ошибке_или_пусто)."""
    printer_name = cfg["printer"]["name"]
    status = get_printer_status(printer_name)
    if not status["online"]:
        return False, status["status_text"]
    try:
        data = build_label_from_template(cfg, cell_number)
        send_raw(printer_name, data)
        return True, ""
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка печати")
        return False, str(e)


if __name__ == "__main__":
    # Быстрый ручной тест шага 2 из раздела 7 ТЗ: "напечатай тестовую этикетку
    # с фиксированным текстом", убедиться что печать вообще работает.
    logging.basicConfig(level=logging.INFO)
    names = list_printers()
    print("Найденные принтеры:", names)
    if names:
        target = names[0]
        label = TsplLabel(width_mm=40, height_mm=30, dpi=203).header()
        label.add_text(5, 5, "TEST 123", font="3")
        label.add_bar(2, 15, 36, 2)
        send_raw(target, label.build())
        print(f"Отправлено на {target}")
    else:
        print("Принтеры не найдены — проверьте подключение и очередь печати Windows")
