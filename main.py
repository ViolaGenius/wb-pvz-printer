"""
main.py
Точка входа. Запускает:
  - фоновый Engine (детектор сканера + захват + OCR + печать),
  - локальный Flask-сервер для веб-UI,
  - окно настроек через pywebview (изначально скрыто, открывается из трея),
  - иконку в системном трее.

АРХИТЕКТУРНОЕ РЕШЕНИЕ (главный поток): pystray и pywebview оба в идеале хотят
работать в главном потоке процесса. На Windows pystray на практике нормально
работает и в фоновом потоке, поэтому здесь он запущен в отдельном потоке, а
pywebview.start() занимает главный поток (это его жёсткое требование на
некоторых бэкендах). Окно настроек создаётся один раз и скрывается/показывается
через window.hide()/window.show(), а не создаётся заново — так обходится
ограничение большинства бэкендов pywebview на повторный вызов start().
"""

import logging
import sys
import threading

import autostart
import config as config_module
import webview2_setup
from engine import Engine
from hotkeys import HotkeyManager
from logger_setup import setup_logging
from tray import TrayApp
from web_ui.server import run_server

log = logging.getLogger("main")

try:
    import webview
except ImportError:
    webview = None


def main():
    setup_logging()
    log.info("Запуск WB ПВЗ Автопечать этикеток")

    # Пытаемся тихо поставить WebView2 Runtime ДО создания окна и ДО
    # health-check (см. webview2_setup.py) — если получится, окно настроек
    # сразу откроется на современном движке; если нет, ничего не падает,
    # pywebview просто откатится на старый движок, а health_check честно
    # предупредит об этом на дашборде.
    try:
        webview2_setup.ensure_installed()
    except Exception:  # noqa: BLE001 — установка не должна ронять запуск программы
        log.exception("Непредвиденная ошибка при проверке/установке WebView2 Runtime")

    cfg = config_module.load_config()

    # применяем настройку автозапуска при каждом старте — так переключатель в
    # UI гарантированно синхронизирован с реальным состоянием реестра, даже
    # если пользователь менял его вручную между запусками
    autostart.set_enabled(cfg["app"].get("autostart", True))

    engine = Engine()

    # "Мастер первого запуска": реальный отдельный визард не заводим (см. docstring
    # health_check.py) — вместо этого при критичных проблемах открываем окно
    # настроек СРАЗУ (не скрытым, как обычно) с блокирующей модалкой внутри UI.
    health = engine.run_health_checks()
    show_window_immediately = health["has_blocking_issues"]
    if show_window_immediately:
        log.warning("Обнаружены критичные проблемы при старте — окно настроек будет открыто сразу")

    window_holder = {"window": None}

    def open_settings():
        w = window_holder.get("window")
        if w is not None:
            try:
                w.show()
            except Exception:  # noqa: BLE001
                log.exception("Не удалось показать окно настроек")

    def toggle_pause():
        return engine.toggle_pause()

    def repeat_last():
        engine.repeat_last_print()

    def do_exit():
        log.info("Завершение работы")
        hotkey_manager.unregister_all()
        engine.stop()
        w = window_holder.get("window")
        if w is not None:
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                pass
        # даём Flask/трей потокам немного времени на остановку, затем жёстко выходим —
        # они daemon-потоки, так что процесс всё равно завершится корректно
        threading.Timer(0.5, lambda: __import__("os")._exit(0)).start()

    hotkey_manager = HotkeyManager()
    hotkey_manager.apply_from_config(
        cfg["app"],
        on_pause=toggle_pause,
        on_repeat=repeat_last,
        on_open_settings=open_settings,
    )
    # движок хранит cfg отдельно от переменной cfg в этой функции — при сохранении
    # настроек в веб-UI хоткеи нужно перерегистрировать под новые комбинации;
    # server.py дергает этот колбэк после успешного сохранения app.* настроек
    engine.on_hotkeys_changed = lambda new_cfg: hotkey_manager.apply_from_config(
        new_cfg["app"], on_pause=toggle_pause, on_repeat=repeat_last, on_open_settings=open_settings,
    )

    tray = TrayApp(
        on_open_settings=open_settings,
        on_toggle_pause=toggle_pause,
        on_repeat_last=repeat_last,
        on_exit=do_exit,
    )
    engine.add_status_listener(lambda status, text: tray.set_status(status, text))

    # Flask-сервер веб-UI — в фоновом потоке
    port = cfg["app"].get("web_ui_port", 8765)
    flask_thread = threading.Thread(target=run_server, args=(engine,), kwargs={"port": port}, daemon=True)
    flask_thread.start()

    # иконка в трее — в фоновом потоке
    tray.run_in_thread()

    # движок (хук клавиатуры сканера) — в фоновом потоке
    engine.start()

    if webview is None:
        log.error("pywebview не установлен — окно настроек недоступно, работает только фоновая печать")
        # держим процесс живым за счёт join фоновых потоков
        flask_thread.join()
        return

    window = webview.create_window(
        "WB ПВЗ — Автопечать этикеток",
        url=f"http://127.0.0.1:{port}",
        width=1080,
        height=760,
        hidden=not show_window_immediately,
    )
    window_holder["window"] = window

    # закрытие окна крестиком должно просто скрывать его (программа продолжает
    # работать в трее), а не завершать процесс — это ожидаемое поведение для
    # фоновых Windows-приложений с иконкой в трее
    def on_closing():
        window.hide()
        return False  # False = не закрывать окно по-настоящему, просто спрятать

    window.events.closing += on_closing

    webview.start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
