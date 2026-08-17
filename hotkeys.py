# -*- coding: utf-8 -*-
"""
hotkeys.py
Глобальные горячие клавиши — доп. функция по фидбэку пользователя, чтобы
пауза/повтор печати/открытие настроек были доступны не только через трей.

Использует ту же библиотеку `keyboard`, что и scanner_listener.py. Библиотека
поддерживает регистрацию нескольких независимых hotkey одновременно с активным
keyboard.hook() — конфликтов между детектором сканера и хоткеями нет, это
разные подписки на один и тот же поток событий ОС.

ДОПУЩЕНИЕ: если пользователь пропишет для сканирования комбинацию, которая
физически совпадает с последовательностью символов, которые может прислать
сканер (например, сканер шлёт "ctrl" — что для HID-сканеров нештатно), это
теоретически может помешать сработать хоткею. Для обычных USB-HID сканеров,
эмулирующих цифры+Enter, такого пересечения не бывает.
"""

import logging
from typing import Callable

log = logging.getLogger("hotkeys")

try:
    import keyboard as kb
except ImportError:  # для разработки/тестов вне Windows
    kb = None


class HotkeyManager:
    def __init__(self):
        self._registered: "list[str]" = []

    def register(self, combo: str, callback: Callable[[], None]) -> bool:
        """Регистрирует одну горячую клавишу. Возвращает False, если комбинация
        некорректна или уже занята другой программой — не роняем приложение."""
        if kb is None or not combo:
            return False
        try:
            kb.add_hotkey(combo, callback)
            self._registered.append(combo)
            return True
        except Exception:  # noqa: BLE001 — некорректный синтаксис комбинации от пользователя
            log.exception("Не удалось зарегистрировать горячую клавишу %r", combo)
            return False

    def unregister_all(self):
        if kb is None:
            return
        for combo in self._registered:
            try:
                kb.remove_hotkey(combo)
            except (KeyError, ValueError):
                pass
        self._registered.clear()

    def apply_from_config(self, app_cfg: dict, on_pause: Callable[[], None],
                           on_repeat: Callable[[], None], on_open_settings: Callable[[], None]):
        """Перечитывает конфиг и переустанавливает все хоткеи (вызывается при
        старте и при сохранении настроек, если пользователь поменял комбинации)."""
        self.unregister_all()
        if not app_cfg.get("hotkeys_enabled", True):
            return
        self.register(app_cfg.get("hotkey_pause", "ctrl+alt+p"), on_pause)
        self.register(app_cfg.get("hotkey_repeat_print", "ctrl+alt+r"), on_repeat)
        self.register(app_cfg.get("hotkey_open_settings", "ctrl+alt+o"), on_open_settings)
