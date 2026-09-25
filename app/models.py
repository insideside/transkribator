"""Загрузка и проверка моделей. Запуск: python -m app.models [--whisper large-v3,large-v3-turbo] [--all].

Скачивает:
  • модели диаризации sherpa-onnx (сегментация pyannote-3.0 и эмбеддинги голоса 3D-Speaker) в models/;
  • модель Whisper в формате текущего движка (MLX или faster-whisper) в кэш Hugging Face.
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_DIR = "sherpa-onnx-pyannote-segmentation-3-0"
SEGMENTATION_URL = f"{SHERPA}/speaker-segmentation-models/{SEGMENTATION_DIR}.tar.bz2"
EMBEDDING_FILE = "3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx"
EMBEDDING_URL = f"{SHERPA}/speaker-recongition-models/{EMBEDDING_FILE}"
# ориентировочные размеры для сообщений установщика
SIZES = {"large-v3": "≈3 ГБ", "large-v3-turbo": "≈1.6 ГБ", "tiny": "≈75 МБ"}


def _download(url: str, dest: Path) -> None:
    """Скачивание с прогрессом во временный файл и атомарная замена."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, shown = 0, -1
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else -1
            if total and pct // 10 != shown // 10:   # раз в 10 %, чтобы не засорять лог установки
                shown = pct
                print(f"    {pct:3d} %  ({done / 1e6:.0f} из {total / 1e6:.0f} МБ)", flush=True)
    tmp.replace(dest)


def diarization_ready() -> bool:
    return (MODELS_DIR / SEGMENTATION_DIR / "model.onnx").exists() and (MODELS_DIR / EMBEDDING_FILE).exists()


def ensure_diarization() -> None:
    if not (MODELS_DIR / SEGMENTATION_DIR / "model.onnx").exists():
        print("• Модель сегментации речи (pyannote 3.0, ≈6 МБ)")
        with tempfile.TemporaryDirectory() as tmp:
            arc = Path(tmp) / "seg.tar.bz2"
            _download(SEGMENTATION_URL, arc)
            with tarfile.open(arc) as t:
                t.extractall(MODELS_DIR, filter="data")
    if not (MODELS_DIR / EMBEDDING_FILE).exists():
        print("• Модель голосовых отпечатков (3D-Speaker ERes2NetV2, ≈70 МБ)")
        _download(EMBEDDING_URL, MODELS_DIR / EMBEDDING_FILE)


def whisper_installed(model: str) -> bool:
    from huggingface_hub import snapshot_download

    from .pipeline import WHISPER_MODELS

    try:
        snapshot_download(WHISPER_MODELS[model], local_files_only=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_whisper(model: str) -> None:
    from huggingface_hub import snapshot_download

    from .pipeline import BACKEND, WHISPER_MODELS

    repo = WHISPER_MODELS[model]
    if whisper_installed(model):
        print(f"• Whisper {model} уже скачана")
        return
    print(f"• Whisper {model} для движка {BACKEND} ({SIZES.get(model, '')}): {repo}")
    from huggingface_hub.utils import enable_progress_bars

    enable_progress_bars()  # в приложении полоски выключены, при установке — нужны
    snapshot_download(repo)


def default_models() -> list[str]:
    """Что ставить по умолчанию: на GPU — точную модель, на CPU — быструю."""
    from .pipeline import BACKEND, fw_device

    if BACKEND == "mlx":
        return ["large-v3"]
    return ["large-v3"] if fw_device()[0] == "cuda" else ["large-v3-turbo"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Скачивание моделей транскрибатора")
    ap.add_argument("--whisper", help="модели Whisper через запятую: large-v3, large-v3-turbo, tiny")
    ap.add_argument("--all", action="store_true", help="обе модели: large-v3 и large-v3-turbo")
    ap.add_argument("--check", action="store_true", help="только проверить, всё ли скачано")
    args = ap.parse_args()

    from .pipeline import BACKEND

    wanted = ["large-v3", "large-v3-turbo"] if args.all else (
        [m.strip() for m in args.whisper.split(",") if m.strip()] if args.whisper else default_models())
    if args.check:
        ok = diarization_ready() and all(whisper_installed(m) for m in wanted)
        print("Модели на месте" if ok else "Не все модели скачаны")
        return 0 if ok else 1

    print(f"Движок распознавания: {BACKEND}")
    try:
        ensure_diarization()
        for m in wanted:
            ensure_whisper(m)
    except Exception as e:  # noqa: BLE001
        print(f"\nОшибка загрузки: {e}\nПроверьте интернет и запустите установку ещё раз — скачанное не потеряется.")
        return 1
    print("Модели готовы.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
