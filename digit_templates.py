# -*- coding: utf-8 -*-
"""
digit_templates.py
Самообучающиеся эталоны цифр — второе "мнение" в ансамбле распознавания,
которое появляется и уточняется САМО, без ручной калибровки пользователем.

Как это работает:
  1. Когда recognize_with_retries получает результат, где tesseract уверен
     ≥ harvest_min_confidence (по умолчанию 90%) И минимум 2 из попыток
     согласились между собой (voting) — engine.py вызывает harvest_from_result(),
     которая сегментирует распознанную строку на отдельные символы
     (segmentation.py) и сохраняет их как образцы для соответствующих цифр.
  2. Как только для цифры накопится min_samples_to_trust образцов — эталон
     считается "созревшим" и участвует в сравнении как ещё один голос в
     ансамбле (наравне с tesseract на целой строке и посимвольным tesseract).
  3. Хранится максимум max_samples_per_digit последних образцов на цифру
     (старые вытесняются) — так эталон "плывёт" вместе с реальным шрифтом,
     если тот вдруг слегка изменится (например, после обновления "Мой ПВЗ").

ОГРАНИЧЕНИЕ (честно, не скрываем от пользователя): "уверенность tesseract
90%+" — это не гарантия правильности, а лишь сигнал "скорее всего верно".
Если первые харвестнутые образцы окажутся ошибочными — это временно ухудшит
качество эталонов для этой цифры. Голосование (2 из 3 попыток согласны)
снижает этот риск, но не убирает полностью. Есть кнопка "Сбросить эталоны"
в UI (вкладка "Область распознавания") — на случай, если заметите, что
эталоны "выучили" не то.
"""

import json
import logging
import time
import uuid
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

import config as config_module

log = logging.getLogger("digit_templates")

TEMPLATES_DIR = config_module.get_app_data_dir() / "digit_templates"
MANIFEST_PATH = TEMPLATES_DIR / "manifest.json"
NORM_SIZE = (24, 32)  # все образцы приводятся к этому размеру перед сравнением/хранением


def _ensure_dirs():
    for d in [str(i) for i in range(10)]:
        (TEMPLATES_DIR / d).mkdir(parents=True, exist_ok=True)


def _load_manifest() -> Dict[str, List[str]]:
    """{"0": ["<файл1>.png", ...], "1": [...], ...} — порядок = от старых к новым."""
    if not MANIFEST_PATH.exists():
        return {str(d): [] for d in range(10)}
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for d in range(10):
            data.setdefault(str(d), [])
        return data
    except (json.JSONDecodeError, OSError):
        return {str(d): [] for d in range(10)}


def _save_manifest(manifest: Dict[str, List[str]]):
    _ensure_dirs()
    tmp = MANIFEST_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    tmp.replace(MANIFEST_PATH)


def sample_counts() -> Dict[str, int]:
    """Для UI — сколько образцов накоплено на каждую цифру (прозрачность самообучения)."""
    manifest = _load_manifest()
    return {d: len(files) for d, files in manifest.items()}


def reset_templates():
    """Кнопка "Сбросить эталоны" в UI — на случай, если самообучение "выучило" не то
    (например, после смены шрифта в "Мой ПВЗ" или если ранние образцы были ошибочными)."""
    import shutil
    if TEMPLATES_DIR.exists():
        shutil.rmtree(TEMPLATES_DIR)
    _ensure_dirs()
    _save_manifest({str(d): [] for d in range(10)})
    log.info("Эталоны цифр сброшены")


def _normalize_image(char_img: Image.Image) -> Image.Image:
    """Приводит вырезанный символ к единому размеру NORM_SIZE, СОХРАНЯЯ пропорции
    (вписывает по большей стороне и центрирует на белом поле), а не растягивает
    напрямую. Растягивание без сохранения пропорций искажало форму символа —
    узкая "1" и широкая "8" приводились к одному прямоугольнику, что мешало
    сравнению. Используется и при сохранении эталона на диск, и при сравнении —
    важно, чтобы обе стороны сравнения проходили ОДНУ и ту же нормализацию."""
    gray = char_img.convert("L")
    w, h = gray.size
    if w == 0 or h == 0:
        return Image.new("L", NORM_SIZE, color=255)
    target_w, target_h = NORM_SIZE
    scale = min(target_w / w, target_h / h)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    resized = gray.resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("L", NORM_SIZE, color=255)  # белый фон — как у обычного bw после бинаризации
    canvas.paste(resized, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    return canvas


def _normalize(char_img: Image.Image) -> np.ndarray:
    """То же самое, что _normalize_image, но сразу как numpy-массив для сравнения."""
    return np.asarray(_normalize_image(char_img), dtype=np.float64)


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    """Нормализованная кросс-корреляция — устойчива к небольшим отличиям в
    среднем уровне яркости между образцами. Возвращает -1..1, где 1 — идеальное
    совпадение формы."""
    a = a - a.mean()
    b = b - b.mean()
    denom = np.sqrt((a ** 2).sum()) * np.sqrt((b ** 2).sum())
    if denom == 0:
        return 0.0
    return float((a * b).sum() / denom)


def harvest_from_result(digits: str, char_images: List[Image.Image],
                         max_samples_per_digit: int = 5) -> int:
    """Сохраняет каждый символ из char_images как образец соответствующей цифры
    из digits (индексы должны совпадать по порядку — вызывающий код отвечает за
    то, что сегментация дала ровно len(digits) кусков). Возвращает, сколько
    образцов реально сохранено."""
    if len(digits) != len(char_images):
        log.debug("harvest_from_result: количество символов не совпало с сегментацией — пропуск")
        return 0
    _ensure_dirs()
    manifest = _load_manifest()
    saved = 0
    for digit_char, char_img in zip(digits, char_images):
        if digit_char not in manifest:
            continue
        fname = f"{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
        norm = _normalize_image(char_img)
        norm.save(TEMPLATES_DIR / digit_char / fname)
        manifest[digit_char].append(fname)
        # держим не больше max_samples_per_digit — старые вытесняем
        while len(manifest[digit_char]) > max_samples_per_digit:
            old_fname = manifest[digit_char].pop(0)
            old_path = TEMPLATES_DIR / digit_char / old_fname
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                pass
        saved += 1
    _save_manifest(manifest)
    return saved


_cache: Optional[Dict[str, List[np.ndarray]]] = None
_cache_mtime: float = 0.0


def _load_templates_cached() -> Dict[str, List[np.ndarray]]:
    """Кэширует загруженные эталоны в память — сравнение идёт на каждый скан,
    незачем читать файлы с диска каждый раз. Кэш сбрасывается при изменении
    manifest.json (harvest/reset)."""
    global _cache, _cache_mtime
    if not MANIFEST_PATH.exists():
        return {str(d): [] for d in range(10)}
    mtime = MANIFEST_PATH.stat().st_mtime
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    manifest = _load_manifest()
    result: Dict[str, List[np.ndarray]] = {}
    for digit, files in manifest.items():
        arrays = []
        for fname in files:
            path = TEMPLATES_DIR / digit / fname
            try:
                with Image.open(path) as im:
                    arrays.append(np.asarray(im.convert("L"), dtype=np.float64))
            except OSError:
                continue
        result[digit] = arrays
    _cache = result
    _cache_mtime = mtime
    return result


def match_digit(char_img: Image.Image, min_samples_to_trust: int = 5,
                 min_score: float = 75.0) -> Tuple[Optional[str], float]:
    """Сравнивает один вырезанный символ со всеми "созревшими" эталонами (те,
    для которых накоплено >= min_samples_to_trust образцов). Возвращает
    (цифра_или_None, score_0_100).

    ВАЖНО: возвращает цифру, только если её score >= min_score (абсолютный
    порог), а не просто "лучшую среди имеющихся" — иначе если эталон для
    РЕАЛЬНОЙ цифры на изображении ещё не созрел, функция могла бы вернуть
    визуально похожую, но НЕВЕРНУЮ цифру из уже готовых эталонов с ложной
    уверенностью. Лучше честно сказать "не знаю", чем угадывать."""
    templates = _load_templates_cached()
    candidate = _normalize(char_img)
    best_digit, best_score = None, -1.0
    for digit, arrays in templates.items():
        if len(arrays) < min_samples_to_trust:
            continue
        scores = [_ncc(candidate, arr) for arr in arrays]
        digit_best = max(scores)
        if digit_best > best_score:
            best_score = digit_best
            best_digit = digit
    if best_digit is None:
        return None, 0.0
    score_0_100 = max(0.0, (best_score + 1) / 2 * 100)
    if score_0_100 < min_score:
        return None, score_0_100
    return best_digit, score_0_100


def match_number(char_images: List[Image.Image], min_samples_to_trust: int = 5,
                  min_score: float = 75.0) -> Tuple[Optional[str], float]:
    """То же самое, но для целой последовательности символов — используется как
    ещё один "голос" в ансамбле recognize_with_retries. Если хотя бы один
    символ не удалось сопоставить ни с одним созревшим эталоном с достаточной
    уверенностью — вся попытка считается неудачной (не гадаем частично)."""
    if not char_images:
        return None, 0.0
    digits = []
    scores = []
    for char_img in char_images:
        d, s = match_digit(char_img, min_samples_to_trust=min_samples_to_trust, min_score=min_score)
        if d is None:
            return None, 0.0
        digits.append(d)
        scores.append(s)
    return "".join(digits), sum(scores) / len(scores)
