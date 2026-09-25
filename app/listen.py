"""«Слушать»: запись звука, который воспроизводит этот компьютер (не микрофон).

• macOS 14.2+: помощник bin/macos/TranskribatorAudio.app (Core Audio Process Tap). Он запускается через
  `open`, поэтому разрешение «Запись системного звука» macOS запрашивает от его имени — как бы ни был
  запущен сервер. Управление — через файлы в папке сессии (control / status.json / audio.wav).
• Windows: WASAPI loopback устройства вывода по умолчанию (PyAudioWPatch), прямо в процессе сервера.
  Разрешений не требуется. Когда ничего не играет, loopback не присылает данных — тишину досыпаем сами,
  чтобы время в записи совпадало с реальным.

Одновременно идёт не больше одной записи. Готовая запись конвертируется в m4a и попадает в журнал.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import ROOT
from .pipeline import FFMPEG, _NO_WINDOW

log = logging.getLogger("transkribator.listen")

SESSIONS = ROOT / "data" / "listen"
MAC_HELPER = ROOT / "bin" / "macos" / "TranskribatorAudio.app"
SILENCE_DB = -90.0  # ниже — считаем, что звука нет


# ─────────────────────────── поддержка платформы ───────────────────────────

def support() -> tuple[bool, str]:
    """(можно ли записывать, пояснение)."""
    if sys.platform == "darwin":
        ver = tuple(int(x) for x in (platform.mac_ver()[0] or "0").split(".")[:2] + ["0"])[:2]
        if ver < (14, 2):
            return False, f"Запись звука компьютера требует macOS 14.2 или новее (у вас {platform.mac_ver()[0]})."
        if not (MAC_HELPER / "Contents" / "MacOS" / "audiotap").exists():
            return False, "Не найден помощник записи bin/macos/TranskribatorAudio.app — переустановите приложение."
        return True, ("Записывается звук, который играет этот Mac. При первой записи macOS попросит разрешение "
                      "«Запись системного звука» для «Транскрибатор Звук» — нажмите «Разрешить».")
    if sys.platform == "win32":
        try:
            import pyaudiowpatch  # noqa: F401
        except ImportError:
            return False, "Не установлен модуль записи PyAudioWPatch — запустите install.bat ещё раз."
        return True, ("Записывается звук, который играет устройство вывода по умолчанию (колонки или наушники). "
                      "Разрешения не нужны.")
    return False, "Запись звука компьютера поддерживается на macOS 14.2+ и Windows 10/11."


def describe_device() -> str:
    """Для самопроверки: с какого устройства будет идти запись (Windows) — без начала записи."""
    if sys.platform != "win32":
        return "системный звук macOS (все приложения)" if sys.platform == "darwin" else "—"
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        loop = next((d for d in pa.get_loopback_device_info_generator() if speakers["name"] in d["name"]), None)
        if loop is None:
            return f"для «{speakers['name']}» нет loopback-входа"
        return f"«{speakers['name']}», {int(loop['defaultSampleRate'])} Гц, {int(loop['maxInputChannels'])} кан."
    finally:
        pa.terminate()


def _level_db(pcm16: bytes, channels: int) -> float:
    a = np.frombuffer(pcm16, dtype=np.int16)
    if a.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean((a[::max(channels, 1)].astype(np.float32) / 32768.0) ** 2)))
    return 20 * np.log10(rms) if rms > 0 else -120.0


# ─────────────────────────── запись WAV с досыпанием тишины ───────────────────────────

class PcmWriter:
    """Пишет PCM16 в WAV в отдельном потоке; следит за паузой, уровнем и пропусками данных.

    feed() вызывается из аудио-колбэка. Если данных нет дольше gap_sec (loopback молчит, когда нечего
    воспроизводить), дописывается тишина по реальным часам — длительность записи совпадает с реальной.
    """

    def __init__(self, path: Path, rate: int, channels: int, gap_sec: float = 0.3,
                 clock: Callable[[], float] = time.monotonic):
        self.rate, self.channels, self.gap = rate, channels, gap_sec
        self.clock = clock
        self.frame_bytes = 2 * channels
        self.wav = wave.open(str(path), "wb")
        self.wav.setnchannels(channels)
        self.wav.setsampwidth(2)
        self.wav.setframerate(rate)
        self.q: queue.Queue = queue.Queue()
        self.recording = False
        self.frames = 0
        self.level_db = -120.0
        self.error: Optional[str] = None
        self._last_audio = self.clock()   # когда последний раз писали (данные или тишину)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="listen-writer")
        self.thread.start()

    def feed(self, pcm16: bytes) -> None:
        self.level_db = _level_db(pcm16, self.channels)
        if self.recording:
            self.q.put((self.clock(), pcm16))  # время прихода: кусок звучал в [t − длительность, t]

    def set_recording(self, on: bool) -> None:
        self.q.put(("state", on))

    def _write(self, data: bytes) -> None:
        self.wav.writeframesraw(data)
        self.frames += len(data) // self.frame_bytes

    def _run(self) -> None:
        try:
            while not self._stop.is_set() or not self.q.empty():
                try:
                    item = self.q.get(timeout=0.1)
                except queue.Empty:
                    item = None
                now = self.clock()
                if isinstance(item, tuple) and item[0] == "state":   # смена состояния запись/пауза
                    self.recording = item[1]
                    self._last_audio = now
                    continue
                if item is not None:
                    t, data = item
                    # тишина между прошлым звуком и началом этого куска (loopback молчал)
                    silence = (t - len(data) / self.frame_bytes / self.rate) - self._last_audio
                    if silence > self.gap / 2:
                        self._write(b"\x00" * (int(silence * self.rate) * self.frame_bytes))
                    self._write(data)
                    self._last_audio = t
                elif self.recording and now - self._last_audio > self.gap:
                    # долго нет данных — дописываем тишину сразу, чтобы длительность росла по реальным часам
                    self._write(b"\x00" * (int((now - self._last_audio) * self.rate) * self.frame_bytes))
                    self._last_audio = now
                    self.level_db = -120.0
        except Exception as e:  # noqa: BLE001
            self.error = f"Ошибка записи файла: {e}"
            log.exception("Ошибка записи WAV")

    def close(self) -> None:
        self._stop.set()
        self.thread.join(timeout=10)
        self.wav.close()

    @property
    def seconds(self) -> float:
        return self.frames / self.rate


# ─────────────────────────── источники звука ───────────────────────────

class MacSource:
    """Помощник TranskribatorAudio.app; состояние читается из status.json."""

    def __init__(self, session: Path):
        self.session = session
        (session / "control").write_text("record", encoding="utf-8")
        subprocess.run(["open", "-n", "-a", str(MAC_HELPER), "--args", str(session), str(os.getpid())],
                       check=True, capture_output=True, timeout=30)
        self.started_at = time.time()

    def _status(self) -> dict:
        try:
            return json.loads((self.session / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def command(self, cmd: str) -> None:
        (self.session / "control").write_text(cmd, encoding="utf-8")

    def status(self) -> dict:
        s = self._status()
        if not s:
            if time.time() - self.started_at > 90:
                return {"state": "error", "error": "Помощник записи не запустился. Проверьте разрешение в Системных "
                                                   "настройках → Конфиденциальность и безопасность."}
            return {"state": "starting"}
        return {"state": s.get("state", "starting"), "seconds": s.get("seconds", 0),
                "level_db": s.get("level_db", -120), "error": s.get("error")}

    def stop(self) -> Path:
        self.command("stop")
        for _ in range(100):  # до 10 с — помощник дописывает очередь и закрывает файл
            if self._status().get("state") in ("stopped", "error"):
                break
            time.sleep(0.1)
        return self.session / "audio.wav"


class WinSource:
    """WASAPI loopback устройства вывода по умолчанию (PyAudioWPatch)."""

    def __init__(self, session: Path):
        import pyaudiowpatch as pyaudio

        self.pa = pyaudio.PyAudio()
        try:
            wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError as e:
            raise RuntimeError("В системе недоступен WASAPI") from e
        speakers = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not speakers.get("isLoopbackDevice"):
            for dev in self.pa.get_loopback_device_info_generator():
                if speakers["name"] in dev["name"]:
                    speakers = dev
                    break
            else:
                raise RuntimeError(f"Для устройства «{speakers['name']}» не найден loopback-вход")
        self.device_name = speakers["name"]
        rate, channels = int(speakers["defaultSampleRate"]), max(1, int(speakers["maxInputChannels"]))
        self.writer = PcmWriter(session / "audio.wav", rate, channels)
        self.writer.set_recording(True)
        self.session = session

        def callback(in_data, frame_count, time_info, status):
            self.writer.feed(in_data)
            return (None, pyaudio.paContinue)

        self.stream = self.pa.open(format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                                   input_device_index=speakers["index"], frames_per_buffer=rate // 10,
                                   stream_callback=callback)
        self.stream.start_stream()
        log.info("Запись Windows: loopback «%s», %d Гц, %d кан.", self.device_name, rate, channels)

    def command(self, cmd: str) -> None:
        self.writer.set_recording(cmd == "record")

    def status(self) -> dict:
        state = "recording" if self.writer.recording else "paused"
        return {"state": "error" if self.writer.error else state, "seconds": self.writer.seconds,
                "level_db": self.writer.level_db, "error": self.writer.error}

    def stop(self) -> Path:
        try:
            self.stream.stop_stream()
            self.stream.close()
        finally:
            self.pa.terminate()
            self.writer.close()
        return self.session / "audio.wav"


# ─────────────────────────── сессия записи ───────────────────────────

OnSaved = Callable[[Path, float, str], str]  # (m4a, длительность, название) -> id задачи в журнале


class Recorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.source = None
        self.session: Optional[Path] = None
        self.state = "idle"          # idle | starting | recording | paused | saving | saved | error
        self.error: Optional[str] = None
        self.job_id: Optional[str] = None
        self.started: Optional[float] = None
        self.on_saved: Optional[OnSaved] = None

    def status(self) -> dict:
        ok, hint = support()
        with self.lock:
            st = {"supported": ok, "hint": hint, "platform": sys.platform, "state": self.state,
                  "seconds": 0.0, "level_db": -120.0, "error": self.error, "job_id": self.job_id}
            if self.source is not None and self.state in ("starting", "recording", "paused"):
                s = self.source.status()
                st.update(seconds=s.get("seconds", 0.0), level_db=s.get("level_db", -120.0))
                if s.get("error"):
                    st["error"] = s["error"]
                if s.get("state") == "error":
                    st["state"] = "error"
                elif self.state == "starting" and s.get("state") in ("recording", "paused"):
                    self.state = st["state"] = s["state"]
            return st

    def start(self) -> dict:
        ok, reason = support()
        if not ok:
            raise RuntimeError(reason)
        with self.lock:
            if self.state in ("starting", "recording", "paused", "saving"):
                raise RuntimeError("Запись уже идёт")
            self.session = SESSIONS / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
            self.session.mkdir(parents=True, exist_ok=True)
            self.error, self.job_id, self.started = None, None, time.time()
            self.source = MacSource(self.session) if sys.platform == "darwin" else WinSource(self.session)
            self.state = "starting" if sys.platform == "darwin" else "recording"
            log.info("Запись звука начата: %s", self.session.name)
        return self.status()

    def pause(self) -> dict:
        with self.lock:
            if self.state == "recording" and self.source is not None:
                self.source.command("pause")
                self.state = "paused"
        return self.status()

    def resume(self) -> dict:
        with self.lock:
            if self.state == "paused" and self.source is not None:
                self.source.command("record")
                self.state = "recording"
        return self.status()

    def stop(self, title: Optional[str] = None) -> dict:
        with self.lock:
            if self.state not in ("starting", "recording", "paused", "error") or self.source is None:
                raise RuntimeError("Запись не идёт")
            source, session = self.source, self.session
            self.state, self.source = "saving", None
        threading.Thread(target=self._finish, args=(source, session, title), daemon=True, name="listen-save").start()
        return self.status()

    def reset(self) -> None:
        with self.lock:
            if self.state in ("saved", "error"):
                self.state, self.error, self.job_id = "idle", None, None

    def _finish(self, source, session: Path, title: Optional[str]) -> None:
        try:
            wav = source.stop()
            job_id = finalize(session, wav, title or _default_title(self.started), self.on_saved)
            with self.lock:
                self.state, self.job_id = "saved", job_id
        except Exception as e:  # noqa: BLE001
            log.exception("Не удалось сохранить запись")
            with self.lock:
                self.state, self.error = "error", f"Не удалось сохранить запись: {e}"


def _default_title(started: Optional[float]) -> str:
    t = time.localtime(started or time.time())
    months = ["янв.", "февр.", "марта", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."]
    return f"Запись {t.tm_mday} {months[t.tm_mon - 1]} {t.tm_hour:02d}:{t.tm_min:02d}"


def finalize(session: Path, wav: Path, title: str, on_saved: Optional[OnSaved]) -> Optional[str]:
    """WAV → m4a (AAC 128 кбит/с) → запись в журнале; папка сессии удаляется."""
    if not wav.exists() or wav.stat().st_size < 1024:
        shutil.rmtree(session, ignore_errors=True)
        raise RuntimeError("Запись пустая — звук не поступал")
    m4a = session / "audio.m4a"
    proc = subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "128k",
                           str(m4a)], capture_output=True, text=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {proc.stderr[-300:]}")
    with wave.open(str(wav), "rb") as w:
        n, rate = w.getnframes(), w.getframerate()
    # заголовок WAV после аварийного завершения может быть неполным — считаем по размеру файла
    duration = n / rate if n else (wav.stat().st_size - 44) / (rate * 2 * 2)
    job_id = on_saved(m4a, duration, title) if on_saved else None
    shutil.rmtree(session, ignore_errors=True)
    log.info("Запись сохранена: %s, %.1f мин → задача %s", title, duration / 60, job_id)
    return job_id


def recover(on_saved: OnSaved) -> int:
    """Записи, прерванные падением сервера: сохраняем то, что успело записаться."""
    count = 0
    for session in sorted(SESSIONS.glob("*")) if SESSIONS.exists() else []:
        wav = session / "audio.wav"
        if not wav.exists():
            shutil.rmtree(session, ignore_errors=True)
            continue
        if sys.platform == "darwin":  # помощник мог ещё не успеть закрыть файл
            try:
                (session / "control").write_text("stop", encoding="utf-8")
            except OSError:
                pass
            time.sleep(1)
        try:
            try:  # папка сессии названа временем начала: 20260925-120255-xxxxxx
                started = time.mktime(time.strptime(session.name[:15], "%Y%m%d-%H%M%S"))
            except ValueError:
                started = session.stat().st_mtime
            finalize(session, wav, "Восстановленная " + _default_title(started).lower(), on_saved)
            count += 1
        except Exception:  # noqa: BLE001
            log.exception("Не удалось восстановить запись %s", session.name)
    return count


recorder = Recorder()
