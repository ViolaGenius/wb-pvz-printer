# -*- coding: utf-8 -*-
"""
segmentation.py
Разбивает уже бинаризованное (Ч/Б) изображение номера ячейки на отдельные
символы по вертикальным "просадкам" — столбцам, где нет ни одного чёрного
пикселя, считаются разделителями между символами.

Используется двумя новыми улучшениями распознавания:
  1. Посимвольное распознавание как дополнительный "голос" в ансамбле OCR
     (ocr.py: recognize_with_retries) — одиночный символ tesseract обычно
     узнаёт надёжнее, чем короткую строку целиком.
  2. Самообучающиеся эталоны цифр (digit_templates.py) — эталон нужно
     сохранять именно как ОДИНОЧНУЮ цифру, а не строку целиком.

ДОПУЩЕНИЕ (подтверждено пользователем): цифры в номере ячейки не слипаются
визуально (не курсив, не наклон) и ведущих нулей не бывает — простой разрыв
по пустым столбцам достаточен, не нужен более сложный алгоритм связных
компонент (cv2.connectedComponents и т.п.).
"""

import logging
from typing import List, Tuple

from PIL import Image

log = logging.getLogger("segmentation")


def _column_has_black_pixel(bw: Image.Image, x: int) -> bool:
    px = bw.load()
    h = bw.height
    for y in range(h):
        if px[x, y] == 0:  # 0 = чёрный в режиме "L" после бинаризации
            return True
    return False


def segment_characters(bw: Image.Image, min_char_width_px: int = 3,
                        min_gap_px: int = 2) -> List[Image.Image]:
    """Возвращает список изображений отдельных символов (слева направо), уже
    обрезанных по фактической ширине каждого символа плюс небольшой отступ.

    bw — бинаризованное изображение (0=чёрный текст, 255=белый фон), как
    возвращает ocr.preprocess(). min_char_width_px отсекает случайный "мусор"
    (пылинки/артефакты дизеринга) от настоящих символов. min_gap_px — сколько
    подряд идущих пустых столбцов считать реальным разрывом между символами
    (1 px иногда даёт ложные разрывы на засечках шрифта)."""
    w, h = bw.size
    if w == 0 or h == 0:
        return []

    col_has_ink = [_column_has_black_pixel(bw, x) for x in range(w)]

    # находим непрерывные диапазоны столбцов с чернилами, разделённые
    # промежутками не короче min_gap_px
    ranges: List[Tuple[int, int]] = []
    start = None
    gap_run = 0
    for x in range(w):
        if col_has_ink[x]:
            if start is None:
                start = x
            gap_run = 0
        else:
            if start is not None:
                gap_run += 1
                if gap_run >= min_gap_px:
                    ranges.append((start, x - gap_run + 1))
                    start = None
                    gap_run = 0
    if start is not None:
        ranges.append((start, w))

    chars = []
    for x0, x1 in ranges:
        if (x1 - x0) < min_char_width_px:
            continue  # похоже на шум, а не на символ
        pad = 1
        crop = bw.crop((max(0, x0 - pad), 0, min(w, x1 + pad), h))
        chars.append(crop)
    return chars
