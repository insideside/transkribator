"""Логирование: ротируемый app.log, crash.log для падений на уровне C/Metal, перехват необработанных исключений."""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"
APP_LOG = LOG_DIR / "app.log"
CRASH_LOG = LOG_DIR / "crash.log"

_configured = False
_crash_file = None


def setup(process_name: str = "server") -> logging.Logger:
    """Настраивает логирование процесса. Повторный вызов безопасен."""
    global _configured, _crash_file
    log = logging.getLogger("transkribator")
    if _configured:
        return log
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        f"%(asctime)s %(levelname)-7s [{process_name}:%(process)d %(threadName)s] %(name)s: %(message)s"
    )
    # несколько процессов пишут в один файл: ротацию делает только сервер
    if process_name == "server":
        fh: logging.Handler = RotatingFileHandler(APP_LOG, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    else:
        fh = logging.FileHandler(APP_LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if sys.stderr is not None:  # у pythonw.exe консоли нет
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    for noisy in ("httpx", "huggingface_hub", "urllib3", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # segfault / abort в нативном коде (MLX, Metal, onnxruntime) — дамп стеков всех потоков
    _crash_file = open(CRASH_LOG, "a", encoding="utf-8")  # noqa: SIM115 — держим открытым до выхода
    _crash_file.write(f"\n--- {process_name} pid={os.getpid()} started ---\n")
    _crash_file.flush()
    faulthandler.enable(file=_crash_file, all_threads=True)

    def excepthook(exc_type, exc, tb):
        logging.getLogger("transkribator").critical("Необработанное исключение", exc_info=(exc_type, exc, tb))

    def thread_excepthook(args):
        logging.getLogger("transkribator").critical(
            "Необработанное исключение в потоке %s", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    _configured = True
    return log
