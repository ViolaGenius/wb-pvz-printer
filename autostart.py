"""
autostart.py
Включение/выключение автозапуска через реестр HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.

ДОПУЩЕНИЕ: используем ключ реестра, а не ярлык в shell:startup — оба варианта
равнозначны по ТЗ ("ярлык... или ключ реестра"), реестр выбран как более
простой для программного управления (не нужно создавать .lnk через COM).
"""

import logging
import sys

log = logging.getLogger("autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "WB_PVZ_Printer"

try:
    import winreg
except ImportError:  # не-Windows
    winreg = None


def _get_target_command() -> str:
    """Если приложение собрано PyInstaller-ом (frozen), запускаем сам exe.
    Иначе (разработка) — запускаем через тот же интерпретатор python main.py."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{sys.argv[0]}"'


def set_enabled(enabled: bool):
    if winreg is None:
        log.warning("winreg недоступен (не Windows) — автозапуск не настроен")
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _get_target_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        log.exception("Не удалось изменить настройку автозапуска")
