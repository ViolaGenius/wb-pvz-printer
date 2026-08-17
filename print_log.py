# -*- coding: utf-8 -*-
"""
print_log.py
Журнал последних N распознанных/напечатанных номеров с таймштампами (п.3.6 ТЗ),
виден в UI. Отдельно от файла логов ошибок (см. logger_setup.py).

success: True — успешная печать, False — ошибка, None — нейтральное событие
(например, печать тихо пропущена из-за несовпадения цветового триггера — это
не ошибка оператора и не должно пугать красным в журнале).

Поддерживает фильтрацию (по дате/статусу/номеру) и экспорт в CSV — доп.
функция по фидбэку пользователя.
"""

import csv
import io
import json
import threading
import time
from typing import List, Optional

from config import get_print_log_path

_lock = threading.Lock()
MAX_ENTRIES = 2000  # храним с запасом, get_recent сам режет под нужный лимит


def _load() -> List[dict]:
    path = get_print_log_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(entries: List[dict]):
    path = get_print_log_path()
    with _lock:
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries[-MAX_ENTRIES:], f, ensure_ascii=False, indent=2)
        tmp.replace(path)


def add_entry(cell_number: Optional[str], success: Optional[bool], note: str = ""):
    entries = _load()
    entries.append(
        {
            "timestamp": time.time(),
            "cell_number": cell_number,
            "success": success,
            "note": note,
        }
    )
    _save(entries)


def clear_log():
    _save([])


def get_recent(
    limit: int = 200,
    date_from: Optional[float] = None,
    date_to: Optional[float] = None,
    status: Optional[str] = None,  # "success" | "error" | "skipped" | None (все)
    search: Optional[str] = None,  # подстрока по номеру ячейки
) -> List[dict]:
    entries = list(reversed(_load()))

    def matches(e: dict) -> bool:
        if date_from is not None and e["timestamp"] < date_from:
            return False
        if date_to is not None and e["timestamp"] > date_to:
            return False
        if status == "success" and e["success"] is not True:
            return False
        if status == "error" and e["success"] is not False:
            return False
        if status == "skipped" and e["success"] is not None:
            return False
        if search:
            cn = (e.get("cell_number") or "")
            if search.lower() not in cn.lower():
                return False
        return True

    filtered = [e for e in entries if matches(e)]
    return filtered[:limit]


def export_csv(date_from: Optional[float] = None, date_to: Optional[float] = None,
                status: Optional[str] = None, search: Optional[str] = None) -> str:
    entries = get_recent(limit=MAX_ENTRIES, date_from=date_from, date_to=date_to,
                          status=status, search=search)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Дата/время", "Номер ячейки", "Статус", "Комментарий"])
    for e in entries:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["timestamp"]))
        status_text = {"True": "Успех", "False": "Ошибка", "None": "Пропущено"}[str(e["success"])]
        writer.writerow([ts, e.get("cell_number") or "", status_text, e.get("note") or ""])
    return buf.getvalue()
