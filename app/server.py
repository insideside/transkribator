"""Веб-сервер транскрибатора: загрузка файлов, очередь, история, выгрузка TXT."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import logs, models, pipeline, updater

log = logs.setup("server")

DATA = pipeline.ROOT / "data"
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "history.sqlite3"
STATIC = Path(__file__).parent / "static"

UPLOADS.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored TEXT NOT NULL,
    created REAL NOT NULL,
    status TEXT NOT NULL,          -- queued | processing | done | error
    stage TEXT DEFAULT '',
    progress REAL DEFAULT 0,
    model TEXT NOT NULL,
    language TEXT,
    speakers_req INTEGER DEFAULT 0,
    speakers INTEGER DEFAULT 0,
    duration REAL DEFAULT 0,
    elapsed REAL DEFAULT 0,
    error TEXT,
    turns TEXT,                    -- JSON: [{speaker, start, end, text}]
    names TEXT DEFAULT '{}',       -- JSON: {"1": "Иван"}
    warning TEXT,                  -- предупреждение (например, не удалось разделить по говорящим)
    attempts INTEGER DEFAULT 0     -- сколько раз начиналась обработка
);
"""
# после скольких падений сервера на одном файле прекращать попытки
MAX_ATTEMPTS = 3

LIST_COLS = ("id, title, filename, created, status, stage, progress, model, language, speakers_req, speakers, "
             "duration, elapsed, error, warning")


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


with db() as _c:
    _c.executescript(SCHEMA)
    _cols = {r["name"] for r in _c.execute("PRAGMA table_info(jobs)")}
    for _col, _decl in (("warning", "TEXT"), ("attempts", "INTEGER DEFAULT 0")):
        if _col not in _cols:
            _c.execute(f"ALTER TABLE jobs ADD COLUMN {_col} {_decl}")
    # Задачи, прерванные остановкой или падением сервера: повторяем (мельче нарезая),
    # а если сервер падал на файле уже несколько раз — помечаем ошибкой, чтобы не зациклиться.
    for _r in _c.execute("SELECT id, title, attempts FROM jobs WHERE status='processing'").fetchall():
        if _r["attempts"] >= MAX_ATTEMPTS:
            log.error("Задача %s (%s): %d прерванных попыток — помечаю ошибкой", _r["id"], _r["title"], _r["attempts"])
            _c.execute("UPDATE jobs SET status='error', error=? WHERE id=?",
                       (f"Обработка прерывалась {_r['attempts']} раза (сервер останавливался или падал). "
                        "Подробности в data/logs/app.log и crash.log. Нажмите «Повторить», чтобы попробовать снова.",
                        _r["id"]))
        else:
            log.warning("Задача %s (%s) была прервана — возвращаю в очередь", _r["id"], _r["title"])
            _c.execute("UPDATE jobs SET status='queued', stage='', progress=0 WHERE id=?", (_r["id"],))


# ---------------------------------------------------------------- очередь

_wake = threading.Event()


def _update(job_id: str, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def _process(row: sqlite3.Row) -> None:
    job_id = row["id"]
    attempt = (row["attempts"] or 0) + 1
    # после прерванной попытки режем запись на вдвое более мелкие части
    chunk_scale = 0.5 ** (attempt - 1)
    _update(job_id, status="processing", stage="decode", progress=0.0, attempts=attempt, warning=None)
    log.info("Старт задачи %s «%s»: модель=%s язык=%s говорящих=%s попытка=%d размер=%.1f МБ",
             job_id, row["title"], row["model"], row["language"] or "авто", row["speakers_req"] or "авто",
             attempt, (UPLOADS / row["stored"]).stat().st_size / 1e6)
    last = [0.0]

    def progress(stage: str, frac: float) -> None:
        # общая шкала: распознавание 2–85 %, ожидание диаризации 85–99 %
        total = {"decode": 0.0, "transcribe": 0.02 + 0.83 * frac, "diarize": 0.85 + 0.14 * frac, "done": 1.0}.get(stage, 0)
        now = time.time()
        if now - last[0] > 0.7 or stage == "done":
            last[0] = now
            _update(job_id, stage=stage, progress=round(total, 4))

    t0 = time.time()
    try:
        res = pipeline.run(
            UPLOADS / row["stored"],
            model=row["model"],
            language=row["language"] or None,
            num_speakers=row["speakers_req"],
            progress=progress,
            chunk_scale=chunk_scale,
        )
        _update(
            job_id, status="done", stage="done", progress=1.0,
            speakers=res.num_speakers, duration=res.duration, language=res.language,
            elapsed=time.time() - t0, turns=json.dumps([asdict(t) for t in res.turns], ensure_ascii=False),
            warning="\n".join(res.warnings) or None,
        )
        log.info("Задача %s готова за %.0f с (запись %.1f мин)", job_id, time.time() - t0, res.duration / 60)
    except Exception as e:  # noqa: BLE001
        log.exception("Задача %s завершилась ошибкой", job_id)
        _update(job_id, status="error", error=f"{e}\n(подробности: data/logs/app.log)", elapsed=time.time() - t0)


def _next_job() -> Optional[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()


def _worker() -> None:
    log.info("Обработчик очереди запущен")
    while True:
        try:
            row = _next_job()
            if row is None:
                _wake.wait(timeout=5)
                _wake.clear()
                continue
            if not (UPLOADS / row["stored"]).exists():
                _update(row["id"], status="error", error="Исходный файл не найден")
                continue
            _process(row)
        except Exception:  # noqa: BLE001 — обработчик очереди не должен умирать
            log.exception("Сбой в обработчике очереди")
            time.sleep(5)


# ---------------------------------------------------------------- API

app = FastAPI(title="Транскрибатор")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("Ошибка при обработке запроса %s %s", request.method, request.url.path)
    return JSONResponse({"detail": f"Внутренняя ошибка сервера: {exc}"}, status_code=500)


@app.get("/api/health")
def health():
    """Для виджета: жив ли сервер и что сейчас обрабатывается."""
    with db() as c:
        cur = c.execute("SELECT id, title, stage, progress FROM jobs WHERE status='processing' LIMIT 1").fetchone()
        queued = c.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
    return {"ok": True, "current": dict(cur) if cur else None, "queued": queued,
            "version": updater.current_version()["hash"]}


@app.get("/api/update/check")
def update_check(force: bool = False):
    """Есть ли новые коммиты на GitHub. Результат кэшируется на 30 мин; force=1 — проверить сейчас."""
    return updater.check(fetch=True, force=force)


@app.post("/api/update/apply")
def update_apply():
    with db() as c:
        busy = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('processing','queued')").fetchone()[0]
    if busy:
        raise HTTPException(409, "Сейчас идёт расшифровка — обновите приложение, когда она закончится")
    try:
        result = updater.apply()
    except updater.UpdateError as e:
        log.error("Обновление не удалось: %s", e)
        raise HTTPException(400, str(e)) from e
    if result.get("updated"):
        updater.restart_soon()
    return {**result, "restarting": bool(result.get("updated"))}


class ClientLog(BaseModel):
    message: str
    source: Optional[str] = None
    stack: Optional[str] = None


@app.post("/api/client-log")
def client_log(entry: ClientLog):
    """Ошибки JavaScript из браузера попадают в общий лог."""
    log.error("Ошибка в браузере: %s | %s\n%s", entry.message[:500], entry.source or "", (entry.stack or "")[:2000])
    return {"ok": True}


@app.on_event("startup")
def _start_worker() -> None:
    threading.Thread(target=_worker, daemon=True, name="transcribe-worker").start()


def _job_or_404(job_id: str, cols: str = "*") -> sqlite3.Row:
    with db() as c:
        row = c.execute(f"SELECT {cols} FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Транскрибация не найдена")
    return row


@app.get("/api/config")
def config():
    """Модели, которые реально скачаны, и сведения о движке распознавания."""
    installed = [m for m in pipeline.WHISPER_MODELS if models.whisper_installed(m)]
    visible = [m for m in ("large-v3", "large-v3-turbo") if m in installed] or installed
    default = pipeline.DEFAULT_WHISPER if pipeline.DEFAULT_WHISPER in visible else (visible[0] if visible else None)
    device = "gpu-apple" if pipeline.BACKEND == "mlx" else pipeline.fw_device()[0]
    return {
        "models": [{"id": m, "label": pipeline.MODEL_LABELS.get(m, m)} for m in visible],
        "default_model": default,
        "backend": pipeline.BACKEND,
        "device": device,
        "diarization": models.diarization_ready(),
    }


@app.post("/api/jobs")
def create_jobs(
    files: list[UploadFile] = File(...),
    model: str = Form(pipeline.DEFAULT_WHISPER),
    language: str = Form("ru"),
    num_speakers: int = Form(0),
):
    if model not in pipeline.WHISPER_MODELS:
        raise HTTPException(400, "Неизвестная модель")
    num_speakers = max(0, min(num_speakers, 20))
    created = []
    for f in files:
        job_id = uuid.uuid4().hex[:12]
        suffix = Path(f.filename or "").suffix.lower()[:10]
        stored = f"{job_id}{suffix}"
        with open(UPLOADS / stored, "wb") as out:
            shutil.copyfileobj(f.file, out, length=1024 * 1024)
        title = Path(f.filename or "Запись").stem
        with db() as c:
            c.execute(
                "INSERT INTO jobs (id, title, filename, stored, created, status, model, language, speakers_req)"
                " VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (job_id, title, f.filename or stored, stored, time.time(), model, language or "", num_speakers),
            )
        created.append(job_id)
        log.info("Загружен файл «%s» → задача %s", f.filename, job_id)
    _wake.set()
    return {"ids": created}


@app.get("/api/jobs")
def list_jobs():
    with db() as c:
        rows = c.execute(f"SELECT {LIST_COLS} FROM jobs ORDER BY created DESC").fetchall()
        queued = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created")]
    out = []
    for r in rows:
        d = dict(r)
        d["queue_pos"] = queued.index(r["id"]) + 1 if r["id"] in queued else 0
        out.append(d)
    return out


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    d = dict(_job_or_404(job_id))
    d["turns"] = json.loads(d["turns"]) if d["turns"] else []
    d["names"] = json.loads(d["names"] or "{}")
    d.pop("stored")
    return d


class JobPatch(BaseModel):
    title: Optional[str] = None
    names: Optional[dict[str, str]] = None


@app.patch("/api/jobs/{job_id}")
def patch_job(job_id: str, patch: JobPatch):
    _job_or_404(job_id, "id")
    if patch.title is not None:
        _update(job_id, title=patch.title.strip() or "Без названия")
    if patch.names is not None:
        names = {k: v.strip() for k, v in patch.names.items() if v and v.strip()}
        _update(job_id, names=json.dumps(names, ensure_ascii=False))
    return {"ok": True}


class RetryOptions(BaseModel):
    num_speakers: Optional[int] = None
    model: Optional[str] = None


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, opts: Optional[RetryOptions] = None):
    """Повтор задачи; можно сменить число говорящих и модель («пересчитать с N голосами»)."""
    row = _job_or_404(job_id, "id, status, speakers_req, model")
    if row["status"] == "processing":
        raise HTTPException(409, "Запись сейчас обрабатывается")
    fields = dict(status="queued", stage="", progress=0, error=None, attempts=0)
    if opts and opts.num_speakers is not None and opts.num_speakers != row["speakers_req"]:
        fields["speakers_req"] = max(0, min(opts.num_speakers, 20))
        fields["names"] = "{}"  # нумерация говорящих изменится — старые имена не подойдут
    if opts and opts.model:
        if opts.model not in pipeline.WHISPER_MODELS:
            raise HTTPException(400, "Неизвестная модель")
        fields["model"] = opts.model
    _update(job_id, **fields)
    log.info("Повтор задачи %s: %s", job_id, {k: v for k, v in fields.items() if k in ("speakers_req", "model")})
    _wake.set()
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    row = _job_or_404(job_id, "stored, status")
    if row["status"] == "processing":
        raise HTTPException(409, "Нельзя удалить запись, которая сейчас обрабатывается")
    (UPLOADS / row["stored"]).unlink(missing_ok=True)
    log.info("Удалена задача %s", job_id)
    with db() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return {"ok": True}


@app.get("/api/jobs/{job_id}/audio")
def job_audio(job_id: str):
    row = _job_or_404(job_id, "stored")
    path = UPLOADS / row["stored"]
    if not path.exists():
        raise HTTPException(404, "Аудиофайл не найден")
    media = {".m4a": "audio/mp4", ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
             ".ogg": "audio/ogg", ".opus": "audio/ogg", ".webm": "audio/webm", ".flac": "audio/flac",
             ".aac": "audio/aac", ".mov": "video/quicktime"}.get(path.suffix.lower())
    return FileResponse(path, media_type=media)


@app.get("/api/jobs/{job_id}/txt")
def job_txt(job_id: str, timestamps: bool = True, speakers: bool = True):
    row = _job_or_404(job_id, "title, status, turns, names")
    if row["status"] != "done":
        raise HTTPException(409, "Транскрибация ещё не готова")
    text = pipeline.to_txt(json.loads(row["turns"]), json.loads(row["names"] or "{}"), timestamps, speakers)
    fname = f"{row['title']}.txt"
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"transcript.txt\"; filename*=UTF-8''{quote(fname)}"},
    )


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
