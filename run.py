#!/usr/bin/env python3
"""Точка входа: поднимает локальный сервер транскрибатора и открывает браузер.

Используется ярлыками (Windows/macOS), виджетом Übersicht и start-скриптами.
  --no-browser — не открывать вкладку.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
import app  # noqa: E402,F401 — читает settings.env и задаёт HF_HOME до всего остального

PORT = int(os.environ.get("TRANSKRIBATOR_PORT", "8770"))
HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}/"
if sys.platform == "darwin":
    # из Finder/виджета PATH минимальный
    os.environ["PATH"] = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")])
# модели скачаны установщиком — работаем без обращения к интернету
os.environ.setdefault("HF_HUB_OFFLINE", "1")
# в работающем приложении не нужны полоски «Fetching files» при каждой загрузке модели (при установке — нужны)
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def _already_running() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/health", timeout=1) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _open_browser() -> None:
    time.sleep(1.5)
    webbrowser.open(URL)


def main() -> None:
    if "--wait-free" in sys.argv:
        # перезапуск после обновления: ждём, пока старый процесс освободит порт
        for _ in range(60):
            if not _already_running():
                break
            time.sleep(0.5)
    if _already_running():
        print(f"Транскрибатор уже запущен: {URL}")
        if "--no-browser" not in sys.argv:
            webbrowser.open(URL)
        return

    import uvicorn

    from app import logs, models

    log = logs.setup("server")
    if not models.diarization_ready():
        print("Модели не найдены. Запустите установщик (install.sh / install.bat) или: python -m app.models")
    log.info("Запуск сервера на %s (pid %d)", URL, os.getpid())
    print(f"\n  Транскрибатор: {URL}\n  Остановить — закройте это окно или нажмите Ctrl+C.\n  Логи: {ROOT / 'data' / 'logs'}\n")
    if "--no-browser" not in sys.argv:
        threading.Thread(target=_open_browser, daemon=True).start()
    try:
        uvicorn.run("app.server:app", host=HOST, port=PORT, log_level="warning")
    except Exception:
        log.exception("Сервер упал")
        raise
    finally:
        log.info("Сервер остановлен")


if __name__ == "__main__":
    main()
