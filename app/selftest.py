"""Самопроверка установки: python -m app.selftest [--model tiny]

Проверяет, что работают все компоненты: ffmpeg, модель Whisper, модели диаризации,
и прогоняет короткую синтетическую запись через весь конвейер.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from . import models, pipeline

OK, FAIL = "  ✓", "  ✗"


def _check(name: str, fn) -> bool:
    t0 = time.time()
    try:
        detail = fn()
        print(f"{OK} {name}{f': {detail}' if detail else ''} ({time.time() - t0:.1f} с)")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"{FAIL} {name}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="какую модель Whisper проверять (по умолчанию — первую скачанную)")
    args = ap.parse_args()
    # консоль Windows по умолчанию не в UTF-8
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print(f"Проверка установки (движок: {pipeline.BACKEND}, Python {sys.version.split()[0]}, {sys.platform})")
    installed = [m for m in pipeline.WHISPER_MODELS if models.whisper_installed(m)]
    model = args.model or next((m for m in ("large-v3", "large-v3-turbo", "tiny") if m in installed), None)
    results = []

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "probe.m4a"

        def make_audio():
            # 6 с: тон и шум → AAC в m4a, как у диктофона
            cmd = [pipeline.FFMPEG, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                   "sine=frequency=220:duration=6", "-f", "lavfi", "-i", "anoisesrc=d=6:a=0.02",
                   "-filter_complex", "amix=inputs=2", "-c:a", "aac", "-y", str(wav)]
            subprocess.run(cmd, check=True, capture_output=True, creationflags=pipeline._NO_WINDOW)
            a = pipeline.load_audio(wav)
            return f"ffmpeg читает m4a, {len(a) / pipeline.SAMPLE_RATE:.1f} с"

        results.append(_check("ffmpeg", make_audio))

        def device():
            if pipeline.BACKEND == "mlx":
                import mlx.core as mx

                return f"MLX, {mx.default_device()}"
            d, c = pipeline.fw_device()
            return f"faster-whisper, {d}/{c}"

        results.append(_check("Движок распознавания", device))
        def diar_models():
            if not models.diarization_ready():
                raise RuntimeError("не скачаны, запустите: python -m app.models")
            return "есть"

        results.append(_check("Модели диаризации", diar_models))
        if not model:
            print(f"{FAIL} Модель Whisper не скачана, запустите: python -m app.models")
            results.append(False)
        else:
            def diar():
                audio = np.random.default_rng(0).normal(0, 0.01, pipeline.SAMPLE_RATE * 4).astype(np.float32)
                pipeline._diarizer().process(audio)
                return "sherpa-onnx загружает модели"

            results.append(_check("Диаризация", diar))

            def full():
                r = pipeline.run(wav, model=model, num_speakers=0)
                return f"модель {model}, конвейер отработал ({r.duration:.0f} с аудио)"

            results.append(_check("Весь конвейер", full))

    # «Слушать» — справочно: на серверах без звуковой карты (CI) её может не быть, установку это не валит
    from . import listen

    can, why = listen.support()
    if can:
        try:
            print(f"  ✓ Запись звука компьютера: {listen.describe_device()}")
        except Exception as e:  # noqa: BLE001 — например, на сервере нет звуковой карты
            print(f"  - Запись звука компьютера: устройство вывода не найдено ({e}), расшифровка файлов работает")
    else:
        print(f"  - Запись звука компьютера недоступна: {why}")

    ok = all(results)
    print("\nВсё работает." if ok else "\nЕсть проблемы, подробности выше и в data/logs/app.log")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
