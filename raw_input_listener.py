# -*- coding: utf-8 -*-
"""
raw_input_listener.py
Точное определение ФИЗИЧЕСКОГО USB-HID устройства через Windows Raw Input API
(WM_INPUT) — в отличие от scanner_listener.py (эвристика по скорости печати:
если интервалы между символами укладываются в max_interkey_ms — считаем, что
это сканер), здесь каждое нажатие клавиши приходит с hDevice — хендлом
КОНКРЕТНОГО физического устройства. Сканер один раз "привязывается" через
калибровку на вкладке "Сканер" (сканируете показанный там QR-код нужным
сканером), а дальше программа реагирует ТОЛЬКО на ввод с этого устройства —
обычная клавиатура и любые другие HID-устройства полностью игнорируются, даже
если человек печатает очень быстро. Это должно убрать оба типа несрабатываний
эвристики: пропуски (сканер настроен нестандартно/шлёт код не в то временное
окно) и ложные срабатывания (человек случайно печатает достаточно быстро).

ЧЕСТНОЕ ПРЕДУПРЕЖДЕНИЕ: это низкоуровневый Win32 API (RegisterRawInputDevices +
скрытое окно со своим message loop в отдельном потоке, через ctypes и pywin32).
Структуры и сигнатуры сверены с документацией Microsoft, но проверить вживую на
реальной Windows-машине с реальным сканером здесь возможности не было — если
при первом запуске детектор не заработает, смотрите лог логгера "raw_input",
там подробно залогированы все точки, где что-то может пойти не так
(RegisterRawInputDevices, GetRawInputData, GetRawInputDeviceInfoW).

Если pywin32/ctypes.windll недоступны (не Windows) — модуль просто не даёт
DeviceScannerListener ничего делать (start() тихо ничего не запускает, лог
предупреждения), engine.py в этом случае продолжает работать через обычный
ScannerListener (эвристика по времени), как было раньше.
"""

import ctypes
import logging
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("raw_input")

try:
    import win32api
    import win32con
    import win32gui
    _WINDOWS_OK = True
except ImportError:  # для разработки/тестов вне Windows
    win32api = win32con = win32gui = None
    _WINDOWS_OK = False

user32 = ctypes.windll.user32 if _WINDOWS_OK else None  # noqa: SIM108

# ---- константы Win32 Raw Input API (см. MSDN: "Raw Input", "WM_INPUT") ----
WM_INPUT = 0x00FF
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x00000100  # получать ввод, даже когда наше скрытое окно не в фокусе —
                               # обязательно, т.к. сканируют в другое (активное) окно/поле
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
RI_KEY_BREAK = 0x01  # флаг в RAWKEYBOARD.Flags: событие "отпускание клавиши"

VK_RETURN = 0x0D
# Виртуальные коды клавиш -> символ. Сканеры почти всегда шлют цифры/буквы/
# Enter — этого достаточно и для номеров ячеек (они цифровые), и для
# калибровочного QR-кода (там важен сам факт скана и устройство, не текст).
_VK_CHAR_MAP: dict = {}
for _i in range(0x30, 0x3A):  # '0'-'9'
    _VK_CHAR_MAP[_i] = chr(_i)
for _i in range(0x41, 0x5B):  # 'A'-'Z'
    _VK_CHAR_MAP[_i] = chr(_i)
_MAX_BUFFER_CHARS = 64  # защита от неограниченного роста буфера, если Enter не пришёл


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ("MakeCode", wintypes.USHORT),
        ("Flags", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("VKey", wintypes.USHORT),
        ("Message", wintypes.UINT),
        ("ExtraInformation", wintypes.ULONG),
    ]


class RAWINPUT(ctypes.Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("keyboard", RAWKEYBOARD),
        # union с мышью/HID-данными тут не нужен — фильтруем по header.dwType и
        # используем только клавиатурную часть; буфер под GetRawInputData всегда
        # берём размером из самого события (size.value), а не sizeof(RAWINPUT)
    ]


def _get_device_path(hDevice) -> Optional[str]:
    """Уникальный путь устройства вида \\\\?\\HID#VID_xxxx&PID_xxxx#...&{guid} —
    стабильный идентификатор конкретного физического HID-устройства/порта."""
    if user32 is None:
        return None
    size = wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(hDevice, RIDI_DEVICENAME, None, ctypes.byref(size))
    if size.value == 0:
        return None
    buf = ctypes.create_unicode_buffer(size.value)
    res = user32.GetRawInputDeviceInfoW(hDevice, RIDI_DEVICENAME, buf, ctypes.byref(size))
    if res <= 0:
        return None
    return buf.value


def device_friendly_name(device_path: str) -> str:
    """Best-effort человекочитаемое имя по VID/PID из пути устройства. Не залезаем
    в реестр/WMI за настоящим названием модели (лишний риск ошибок без возможности
    проверить) — VID/PID уже достаточно, чтобы отличить один сканер от другого и
    от клавиатуры на глаз. Если распарсить не удалось — возвращаем сам путь, он
    тоже уникален и годится для отображения."""
    if not device_path:
        return "неизвестное устройство"
    m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", device_path)
    if m:
        return f"USB-устройство VID:{m.group(1)} PID:{m.group(2)}"
    return device_path


@dataclass
class ScanEvent:
    """Тот же формат, что и scanner_listener.ScanEvent — engine.py обрабатывает
    сканы одинаково независимо от того, какой из двух листенеров их прислал."""
    code: str
    timestamp: float
    is_duplicate: bool


class DeviceScannerListener:
    """Слушатель с тем же публичным интерфейсом, что и ScannerListener
    (start/stop/pause/resume/is_paused + on_scan-колбэк с ScanEvent), но
    фильтрующий ввод по конкретному физическому устройству вместо эвристики по
    скорости печати. Плюс отдельный режим калибровки (begin_detect/
    get_detected_device) для привязки устройства через сканирование QR-кода на
    вкладке "Сканер" в UI."""

    def __init__(self, cfg_provider: Callable[[], dict], on_scan: Callable[[ScanEvent], None]):
        self._cfg_provider = cfg_provider
        self._on_scan = on_scan

        self._paused = threading.Event()
        self._lock = threading.Lock()

        self._bound_device_path: Optional[str] = None
        self._buffer: list = []  # [(char, ts)] — только для ПРИВЯЗАННОГО устройства

        self._last_codes: dict = {}  # code -> timestamp последнего срабатывания (debounce дублей)

        self._detect_mode = False
        self._detect_buffers: dict = {}  # device_path -> [char, ...], только во время калибровки
        self._detected_device_path: Optional[str] = None
        self._detected_device_seen_at: Optional[float] = None

        self._hwnd = None
        self._thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()

    # ---------- публичный интерфейс, зеркалящий scanner_listener.ScannerListener ----------

    def start(self):
        if not _WINDOWS_OK:
            log.warning(
                "pywin32 недоступен — привязка сканера к конкретному устройству не "
                "работает (используется обычная эвристика по времени между символами)"
            )
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_message_loop, daemon=True, name="raw-input-listener")
        self._thread.start()

    def stop(self):
        self._stop_flag.set()
        if self._hwnd and win32gui is not None:
            try:
                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось корректно закрыть окно детектора устройства")
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    # ---------- привязка устройства ----------

    def set_bound_device(self, device_path: Optional[str]):
        with self._lock:
            if device_path != self._bound_device_path:
                self._buffer = []
            self._bound_device_path = device_path

    @property
    def bound_device_path(self) -> Optional[str]:
        return self._bound_device_path

    def begin_detect(self):
        """Включает режим калибровки — вызывается при открытии диалога на вкладке
        "Сканер". Следующий ПОЛНЫЙ скан (код нужной минимальной длины + Enter) с
        любого устройства будет зафиксирован; get_detected_device() отдаёт
        результат поллингом из фронтенда."""
        with self._lock:
            self._detect_mode = True
            self._detect_buffers = {}
            self._detected_device_path = None
            self._detected_device_seen_at = None

    def cancel_detect(self):
        with self._lock:
            self._detect_mode = False
            self._detect_buffers = {}

    def get_detected_device(self) -> Optional[dict]:
        """Не блокирует — вызывается фронтендом поллингом раз в ~500мс, пока открыт
        диалог калибровки. None, пока подходящего скана не было."""
        with self._lock:
            if self._detected_device_path is None:
                return None
            return {
                "device_path": self._detected_device_path,
                "device_name": device_friendly_name(self._detected_device_path),
                "detected_at": self._detected_device_seen_at,
            }

    # ---------- внутреннее: скрытое окно + message loop (свой поток) ----------

    def _run_message_loop(self):
        try:
            wc = win32gui.WNDCLASS()
            wc.lpfnWndProc = self._wnd_proc
            wc.lpszClassName = "WbPvzRawInputListener"
            wc.hInstance = win32api.GetModuleHandle(None)
            try:
                class_atom = win32gui.RegisterClass(wc)
            except Exception:  # noqa: BLE001 — класс мог остаться зарегистрированным с прошлого запуска
                class_atom = wc.lpszClassName

            self._hwnd = win32gui.CreateWindow(
                class_atom, "WbPvzRawInputListener", 0, 0, 0, 0, 0, 0, 0,
                wc.hInstance, None,
            )

            rid = RAWINPUTDEVICE()
            rid.usUsagePage = 0x01  # Generic Desktop Controls
            rid.usUsage = 0x06  # Keyboard
            rid.dwFlags = RIDEV_INPUTSINK
            rid.hwndTarget = self._hwnd
            if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
                log.error(
                    "RegisterRawInputDevices не удался (код ошибки %s) — детектор "
                    "устройства сканера не будет получать ввод", ctypes.GetLastError()
                )
                return

            log.info("Детектор устройства сканера (Raw Input) запущен")
            win32gui.PumpMessages()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в message loop детектора устройства сканера")
        finally:
            self._hwnd = None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            try:
                self._handle_raw_input(lparam)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработки WM_INPUT")
            return 0
        if msg == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _handle_raw_input(self, lparam):
        size = wintypes.UINT(0)
        user32.GetRawInputData(lparam, RID_INPUT, None, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        if size.value == 0:
            return
        buf = ctypes.create_string_buffer(size.value)
        got = user32.GetRawInputData(lparam, RID_INPUT, buf, ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        if got != size.value:
            return
        raw = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
        if raw.header.dwType != RIM_TYPEKEYBOARD:
            return

        kb = raw.keyboard
        if kb.Flags & RI_KEY_BREAK:
            return  # интересует только "нажатие", не "отпускание"

        device_path = _get_device_path(raw.header.hDevice)
        if not device_path:
            return
        now = time.time()
        cfg = self._cfg_provider()
        min_len = cfg.get("min_code_length", 3)

        with self._lock:
            if self._detect_mode:
                self._feed_detect_buffer(device_path, kb.VKey, now, min_len)
                return  # в режиме калибровки обычную логику скана/печати не выполняем

            if self._paused.is_set():
                return
            if self._bound_device_path is None or device_path != self._bound_device_path:
                return  # чужое устройство (например, обычная клавиатура) — полностью игнорируем

        self._feed_operating_buffer(kb.VKey, now, min_len, cfg)

    def _feed_detect_buffer(self, device_path: str, vk: int, now: float, min_len: int):
        """Вызывается уже под self._lock. Буферизуем ПО КАЖДОМУ устройству отдельно —
        так калибровка не перепутает сканер с клавиатурой, если человек случайно
        нажмёт что-то во время ожидания скана: у сканера свой буфер, у клавиатуры
        свой, "детектируется" только тот, что первым наберёт полный код + Enter."""
        if vk == VK_RETURN:
            buf = self._detect_buffers.pop(device_path, [])
            if len(buf) >= min_len:
                self._detected_device_path = device_path
                self._detected_device_seen_at = now
                self._detect_mode = False  # первого валидного скана достаточно
            return
        char = _VK_CHAR_MAP.get(vk)
        if char is None:
            return
        buf = self._detect_buffers.setdefault(device_path, [])
        buf.append(char)
        if len(buf) > _MAX_BUFFER_CHARS:
            buf.clear()

    def _feed_operating_buffer(self, vk: int, now: float, min_len: int, cfg: dict):
        debounce = cfg.get("duplicate_debounce_ms", 1500) / 1000.0

        if vk == VK_RETURN:
            with self._lock:
                buf = self._buffer
                self._buffer = []
            if len(buf) < min_len:
                return
            code = "".join(buf)
            with self._lock:
                last_time = self._last_codes.get(code)
                is_duplicate = last_time is not None and (now - last_time) < debounce
                self._last_codes[code] = now
            if is_duplicate:
                log.info("Дубль отклонён (debounce): %s", code)
                return  # тот же критерий, что и в scanner_listener.py — см. его докстринг
            log.info("Обнаружено сканирование (по привязанному устройству): %s", code)
            event = ScanEvent(code=code, timestamp=now, is_duplicate=False)
            try:
                self._on_scan(event)
            except Exception:  # noqa: BLE001 — ошибка обработчика не должна ронять message loop
                log.exception("Ошибка в обработчике сканирования")
            return

        char = _VK_CHAR_MAP.get(vk)
        if char is None:
            return  # служебная клавиша — не часть кода
        with self._lock:
            self._buffer.append(char)
            if len(self._buffer) > _MAX_BUFFER_CHARS:
                self._buffer = []
