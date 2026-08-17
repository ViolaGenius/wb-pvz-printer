"""
sound.py
Звуковые сигналы: "идёт распознавание", "успех", "ошибка".

Порядок поиска звука для каждого события:
  1. Пользовательский .wav, загруженный через веб-UI (вкладка "Детектор
     сканера" -> "Прослушать/Загрузить") — хранится в %APPDATA%\\WB_PVZ_Printer\\sounds\\.
  2. Встроенный .wav из assets/sounds/ (если разработчик положил свои файлы
     туда до сборки exe).
  3. winsound.Beep — системный тон через динамик, громкость которого нельзя
     программно приглушить (ограничение самого API, не библиотеки).
"""

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("sound")

try:
    import winsound
except ImportError:  # для разработки/тестов вне Windows
    winsound = None

ASSETS_SOUNDS_DIR = Path(__file__).parent / "assets" / "sounds"


def get_user_sounds_dir() -> Path:
    import config as config_module
    d = config_module.get_app_data_dir() / "sounds"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_wav_path(wav_name: str) -> Optional[Path]:
    """Возвращает путь к звуку с учётом приоритета: пользовательский > встроенный.
    None, если ни то ни другое не найдено (тогда используется Beep)."""
    user_path = get_user_sounds_dir() / wav_name
    if user_path.exists():
        return user_path
    bundled_path = ASSETS_SOUNDS_DIR / wav_name
    if bundled_path.exists():
        return bundled_path
    return None


def _play_wav_or_beep(wav_name: str, beep_freq: int, beep_dur_ms: int):
    if winsound is None:
        log.debug("winsound недоступен (не Windows) — звук пропущен")
        return
    wav_path = resolve_wav_path(wav_name)
    try:
        if wav_path is not None:
            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            winsound.Beep(beep_freq, beep_dur_ms)
    except RuntimeError:
        # Beep может кинуть RuntimeError если нет физического динамика/спикера —
        # не критично, просто пропускаем звук
        log.debug("Не удалось воспроизвести звук")


def play_capture(cfg_sounds: dict):
    if cfg_sounds.get("on_capture_enabled", True):
        _play_wav_or_beep("capture.wav", 1200, 60)


def play_success(cfg_sounds: dict):
    if cfg_sounds.get("on_success_enabled", True):
        _play_wav_or_beep("success.wav", 1600, 90)


def play_error(cfg_sounds: dict):
    if cfg_sounds.get("on_error_enabled", True):
        # заметно более низкий и длинный сигнал — специально неприятный,
        # чтобы оператор точно заметил проблему (п.3.3 ТЗ), если нет своего .wav
        if winsound is None:
            return
        wav_path = resolve_wav_path("error.wav")
        try:
            if wav_path is not None:
                winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.Beep(300, 200)
                winsound.Beep(220, 300)
        except RuntimeError:
            log.debug("Не удалось воспроизвести звук ошибки")
