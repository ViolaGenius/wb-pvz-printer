# -*- coding: utf-8 -*-
"""
webview2_setup.py
Проверка и (по возможности) тихая установка Microsoft Edge WebView2 Runtime —
системного компонента, которым pywebview на Windows рисует окно настроек
(современный Chromium-движок; см. README про редизайн "Liquid Glass" —
он использует backdrop-filter/blur, чего старый движок не умеет).

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ПРОСТО ФАЙЛ В assets/:
WebView2 Runtime — не файл рядом с программой, а системный компонент со
своей регистрацией в реестре и собственным автообновлением (как сам
Microsoft Edge). Поэтому его нельзя "зашить" внутрь exe так же, как
Tesseract (см. ocr.py) — можно только (а) проверить, стоит ли он уже,
и (б) если нет, запустить его официальный маленький установщик
(Evergreen Bootstrapper, ~2 МБ), который сам docкачает и поставит нужную
версию с серверов Microsoft. Для шага (б) один раз нужен интернет на
целевом компьютере, и Windows может показать стандартный запрос UAC
(если программа запущена без прав администратора) — это поведение самой
Windows, обойти его нельзя и не нужно.

НА ПРАКТИКЕ: на подавляющем большинстве Windows 10/11 (актуальных,
получающих обновления) WebView2 Runtime уже стоит из коробки — он
устанавливается вместе с обновлениями Windows и Edge. Поэтому для
большинства пользователей ensure_installed() ничего не делает (тихий
no-op), а описанный сценарий установки — подстраховка для редких машин
без него (урезанные/устаревшие сборки Windows, серверные версии и т.п.).

БЕЗ WebView2 ПРОГРАММА НЕ ПАДАЕТ: pywebview на Windows при отсутствии
WebView2 автоматически откатывается на старый встроенный движок Internet
Explorer/Trident — окно настроек всё равно откроется, но без современной
вёрстки (блюр, скругления и т.п.) и, возможно, с визуальными огрехами.
Поэтому эта проверка НЕ считается блокирующей (см. health_check.py) —
это предупреждение, а не критичная ошибка.
"""

import logging
import os
import subprocess
import sys

log = logging.getLogger("webview2_setup")

try:
    import winreg
except ImportError:  # не Windows — модуль всё равно безопасно импортируется
    winreg = None

# GUID клиента WebView2 Runtime в реестре EdgeUpdate — официально
# задокументирован Microsoft, одинаковый на всех машинах и во всех версиях.
_WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

_REGISTRY_LOCATIONS = (
    # (hive, путь) — проверяем и per-machine (обычную и WOW6432Node для
    # 32-битных программ на 64-битной Windows), и per-user установку.
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_CLIENT_GUID),
    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_CLIENT_GUID),
    ("HKEY_CURRENT_USER", r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_CLIENT_GUID),
)


def is_installed() -> bool:
    """True, если в реестре есть версия (значение 'pv') хотя бы по одному
    из стандартных путей WebView2 Runtime — этим же способом его наличие
    определяет сам WebView2 SDK."""
    if winreg is None:
        return False
    for hive_name, path in _REGISTRY_LOCATIONS:
        hive = getattr(winreg, hive_name)
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _bundled_bootstrapper_path():
    """Путь к встроенному установщику WebView2 (assets/webview2/...), если
    он включён в сборку — build.bat кладёт его туда автоматически перед
    вызовом pyinstaller. Работает и при обычном запуске, и из onefile-exe
    (там assets/ распаковывается во временную sys._MEIPASS)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(base, "assets", "webview2", "MicrosoftEdgeWebview2Setup.exe")
    return candidate if os.path.isfile(candidate) else None


def ensure_installed(timeout_sec: int = 180) -> bool:
    """Если WebView2 Runtime не найден — пытается тихо поставить его через
    встроенный Bootstrapper. Возвращает True, если рантайм в итоге доступен
    (был уже установлен или установка прошла успешно). Никогда не бросает
    исключение наружу — падение установки не должно ронять запуск программы,
    т.к. pywebview всё равно откатится на старый движок (см. docstring выше)."""
    if is_installed():
        return True

    bootstrapper = _bundled_bootstrapper_path()
    if not bootstrapper:
        log.warning(
            "WebView2 Runtime не найден, а встроенный установщик "
            "(assets/webview2/MicrosoftEdgeWebview2Setup.exe) отсутствует в этой сборке — "
            "окно настроек откроется в устаревшем режиме совместимости. Пересоберите exe "
            "через build.bat (он готовит установщик автоматически) либо поставьте WebView2 "
            "Runtime вручную: https://developer.microsoft.com/microsoft-edge/webview2/"
        )
        return False

    log.info("WebView2 Runtime не найден, запускаю встроенный установщик (нужен интернет)...")
    try:
        # /silent /install — официальные флаги тихой установки Evergreen
        # Bootstrapper'а. Если программа запущена без прав администратора,
        # Windows может показать стандартный запрос UAC — это поведение
        # самой ОС при установке системного компонента, а не нашей программы.
        result = subprocess.run(
            [bootstrapper, "/silent", "/install"],
            timeout=timeout_sec,
            capture_output=True,
        )
        log.info("Установщик WebView2 завершился с кодом %s", result.returncode)
    except Exception:  # noqa: BLE001 — установка не должна ронять запуск программы
        log.exception("Не удалось запустить встроенный установщик WebView2")
        return False

    installed_now = is_installed()
    if not installed_now:
        log.warning(
            "После попытки установки WebView2 Runtime всё ещё не обнаружен "
            "(нет интернета на этом ПК, установка отменена в UAC, или нужно время "
            "на завершение фонового процесса Edge Update) — окно настроек откроется "
            "в устаревшем режиме совместимости."
        )
    return installed_now
