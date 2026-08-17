# -*- coding: utf-8 -*-
"""
health_check.py
"Мастер первого запуска" — на самом деле не отдельный мастер-визард, а набор
проверок окружения, которые выполняются при каждом старте программы и видны
в веб-UI (баннер/модалка). Решение НЕ городить отдельное окно pywebview для
визарда: окно настроек в этом приложении по архитектуре одно, создаётся один
раз и живёт всю сессию (см. main.py) — плодить второе только ради разового
экрана проверок усложнило бы код больше, чем экран того стоит. Вместо этого
результаты проверок доступны через /api/health-check, и:
  - если есть КРИТИЧНАЯ проблема (Tesseract не найден, хук клавиатуры
    недоступен, pywebview не установлен) — окно настроек открывается СРАЗУ
    при старте (а не скрытым, как обычно) с блокирующей модалкой поверх всего
    интерфейса, которую нельзя закрыть, пока проблема не решена;
  - если есть только предупреждения (принтер не выбран, область не
    откалибрована) — они показываются ненавязчивым баннером на дашборде,
    который можно закрыть, работа с программой не блокируется.
"""

import logging
from typing import List, TypedDict

log = logging.getLogger("health_check")


class HealthCheckResult(TypedDict):
    id: str
    title: str
    status: str  # "ok" | "warning" | "error"
    message: str
    blocking: bool  # True — считается критичной проблемой (блокирует, см. модуль docstring)


def _check_tesseract() -> HealthCheckResult:
    try:
        import pytesseract
        version = pytesseract.get_tesseract_version()
        return {"id": "tesseract", "title": "Tesseract OCR", "status": "ok",
                "message": f"Найден, версия {version}", "blocking": False}
    except Exception as e:  # noqa: BLE001 — pytesseract кидает разные типы ошибок в зависимости от причины
        return {
            "id": "tesseract", "title": "Tesseract OCR", "status": "error",
            "message": (
                "Tesseract не найден. Без него не будет работать распознавание номера ячейки. "
                "Если это портативная сборка (WB_PVZ_Printer.exe от build.bat) — значит при сборке "
                "не был подготовлен assets/tesseract (см. README, раздел «Сборка в портативный exe»); "
                "пересоберите exe после того, как assets/tesseract/tesseract.exe появится в проекте. "
                "Если запускаете из исходников — установите Tesseract с "
                "https://github.com/UB-Mannheim/tesseract/wiki и добавьте путь в PATH. "
                "Подробности: " + str(e)
            ),
            "blocking": True,
        }


def _check_keyboard_hook() -> HealthCheckResult:
    try:
        import keyboard

        def _noop():
            pass

        # маловероятная комбинация — используем только чтобы проверить, что
        # регистрация глобального хука в принципе разрешена ОС/антивирусом
        test_combo = "ctrl+alt+shift+f24"
        keyboard.add_hotkey(test_combo, _noop)
        keyboard.remove_hotkey(test_combo)
        return {"id": "keyboard_hook", "title": "Глобальный хук клавиатуры (детектор сканера)",
                "status": "ok", "message": "Доступен", "blocking": False}
    except Exception as e:  # noqa: BLE001
        return {
            "id": "keyboard_hook", "title": "Глобальный хук клавиатуры (детектор сканера)",
            "status": "error",
            "message": (
                "Не удалось зарегистрировать глобальный хук клавиатуры — без него программа не "
                "увидит сканирование штрихкода. Попробуйте запустить программу от имени "
                "администратора. Подробности: " + str(e)
            ),
            "blocking": True,
        }


def _check_webview() -> HealthCheckResult:
    try:
        import webview  # noqa: F401
        return {"id": "webview", "title": "Окно настроек (pywebview)", "status": "ok",
                "message": "Доступно", "blocking": False}
    except ImportError as e:
        return {"id": "webview", "title": "Окно настроек (pywebview)", "status": "error",
                "message": "pywebview не установлен — окно настроек не откроется. "
                            "Установите зависимости: pip install -r requirements.txt. " + str(e),
                "blocking": True}


def _check_webview2_runtime() -> HealthCheckResult:
    """Проверяет НЕ python-пакет pywebview (см. _check_webview выше), а сам
    системный движок Microsoft Edge WebView2 Runtime, которым pywebview
    рисует окно на Windows. main.py вызывает webview2_setup.ensure_installed()
    ДО этой проверки и пытается тихо поставить рантайм сам — если это не
    сработало (нет интернета, отменили UAC, отсутствует в сборке), здесь
    просто честно об этом сообщаем. НЕ блокирующая: без WebView2 pywebview
    откатывается на старый движок, окно всё равно откроется, но без
    современной вёрстки (блюр/скругления из редизайна "Liquid Glass")."""
    try:
        import webview2_setup
        if webview2_setup.is_installed():
            return {"id": "webview2_runtime", "title": "Microsoft Edge WebView2 Runtime",
                    "status": "ok", "message": "Найден", "blocking": False}
        return {
            "id": "webview2_runtime", "title": "Microsoft Edge WebView2 Runtime",
            "status": "warning",
            "message": (
                "Не найден. Окно настроек всё равно откроется, но в устаревшем режиме "
                "совместимости (без современной вёрстки). Автоустановка при старте не "
                "сработала — проверьте интернет на этом ПК или установите вручную: "
                "https://developer.microsoft.com/microsoft-edge/webview2/"
            ),
            "blocking": False,
        }
    except Exception as e:  # noqa: BLE001
        return {"id": "webview2_runtime", "title": "Microsoft Edge WebView2 Runtime",
                "status": "warning", "message": f"Не удалось проверить: {e}", "blocking": False}


def _check_printer(cfg: dict) -> HealthCheckResult:
    try:
        import printer_tspl
        printers = printer_tspl.list_printers()
    except Exception as e:  # noqa: BLE001
        return {"id": "printer", "title": "Принтер", "status": "warning",
                "message": f"Не удалось получить список принтеров Windows: {e}", "blocking": False}

    selected = cfg["printer"].get("name")
    if not printers:
        return {"id": "printer", "title": "Принтер", "status": "warning",
                "message": "Windows не видит ни одного установленного принтера. Подключите принтер "
                            "и установите драйвер, затем выберите его на вкладке «Принтер».",
                "blocking": False}
    if not selected:
        return {"id": "printer", "title": "Принтер", "status": "warning",
                "message": "Принтер найден в системе, но не выбран в настройках — откройте "
                            "вкладку «Принтер» и выберите его из списка.", "blocking": False}
    if selected not in printers:
        return {"id": "printer", "title": "Принтер", "status": "warning",
                "message": f"Выбранный принтер «{selected}» сейчас не виден Windows — проверьте, "
                            f"подключён ли он и включён ли.", "blocking": False}
    return {"id": "printer", "title": "Принтер", "status": "ok",
            "message": f"«{selected}» найден в системе", "blocking": False}


def _check_calibration(cfg: dict) -> HealthCheckResult:
    region = cfg["capture"].get("region_relative", {})
    if region.get("width", 0) > 0 and region.get("height", 0) > 0:
        return {"id": "calibration", "title": "Калибровка области номера", "status": "ok",
                "message": "Область задана", "blocking": False}
    return {"id": "calibration", "title": "Калибровка области номера", "status": "warning",
            "message": "Область распознавания ещё не откалибрована — откройте вкладку "
                        "«Область распознавания» и нажмите «Откалибровать».", "blocking": False}


def run_health_checks(cfg: dict) -> List[HealthCheckResult]:
    checks = [
        _check_tesseract(),
        _check_keyboard_hook(),
        _check_webview(),
        _check_webview2_runtime(),
        _check_printer(cfg),
        _check_calibration(cfg),
    ]
    for c in checks:
        if c["status"] != "ok":
            log.warning("Проверка «%s»: %s — %s", c["title"], c["status"], c["message"])
    return checks


def has_blocking_issues(checks: List[HealthCheckResult]) -> bool:
    return any(c["status"] == "error" and c["blocking"] for c in checks)
