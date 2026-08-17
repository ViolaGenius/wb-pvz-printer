"""
scanner_listener.py
Глобальный перехват клавиатуры для отличения "сканирования штрихкода" от
обычного ручного набора текста человеком.

ПРИНЦИП (реализован по рекомендованному в ТЗ стеку — библиотека `keyboard`):
  - копим введённые символы вместе с временными метками;
  - если интервал между соседними символами меньше max_interkey_ms — считаем,
    что это может быть сканер;
  - серия завершается Enter'ом; если длина серии >= min_code_length И все
    интервалы внутри серии были "быстрыми" — событие считается сканированием;
  - защита от дублей: если тот же код пришёл повторно раньше duplicate_debounce_ms
    после предыдущего срабатывания — новое срабатывание игнорируется, но именно
    как "дубль по таймингу", а не "дубль по содержимому" — см. критерий приёмки №4
    в ТЗ: два РАЗНЫХ скана с одинаковым результатом должны печататься оба раза,
    поэтому debounce считается от времени последнего успешного скана ЭТОГО кода,
    а не запоминается "навсегда".

РИСК И АЛЬТЕРНАТИВА (см. также README):
Эвристика по таймингу — самый простой вариант и он же рекомендован в ТЗ, но у
неё есть два теоретических слабых места:
  1. очень быстро печатающий человек на короткой последовательности цифр
     теоретически может быть принят за сканер (маловероятно для 3+ цифр подряд
     без опечаток, но не исключено);
  2. если сканер настроен слать штрихкод БЕЗ завершающего Enter — детектор
     его не поймает (нужно настраивать сканер на добавление Enter/CR как
     суффикса — это стандартная настройка для 99% USB-HID сканеров).

Более надёжная альтернатива — Raw Input API (WM_INPUT): регистрировать
устройства через RegisterRawInputDevices и в цикле сообщений скрытого окна
различать ввод по device handle, т.е. буфер клавиш от сканера физически
отделён от буфера клавиш обычной клавиатуры, и никакая скорость печати
человека не даст ложного срабатывания. Минусы: сложнее реализация (нужен
Win32 message loop через ctypes/pywin32), сложнее отладка, требует, чтобы
сканер и клавиатура определялись как разные HID-устройства (обычно так и
есть). Оставлено как возможное улучшение "фазы 2", не реализовано в этой
версии, чтобы уложиться в рекомендованный ТЗ стек.
"""

import logging
import time
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("scanner_listener")

try:
    import keyboard as kb
except ImportError:  # для разработки/тестов вне Windows
    kb = None


@dataclass
class ScanEvent:
    code: str
    timestamp: float
    is_duplicate: bool


# Клавиши, которые не являются символами кода (модификаторы и т.п.) — игнорируем их
_IGNORED_KEYS = {
    "shift", "ctrl", "alt", "left shift", "right shift", "left ctrl", "right ctrl",
    "left alt", "right alt", "caps lock", "tab", "windows", "menu",
}


class ScannerListener:
    def __init__(self, cfg_provider: Callable[[], dict], on_scan: Callable[[ScanEvent], None]):
        """cfg_provider — функция без аргументов, возвращающая актуальный
        cfg['scanner_detection'] (читаем настройки "живьём", чтобы изменения
        в UI применялись без перезапуска)."""
        self._cfg_provider = cfg_provider
        self._on_scan = on_scan
        self._buffer = []  # список (char, timestamp)
        self._last_key_time = None
        self._last_codes = {}  # code -> timestamp последнего срабатывания
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._hook_handle = None

    def start(self):
        if kb is None:
            log.error("Библиотека 'keyboard' не установлена — детектор сканера не запущен")
            return
        self._hook_handle = kb.hook(self._on_key_event, suppress=False)
        log.info("Детектор сканера запущен")

    def stop(self):
        if kb is not None and self._hook_handle is not None:
            kb.unhook(self._hook_handle)
            self._hook_handle = None
        log.info("Детектор сканера остановлен")

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def _on_key_event(self, event):
        if event.event_type != "down":
            return
        if self._paused.is_set():
            return

        name = event.name
        now = time.time()

        if name == "enter":
            self._finalize_buffer(now)
            return

        if name in _IGNORED_KEYS or name is None:
            return

        char = name if len(name) == 1 else ""
        if not char:
            # non-printable служебные клавиши (esc, f1 и т.п.) прерывают серию —
            # это точно не может быть частью штрихкода
            self._buffer.clear()
            self._last_key_time = None
            return

        cfg = self._cfg_provider()
        max_gap = cfg.get("max_interkey_ms", 40) / 1000.0

        with self._lock:
            if self._last_key_time is not None and (now - self._last_key_time) > max_gap:
                # слишком большая пауза — начинаем новую серию с этого символа
                self._buffer = [(char, now)]
            else:
                self._buffer.append((char, now))
            self._last_key_time = now

    def _finalize_buffer(self, now: float):
        cfg = self._cfg_provider()
        min_len = cfg.get("min_code_length", 3)
        max_gap = cfg.get("max_interkey_ms", 40) / 1000.0
        debounce = cfg.get("duplicate_debounce_ms", 1500) / 1000.0

        with self._lock:
            buf = self._buffer
            self._buffer = []
            self._last_key_time = None

        if len(buf) < min_len:
            return

        # проверяем, что ВСЕ интервалы внутри серии были быстрыми (похоже на сканер,
        # а не на человека, который в целом печатал быстро, но с естественными вариациями)
        for i in range(1, len(buf)):
            gap = buf[i][1] - buf[i - 1][1]
            if gap > max_gap:
                log.debug("Серия отклонена: интервал %.3fs превышает порог %.3fs", gap, max_gap)
                return

        code = "".join(ch for ch, _ in buf)

        last_time = self._last_codes.get(code)
        is_duplicate = last_time is not None and (now - last_time) < debounce
        self._last_codes[code] = now

        event = ScanEvent(code=code, timestamp=now, is_duplicate=is_duplicate)
        if is_duplicate:
            log.info("Дубль отклонён (debounce): %s", code)
            return  # критерий приёмки №3: дубль быстрее debounce не печатается

        log.info("Обнаружено сканирование: %s", code)
        try:
            self._on_scan(event)
        except Exception:  # noqa: BLE001 — ошибка обработчика не должна ронять хук
            log.exception("Ошибка в обработчике сканирования")
