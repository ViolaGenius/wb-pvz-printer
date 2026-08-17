"""
screen_capture.py
Поиск окна "Мой ПВЗ" и захват заданной области его клиентской части.

ДОПУЩЕНИЕ: заголовок окна ищем по вхождению подстроки (не по точному совпадению),
т.к. в некоторых приложениях в заголовок дополнительно попадает название текущей
вкладки/версия и т.п. Если это создаёт проблемы (находится не то окно) —
включите точное совпадение через capture.exact_title_match в конфиге.

Координаты области калибровки хранятся ОТНОСИТЕЛЬНО клиентской области окна
(через ClientToScreen), а не относительно всего экрана — так область остаётся
верной, даже если пользователь подвинул окно "Мой ПВЗ" после калибровки.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from PIL import Image
import mss

log = logging.getLogger("screen_capture")

try:
    import win32gui
    import win32process
except ImportError:  # для разработки/тестов вне Windows
    win32gui = None
    win32process = None


@dataclass
class WindowState:
    found: bool
    visible: bool = False
    minimized: bool = False
    hwnd: Optional[int] = None
    client_left: int = 0
    client_top: int = 0
    client_width: int = 0
    client_height: int = 0


def find_window(title_substring: str, exact: bool = False) -> Optional[int]:
    if win32gui is None:
        return None

    result = {"hwnd": None}

    def _enum_handler(hwnd, _):
        if result["hwnd"] is not None:
            return
        if not win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
            # неминимизированное и невидимое окно почти наверняка не то, что нужно,
            # но минимизированные окна тоже видимыми не считаются в Windows —
            # поэтому явную проверку видимости здесь не делаем строго
            pass
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        matched = (title == title_substring) if exact else (title_substring.lower() in title.lower())
        if matched:
            result["hwnd"] = hwnd

    win32gui.EnumWindows(_enum_handler, None)
    return result["hwnd"]


def get_window_state(title_substring: str, exact: bool = False) -> WindowState:
    hwnd = find_window(title_substring, exact=exact)
    if hwnd is None:
        return WindowState(found=False)

    minimized = bool(win32gui.IsIconic(hwnd))
    visible = bool(win32gui.IsWindowVisible(hwnd)) and not minimized

    if minimized:
        return WindowState(found=True, visible=False, minimized=True, hwnd=hwnd)

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
    width = right - left
    height = bottom - top

    return WindowState(
        found=True,
        visible=visible,
        minimized=False,
        hwnd=hwnd,
        client_left=screen_left,
        client_top=screen_top,
        client_width=width,
        client_height=height,
    )


def capture_screen_region(left: int, top: int, width: int, height: int) -> Optional[Image.Image]:
    if width <= 0 or height <= 0:
        return None
    with mss.mss() as sct:
        monitor = {"left": left, "top": top, "width": width, "height": height}
        shot = sct.grab(monitor)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def capture_full_window(title_substring: str, exact: bool = False):
    """Для калибровки: возвращает (PIL.Image всего клиентского окна, WindowState)
    или (None, WindowState) если окно не найдено/недоступно."""
    state = get_window_state(title_substring, exact=exact)
    if not state.found or not state.visible:
        return None, state
    img = capture_screen_region(state.client_left, state.client_top, state.client_width, state.client_height)
    return img, state


def capture_calibrated_region(cfg_capture: dict) -> "tuple[Optional[Image.Image], WindowState]":
    """Захват области распознавания по сохранённым в конфиге относительным координатам."""
    state = get_window_state(cfg_capture["window_title"], exact=cfg_capture.get("exact_title_match", False))
    if not state.found or not state.visible:
        return None, state
    rel = cfg_capture["region_relative"]
    left = state.client_left + rel["x"]
    top = state.client_top + rel["y"]
    img = capture_screen_region(left, top, rel["width"], rel["height"])
    return img, state


def capture_calibrated_region_stable(
    cfg_capture: dict, max_wait_ms: int = 150, check_interval_ms: int = 40,
) -> "tuple[Optional[Image.Image], WindowState]":
    """То же самое, что capture_calibrated_region, но дожидается, пока картинка в
    области перестанет меняться между двумя последовательными захватами (до
    max_wait_ms), прежде чем отдать результат для OCR.

    ЗАЧЕМ: если "Мой ПВЗ" в момент захвата ещё дорисовывает номер ячейки (анимация
    появления, обновление после сканирования штрихкода) — можно случайно захватить
    "промежуточный" кадр (частично отрисованный текст, наложение старого/нового
    значения), из-за чего OCR даст СЛУЧАЙНУЮ ошибку, не связанную с качеством
    самого распознавания. Эта функция снижает риск такого промаха почти бесплатно
    по времени: в типичном случае (кадр уже стабилен) не добавляет задержки вовсе,
    а в худшем — не более max_wait_ms."""
    img, state = capture_calibrated_region(cfg_capture)
    if img is None:
        return img, state
    prev_bytes = img.tobytes()
    deadline = time.time() + max_wait_ms / 1000.0
    while time.time() < deadline:
        time.sleep(check_interval_ms / 1000.0)
        img2, state2 = capture_calibrated_region(cfg_capture)
        if img2 is None:
            return img2, state2
        cur_bytes = img2.tobytes()
        if cur_bytes == prev_bytes:
            return img2, state2
        prev_bytes = cur_bytes
        img, state = img2, state2
    # не стабилизировалось за отведённое время — отдаём последний захват, не блокируем печать бесконечно
    return img, state


def capture_pixel_color(state: WindowState, x: int, y: int) -> "Optional[tuple[int, int, int]]":
    """Возвращает RGB-цвет пикселя по координатам, ОТНОСИТЕЛЬНЫМ клиентской области
    окна (та же система координат, что и region_relative). Используется цветовыми
    точками-триггерами."""
    if not state.found or not state.visible:
        return None
    img = capture_screen_region(state.client_left + x, state.client_top + y, 1, 1)
    if img is None:
        return None
    return img.convert("RGB").getpixel((0, 0))


def countdown_then_capture_window(title_substring: str, seconds: int = 4, exact: bool = False):
    """Для кнопки «Откалибровать»: ждёт seconds секунд (даёт оператору время
    переключиться на окно «Мой ПВЗ»), затем делает скриншот окна целиком."""
    for remaining in range(seconds, 0, -1):
        log.info("Калибровка: скриншот через %s сек...", remaining)
        time.sleep(1)
    return capture_full_window(title_substring, exact=exact)
