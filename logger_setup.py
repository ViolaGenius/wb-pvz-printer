"""
logger_setup.py
Настройка логирования ошибок в файл (отдельно от "журнала печати", который
видит пользователь в UI и который хранится в print_log.json через config.py).
"""

import logging
import logging.handlers

from config import get_app_data_dir


def setup_logging(level=logging.INFO):
    log_path = get_app_data_dir() / "logs" / "app.log"

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    return root
