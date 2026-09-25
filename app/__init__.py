"""Транскрибатор: локальная расшифровка аудио с разделением по говорящим."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Локальные настройки установки (пишет установщик), формат KEY=VALUE, например TRANSKRIBATOR_DEVICE=cpu.
# Переменные окружения, заданные явно, имеют приоритет.
_settings = ROOT / "settings.env"
if _settings.exists():
    for _line in _settings.read_text(encoding="utf-8").splitlines():
        _key, _sep, _value = _line.strip().partition("=")
        if _sep and _key and not _key.startswith("#"):
            os.environ.setdefault(_key.strip(), _value.strip())

# Все модели — внутри папки приложения (models/huggingface), а не в скрытом кэше пользователя:
# удалить приложение = удалить папку.
os.environ.setdefault("HF_HOME", str(ROOT / "models" / "huggingface"))
# модели публичные: без токена и телеметрии
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
