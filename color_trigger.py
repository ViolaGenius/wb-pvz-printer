# -*- coding: utf-8 -*-
"""
color_trigger.py
Доп. функция из ТЗ: печать разрешена только если в заданной точке (или точках)
экрана присутствует определённый цвет. Например, индикатор "ячейка подсвечена
зелёным" рядом с номером — если оператор промахнулся мимо нужного момента,
цвета не будет и печать нужно тихо отменить (без сигнала ошибки).

Сравнение — по одной конкретной точке (X,Y), с допуском в процентах: допуск
задаёт максимально разрешённое отклонение по каждому из каналов R,G,B
относительно 255 (простая и предсказуемая метрика, не требует объяснять
пользователю евклидовы расстояния в цветовом пространстве).
"""

import logging
from typing import Optional, Tuple

import screen_capture

log = logging.getLogger("color_trigger")


def color_within_tolerance(
    actual_rgb: Optional[Tuple[int, int, int]],
    target_rgb: Tuple[int, int, int],
    tolerance_percent: float,
) -> bool:
    if actual_rgb is None:
        return False
    allowed = 255.0 * (max(0.0, tolerance_percent) / 100.0)
    return all(abs(a - t) <= allowed for a, t in zip(actual_rgb, target_rgb))


def evaluate_triggers(cfg_capture: dict, state: "screen_capture.WindowState") -> dict:
    """Проверяет все включённые точки-триггеры согласно логике И/ИЛИ.

    Возвращает {"passed": bool, "details": [...]} — details пригодится и для
    UI (показать какая именно точка не совпала), и для журнала."""
    triggers = [t for t in cfg_capture.get("color_triggers", []) if t.get("enabled", True)]
    if not triggers:
        return {"passed": True, "details": []}

    logic = cfg_capture.get("color_triggers_logic", "AND").upper()
    details = []
    for t in triggers:
        actual = screen_capture.capture_pixel_color(state, t["x"], t["y"])
        target = tuple(t.get("color_rgb", [0, 0, 0]))
        tol = t.get("tolerance_percent", 12)
        ok = color_within_tolerance(actual, target, tol)
        details.append({
            "id": t.get("id"),
            "ok": ok,
            "expected_rgb": list(target),
            "actual_rgb": list(actual) if actual else None,
            "tolerance_percent": tol,
        })

    if logic == "OR":
        passed = any(d["ok"] for d in details)
    else:  # AND по умолчанию
        passed = all(d["ok"] for d in details)

    return {"passed": passed, "details": details}
