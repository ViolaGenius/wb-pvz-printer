"""
tray.py
Иконка в системном трее с цветовой индикацией статуса и контекстным меню.

Цвета статуса (п.3.6 ТЗ):
  ready   — серый/зелёный: готов, ожидает сканирования
  busy    — жёлтый: идёт распознавание/печать
  error   — красный: принтер не найден / окно не найдено / OCR не распознал
  paused  — синий: пауза/тихий режим (добавлено дополнительно для наглядности,
            в ТЗ явно не указан отдельный цвет для паузы, но без него оператор
            не отличит "пауза" от "готов", что важно на практике)
"""

import logging
import threading
from typing import Callable, Optional

from PIL import Image, ImageDraw

log = logging.getLogger("tray")

try:
    import pystray
except ImportError:  # для разработки/тестов вне Windows
    pystray = None

STATUS_COLORS = {
    "ready": (90, 200, 90),
    "busy": (240, 200, 40),
    "error": (220, 50, 50),
    "paused": (90, 140, 220),
}

WB_ACCENT = (203, 17, 171)  # фирменный фиолетовый WB, используется в акценте иконки


def _make_icon_image(status: str) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # внешний фиолетовый круг (бренд), внутренний — цвет статуса
    draw.ellipse((2, 2, size - 2, size - 2), fill=WB_ACCENT)
    inner = 16
    draw.ellipse((inner, inner, size - inner, size - inner), fill=STATUS_COLORS.get(status, (150, 150, 150)))
    return img


class TrayApp:
    def __init__(
        self,
        on_open_settings: Callable[[], None],
        on_toggle_pause: Callable[[], bool],
        on_repeat_last: Callable[[], None],
        on_exit: Callable[[], None],
    ):
        self._on_open_settings = on_open_settings
        self._on_toggle_pause = on_toggle_pause
        self._on_repeat_last = on_repeat_last
        self._on_exit = on_exit
        self._icon: Optional["pystray.Icon"] = None
        self._status = "ready"
        self._status_text = "Готов"

    def _menu(self):
        paused_label = "Возобновить" if self._status == "paused" else "Пауза"
        return pystray.Menu(
            pystray.MenuItem("Открыть настройки", lambda: self._on_open_settings()),
            pystray.MenuItem(paused_label, lambda: self._handle_toggle_pause()),
            pystray.MenuItem("Повторить последнюю печать", lambda: self._on_repeat_last()),
            pystray.MenuItem(lambda item: self._status_text, None, enabled=False),
            pystray.MenuItem("Выход", lambda: self._handle_exit()),
        )

    def _handle_toggle_pause(self):
        is_now_paused = self._on_toggle_pause()
        self.set_status("paused" if is_now_paused else "ready", "Пауза" if is_now_paused else "Готов")

    def _handle_exit(self):
        self._on_exit()
        if self._icon:
            self._icon.stop()

    def build(self):
        if pystray is None:
            log.error("pystray не установлен — иконка в трее недоступна")
            return None
        self._icon = pystray.Icon(
            "wb_pvz_printer",
            icon=_make_icon_image(self._status),
            title=f"WB ПВЗ Печать этикеток — {self._status_text}",
            menu=self._menu(),
        )
        return self._icon

    def set_status(self, status: str, text: str):
        """Потокобезопасно (pystray сам маршалит обновления во внутренний поток)."""
        self._status = status
        self._status_text = text
        if self._icon is not None:
            self._icon.icon = _make_icon_image(status)
            self._icon.title = f"WB ПВЗ Печать этикеток — {text}"
            self._icon.menu = self._menu()

    def run(self):
        icon = self.build()
        if icon is not None:
            icon.run()  # блокирующий вызов — запускать в отдельном потоке из main.py

    def run_in_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="tray-thread")
        t.start()
        return t
