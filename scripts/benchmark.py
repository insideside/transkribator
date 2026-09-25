"""Замер скорости и памяти на вашем компьютере.

Запуск из папки приложения:
    macOS / Linux:  .venv/bin/python scripts/benchmark.py <запись> [модель] [число говорящих]
    Windows:        .venv\\Scripts\\python.exe scripts\\benchmark.py <запись> [модель] [число говорящих]

модель: large-v3 (точная, по умолчанию) или large-v3-turbo (быстрая); число говорящих: 0 = авто (по умолчанию).
Печатает одну строку JSON — её можно добавить в docs/BENCHMARKS.md (раздел «Замеры сообщества»).
Нагрузка — как у обычной расшифровки этой записи; для ориентира достаточно записи на 10–20 минут.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import pipeline as P  # noqa: E402


def _peak_rss_gb() -> tuple[float | None, float | None]:
    """Пик памяти (ГБ): основной процесс и дочерний (разделение по голосам)."""
    try:
        import resource

        scale = 1 if sys.platform == "darwin" else 1024  # macOS — байты, Linux — КБ
        return (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale / 1e9,
                resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale / 1e9)
    except ImportError:  # Windows: пик рабочего набора своего процесса
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = PMC(cb=ctypes.sizeof(PMC))
        ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize / 1e9, None


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    path = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else "large-v3"
    speakers = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    t0 = time.time()
    r = P.run(path, model=model, num_speakers=speakers)
    elapsed = time.time() - t0
    main_gb, diar_gb = _peak_rss_gb()
    gpu_gb = None
    if P.BACKEND == "mlx":
        import mlx.core as mx

        gpu_gb = mx.get_peak_memory() / 1e9
    device = "gpu-apple" if P.BACKEND == "mlx" else "/".join(P.fw_device())
    print(json.dumps({
        "machine": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpu": platform.processor() or platform.machine(), "cpu_count": os.cpu_count(),
        "backend": P.BACKEND, "device": device, "model": model,
        "audio_min": round(r.duration / 60, 1), "sec": round(elapsed), "x_realtime": round(r.duration / elapsed, 1),
        "hour_min_est": round(elapsed / r.duration * 60, 1),
        "ram_main_gb": main_gb and round(main_gb, 2), "ram_diarize_gb": diar_gb and round(diar_gb, 2),
        "mlx_peak_gb": gpu_gb and round(gpu_gb, 2), "speakers": r.num_speakers, "warnings": r.warnings,
    }, ensure_ascii=False))


if __name__ == "__main__":  # обязательно: диаризация идёт в отдельном процессе (spawn)
    main()
