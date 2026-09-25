"""Конвейер транскрибации: ffmpeg → Whisper → диаризация (sherpa-onnx) → реплики.

Распознавание — два движка с одинаковой моделью Whisper:
  • MLX (GPU Apple) — на Mac с Apple Silicon;
  • faster-whisper (CTranslate2) — на Windows, Linux и Intel Mac; NVIDIA CUDA, иначе CPU (int8).
"""

from __future__ import annotations

import bisect
import logging
import multiprocessing as mp
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("transkribator.pipeline")

SAMPLE_RATE = 16000
ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"


def _find_ffmpeg() -> str:
    """ffmpeg из пакета imageio-ffmpeg (ставится вместе с приложением на любой ОС), иначе системный."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg") or "ffmpeg"


FFMPEG = _find_ffmpeg()
# на Windows не показывать консольное окно ffmpeg
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _detect_backend() -> str:
    forced = os.environ.get("TRANSKRIBATOR_BACKEND")
    if forced in ("mlx", "faster-whisper"):
        return forced
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "mlx"
    return "faster-whisper"


BACKEND = _detect_backend()
# Одна и та же модель Whisper в форматах двух движков
WHISPER_REPOS = {
    "mlx": {
        "large-v3": "mlx-community/whisper-large-v3-mlx",
        "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
        "tiny": "mlx-community/whisper-tiny-mlx",
    },
    "faster-whisper": {
        "large-v3": "Systran/faster-whisper-large-v3",
        "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
        "tiny": "Systran/faster-whisper-tiny",
    },
}
WHISPER_MODELS = WHISPER_REPOS[BACKEND]
MODEL_LABELS = {
    "large-v3": "Точная (large-v3)",
    "large-v3-turbo": "Быстрая (large-v3 turbo)",
    "tiny": "Тестовая (tiny)",  # только для проверки установки
}
DEFAULT_WHISPER = "large-v3"

SEGMENTATION_MODEL = MODELS_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMBEDDING_MODEL = MODELS_DIR / "3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx"
# Порог косинусной близости для автоопределения числа говорящих
AUTO_CLUSTER_THRESHOLD = 0.6
# Авто-режим: кластеры с таким сходством центроидов считаем одним человеком.
# На реальных записях разные люди дают сходство до ~0.80, один человек,
# разбитый диаризацией на части, — от ~0.9.
SAME_SPEAKER_SIM = 0.88
# Авто-режим: «говорящий» с меньшим объёмом речи на всю запись — почти всегда
# шум эмбеддингов на коротких фрагментах; его отрезки отдаём ближайшему голосу.
MIN_SPEAKER_SEC = 12.0
# Потоки CPU для диаризации. Если Whisper считает на видеокарте (Apple Silicon или NVIDIA) — отдаём
# диаризации до 8 ядер; если на процессоре — распознавание и диаризация делят его пополам.
_CPUS = os.cpu_count() or 4
_GPU_ASR = BACKEND == "mlx" or (os.environ.get("TRANSKRIBATOR_DEVICE") != "cpu" and shutil.which("nvidia-smi") is not None)
THREADS = min(8, _CPUS) if _GPU_ASR else max(2, _CPUS // 2)

# Длинные записи обрабатываются частями — память и время на часть ограничены,
# а сбой одной части не губит всю работу. Резы делаются в самой тихой точке
# рядом с целевой границей, чтобы не разрезать слово.
TRANSCRIBE_CHUNK_SEC = 20 * 60
DIARIZE_CHUNK_SEC = 30 * 60
MIN_CHUNK_SEC = 60  # мельче не дробим: при повторных сбоях — ошибка
CUT_SEARCH_SEC = 15  # в каком окне вокруг границы искать паузу

# Подсказка Whisper: задаёт стиль с пунктуацией и заглавными буквами
RU_PROMPT = "Здравствуйте. Давайте начнём, у нас сегодня несколько вопросов."

# Фразы, которые Whisper «придумывает» на тишине и музыке (наследие обучения на субтитрах)
HALLUCINATIONS = re.compile(
    r"(субтитр\w*\s+(сделал|создавал|делал|подогнал)|редактор\s+субтитров|корректор\s+[А-ЯЁ]\.|"
    r"dimatorzok|продолжение\s+следует|спасибо\s+за\s+просмотр|подписывайтесь\s+на\s+(наш\s+)?канал|"
    r"ставьте\s+лайк|amara\.org)",
    re.IGNORECASE,
)

Progress = Callable[[str, float], None]
Segment = tuple[float, float, int]


@dataclass
class Word:
    start: float
    end: float
    text: str
    speaker: int = -1


@dataclass
class Turn:
    speaker: int
    start: float
    end: float
    text: str


@dataclass
class Result:
    duration: float
    language: str
    turns: list[Turn] = field(default_factory=list)
    num_speakers: int = 0
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- аудио

def load_audio(path: str | Path) -> np.ndarray:
    """Декодирует любой формат (m4a, mp3, wav, ogg, видео…) в 16 кГц моно float32."""
    cmd = [
        FFMPEG, "-nostdin", "-loglevel", "error", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg не смог прочитать файл: {proc.stderr.decode(errors='ignore')[-500:]}")
    audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    if audio.size == 0:
        raise RuntimeError("В файле нет аудиодорожки")
    return audio


def _quietest_point(audio: np.ndarray, target: int, search: int) -> int:
    """Сэмпл с минимальной громкостью (окна по 100 мс) в пределах ±search от target."""
    lo, hi = max(0, target - search), min(len(audio), target + search)
    frame = SAMPLE_RATE // 10
    n = (hi - lo) // frame
    if n < 3:
        return target
    energy = (audio[lo:lo + n * frame].reshape(n, frame) ** 2).mean(1)
    # сглаживаем соседями, чтобы выбрать середину паузы, а не щель между слогами
    energy = np.convolve(energy, np.ones(5) / 5, mode="same")
    return lo + int(energy.argmin()) * frame + frame // 2


def split_points(audio: np.ndarray, chunk_sec: float, start: int = 0, end: Optional[int] = None) -> list[tuple[int, int]]:
    """Делит [start, end) на части ≈chunk_sec, разрезая в паузах. Части покрывают отрезок целиком."""
    end = len(audio) if end is None else end
    chunk = int(chunk_sec * SAMPLE_RATE)
    if end - start <= chunk * 1.25:
        return [(start, end)]
    bounds, pos = [], start
    while end - pos > chunk * 1.25:
        cut = _quietest_point(audio, pos + chunk, CUT_SEARCH_SEC * SAMPLE_RATE)
        bounds.append((pos, cut))
        pos = cut
    bounds.append((pos, end))
    return bounds


# ---------------------------------------------------------------- распознавание

def _transcribe_chunk(audio: np.ndarray, offset: float, model: str, language: Optional[str],
                      on_frac: Callable[[float], None]) -> tuple[list[Word], str]:
    if BACKEND == "mlx":
        segments, lang = _mlx_segments(audio, model, language, on_frac)
    else:
        segments, lang = _fw_segments(audio, model, language, on_frac)
    return _segments_to_words(segments, offset, len(audio) / SAMPLE_RATE), lang


def _segments_to_words(segments: list[dict], offset: float, total: float) -> list[Word]:
    words: list[Word] = []
    for seg in segments:
        if HALLUCINATIONS.search(seg.get("text", "")):
            log.info("Отброшена галлюцинация Whisper @%.1fs: %r", offset + seg.get("start", 0), seg.get("text", "")[:80])
            continue
        seg_words = seg.get("words") or []
        if not seg_words and seg.get("text", "").strip():
            seg_words = [{"start": seg["start"], "end": seg["end"], "word": seg["text"]}]
        for w in seg_words:
            start, end = float(w["start"]), min(float(w["end"]), total)
            words.append(Word(offset + start, offset + max(end, start), w["word"]))
    return words


def _mlx_segments(audio: np.ndarray, model: str, language: Optional[str],
                  on_frac: Callable[[float], None]) -> tuple[list[dict], str]:
    import mlx_whisper

    mt = sys.modules["mlx_whisper.transcribe"]

    class _Tqdm:
        """Подменяет tqdm внутри mlx_whisper, чтобы отдавать прогресс наружу."""

        def __init__(self, total=None, **_):
            self.total, self.n = total or 1, 0

        def update(self, n):
            self.n += n
            on_frac(min(self.n / self.total, 1.0))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    orig = mt.tqdm.tqdm
    mt.tqdm.tqdm = _Tqdm
    try:
        res = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=WHISPER_MODELS.get(model, model),
            language=language or None,
            word_timestamps=True,
            condition_on_previous_text=False,  # меньше «зацикливаний» на длинных записях
            initial_prompt=RU_PROMPT if language == "ru" else None,
            hallucination_silence_threshold=2.0,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            verbose=None,
        )
    finally:
        mt.tqdm.tqdm = orig
    return res.get("segments", []), res.get("language", language or "")


_asr_cache: dict[str, object] = {}


def _setup_cuda_dlls() -> None:
    """Делает библиотеки CUDA/cuDNN из pip-пакетов nvidia-* видимыми для CTranslate2.

    Windows — через os.add_dll_directory; Linux — предзагрузкой .so (LD_LIBRARY_PATH читается
    только при старте процесса, поэтому менять его здесь бесполезно).
    """
    if sys.platform not in ("win32", "linux"):
        return
    import importlib.util

    for pkg in ("nvidia.cuda_runtime", "nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(pkg)
        except (ImportError, ValueError):
            continue
        for base in (spec.submodule_search_locations or []) if spec else []:
            if sys.platform == "win32":
                bin_dir = Path(base) / "bin"
                if bin_dir.is_dir():
                    os.add_dll_directory(str(bin_dir))
                    os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            else:
                import ctypes

                for lib in sorted((Path(base) / "lib").glob("lib*.so*")):
                    try:
                        ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass  # не все .so нужны/загружаемы по отдельности


def _gpu_memory_mb() -> Optional[int]:
    """Объём видеопамяти первой карты NVIDIA (через nvidia-smi), None — если не удалось узнать."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW).stdout
        return int(out.split()[0])
    except Exception:  # noqa: BLE001
        return None


def fw_device() -> tuple[str, str]:
    """(устройство, тип вычислений) для faster-whisper.

    TRANSKRIBATOR_DEVICE=cpu|cuda и TRANSKRIBATOR_COMPUTE_TYPE — принудительно.
    На видеокартах с памятью < 6 ГБ — int8_float16 (≈3 ГБ для large-v3) вместо float16 (≈4,5 ГБ).
    """
    forced = os.environ.get("TRANSKRIBATOR_DEVICE")
    compute = os.environ.get("TRANSKRIBATOR_COMPUTE_TYPE")
    if forced == "cpu":
        return "cpu", compute or "int8"
    _setup_cuda_dlls()
    try:
        import ctranslate2

        has_cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        has_cuda = False
    if forced == "cuda" or has_cuda:
        if not compute:
            vram = _gpu_memory_mb()
            compute = "int8_float16" if vram is not None and vram < 6000 else "float16"
        return "cuda", compute
    return "cpu", compute or "int8"


def _fw_model(model: str):
    if model not in _asr_cache:
        from faster_whisper import WhisperModel

        device, compute = fw_device()
        t0 = time.time()

        def load(dev: str, comp: str):
            return WhisperModel(
                WHISPER_MODELS.get(model, model), device=dev, compute_type=comp,
                cpu_threads=max(2, _CPUS - THREADS) if dev == "cpu" else 4,
            )

        # на видеокарте пробуем от быстрого к экономному (GTX 10xx не умеют эффективный float16),
        # затем процессор: нет cuBLAS/cuDNN, старый драйвер и т.п. — работаем, а не падаем
        attempts = [(device, compute)]
        if device == "cuda":
            attempts += [("cuda", c) for c in ("int8_float16", "int8") if c != compute] + [("cpu", "int8")]
        for i, (dev, comp) in enumerate(attempts):
            try:
                _asr_cache[model] = load(dev, comp)
                device, compute = dev, comp
                break
            except Exception:
                if i == len(attempts) - 1:
                    raise
                log.warning("Модель не загрузилась на %s/%s — пробую %s/%s", dev, comp, *attempts[i + 1], exc_info=True)
        log.info("faster-whisper: модель %s загружена на %s/%s за %.1f с", model, device, compute, time.time() - t0)
    return _asr_cache[model]


def _fw_segments(audio: np.ndarray, model: str, language: Optional[str],
                 on_frac: Callable[[float], None]) -> tuple[list[dict], str]:
    total = len(audio) / SAMPLE_RATE or 1.0
    segments, info = _fw_model(model).transcribe(
        audio,
        language=language or None,
        word_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=RU_PROMPT if language == "ru" else None,
        hallucination_silence_threshold=2.0,
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        beam_size=5,
        vad_filter=False,
    )
    out = []
    for seg in segments:  # генератор: распознавание идёт по мере чтения
        out.append({
            "start": seg.start, "end": seg.end, "text": seg.text,
            "words": [{"start": w.start, "end": w.end, "word": w.word} for w in (seg.words or [])],
        })
        on_frac(min(seg.end / total, 1.0))
    return out, info.language or (language or "")


def transcribe(audio: np.ndarray, model: str, language: Optional[str], progress: Progress,
               chunk_sec: float = TRANSCRIBE_CHUNK_SEC) -> tuple[list[Word], str]:
    """Распознаёт запись частями. Если часть не удалась — делит её пополам в паузе и пробует снова."""
    total = len(audio)
    pending = split_points(audio, chunk_sec)
    done_samples = 0
    words: list[Word] = []
    lang = language or ""
    log.info("Распознавание: %.1f мин, частей %d (по ~%d мин)", total / SAMPLE_RATE / 60, len(pending), chunk_sec // 60)
    while pending:
        s, e = pending.pop(0)
        t0 = time.time()
        try:
            part, detected = _transcribe_chunk(
                audio[s:e], s / SAMPLE_RATE, model, language,
                lambda f: progress("transcribe", (done_samples + f * (e - s)) / total),
            )
        except Exception:
            dur = (e - s) / SAMPLE_RATE
            log.exception("Сбой распознавания части %.1f–%.1f с", s / SAMPLE_RATE, e / SAMPLE_RATE)
            if dur / 2 < MIN_CHUNK_SEC:
                raise
            _clear_mlx_cache()
            halves = split_points(audio, dur / 2, s, e)
            log.warning("Повтор части по кускам: %s", [(round(a / SAMPLE_RATE), round(b / SAMPLE_RATE)) for a, b in halves])
            pending[:0] = halves
            continue
        words += part
        lang = lang or detected
        done_samples += e - s
        log.info("Часть %.1f–%.1f мин готова за %.0f с, слов %d",
                 s / SAMPLE_RATE / 60, e / SAMPLE_RATE / 60, time.time() - t0, len(part))
    return words, lang


def _clear_mlx_cache() -> None:
    if BACKEND != "mlx":
        return
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- диаризация

_cache: dict[str, object] = {}


def _diarizer(threshold: float = AUTO_CLUSTER_THRESHOLD):
    import sherpa_onnx

    key = f"diar:{threshold}"
    if key not in _cache:
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEGMENTATION_MODEL)),
                num_threads=THREADS,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMBEDDING_MODEL), num_threads=THREADS),
            # num_clusters в sherpa-onnx работает ненадёжно, поэтому всегда
            # кластеризуем по порогу, а точное число говорящих добиваем сами
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise RuntimeError("Неверная конфигурация диаризации (проверьте папку models/)")
        _cache[key] = sherpa_onnx.OfflineSpeakerDiarization(config)
    return _cache[key]


def _extractor():
    import sherpa_onnx

    if "emb" not in _cache:
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMBEDDING_MODEL), num_threads=THREADS)
        _cache["emb"] = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
    return _cache["emb"]


def diarize(audio: np.ndarray, num_speakers: int, progress: Progress,
            chunk_sec: float = DIARIZE_CHUNK_SEC) -> list[Segment]:
    """Диаризация частями; метки говорящих затем связываются между частями по голосу."""
    total = len(audio)
    pending = split_points(audio, chunk_sec)
    done_samples = 0
    segments: list[Segment] = []
    label_base = 0
    log.info("Диаризация: %.1f мин, частей %d", total / SAMPLE_RATE / 60, len(pending))
    while pending:
        s, e = pending.pop(0)

        def cb(done, n, s=s, e=e):
            progress("diarize", (done_samples + (e - s) * done / max(n, 1)) / total)
            return 0

        t0 = time.time()
        try:
            result = _diarizer().process(audio[s:e], callback=cb).sort_by_start_time()
        except Exception:
            dur = (e - s) / SAMPLE_RATE
            log.exception("Сбой диаризации части %.1f–%.1f с", s / SAMPLE_RATE, e / SAMPLE_RATE)
            if dur / 2 < MIN_CHUNK_SEC:
                raise
            pending[:0] = split_points(audio, dur / 2, s, e)
            continue
        off = s / SAMPLE_RATE
        # метки каждой части уникальны; одинаковых людей объединит _recluster
        segments += [(off + x.start, off + x.end, label_base + x.speaker) for x in result]
        label_base += 1000
        done_samples += e - s
        log.info("Диаризация части %.1f–%.1f мин: %.0f с, говорящих %d",
                 s / SAMPLE_RATE / 60, e / SAMPLE_RATE / 60, time.time() - t0, len({x.speaker for x in result}))

    found = len({seg[2] for seg in segments})
    if num_speakers > 0 and found != num_speakers:
        segments = _recluster(audio, segments, num_speakers)
    elif num_speakers <= 0 and found > 1:
        segments = _recluster(audio, segments, 0)
    log.info("Диаризация: найдено %d → итог %d говорящих", found, len({seg[2] for seg in segments}))
    return segments


def _window_embeddings(audio: np.ndarray, segments: list[Segment], window: float = 6.0, min_len: float = 0.5):
    """Режет отрезки на окна до `window` секунд и считает для каждого эмбеддинг голоса."""
    units = []
    for s, e, spk in segments:
        k = max(1, round((e - s) / window))
        step = (e - s) / k
        units += [(s + i * step, s + (i + 1) * step, spk) for i in range(k)]
    ext = _extractor()
    emb = np.zeros((len(units), ext.dim), dtype=np.float32)
    has = np.zeros(len(units), dtype=bool)
    for i, (s, e, _) in enumerate(units):
        if e - s < min_len:
            continue
        st = ext.create_stream()
        st.accept_waveform(SAMPLE_RATE, audio[int(s * SAMPLE_RATE):int(e * SAMPLE_RATE)])
        st.input_finished()
        v = np.array(ext.compute(st), dtype=np.float32)
        emb[i], has[i] = v / (np.linalg.norm(v) + 1e-9), True
    return units, emb, has


def _centroids(emb: np.ndarray, labels: np.ndarray, dur: np.ndarray, keys) -> np.ndarray:
    c = np.stack([(emb[labels == k] * dur[labels == k, None]).sum(0) for k in keys])
    return c / (np.linalg.norm(c, axis=1, keepdims=True) + 1e-9)


def _absorb_minor(E: np.ndarray, lab: np.ndarray, dur: np.ndarray) -> np.ndarray:
    """Кластеры с суммарной речью < MIN_SPEAKER_SEC присоединяет к ближайшему по голосу крупному."""
    keys = sorted(set(lab))
    size = {k: dur[lab == k].sum() for k in keys}
    big = [k for k in keys if size[k] >= MIN_SPEAKER_SEC] or [max(size, key=size.get)]
    if len(big) == len(keys):
        return lab
    c_all, c_big = _centroids(E, lab, dur, keys), _centroids(E, lab, dur, big)
    out = lab.copy()
    for pos, k in enumerate(keys):
        if k not in big:
            out[lab == k] = big[int((c_all[pos] @ c_big.T).argmax())]
    log.info("Авто-режим: %d мелких кластеров (<%.0f с речи) присоединены к %d основным голосам",
             len(keys) - len(big), MIN_SPEAKER_SEC, len(big))
    return out


def _recluster(audio: np.ndarray, segments: list[Segment], n: int) -> list[Segment]:
    """Приводит результат диаризации ровно к n говорящим (n=0 — авто).

    Авто: сливаем кластеры, чьи голоса почти совпадают (один человек,
    разбитый на два «говорящих» или попавший в разные части записи).
    Больше кластеров, чем нужно, — последовательно сливаем самые похожие
    (по центроидам эмбеддингов). Меньше — заново кластеризуем окна методом
    Ward (он не выделяет одиночные выбросы в отдельных «говорящих»).
    """
    units, emb, has = _window_embeddings(audio, segments)
    idx = np.flatnonzero(has)
    if len(idx) < max(n, 1):
        return segments
    E = emb[idx]
    dur = np.array([units[i][1] - units[i][0] for i in idx])
    lab = np.array([units[i][2] for i in idx])
    if n <= 0 or len(set(lab)) > n:
        while len(set(lab)) > max(n, 1):
            keys = sorted(set(lab))
            c = _centroids(E, lab, dur, keys)
            sim = c @ c.T
            np.fill_diagonal(sim, -np.inf)
            i, j = np.unravel_index(sim.argmax(), sim.shape)
            if n <= 0 and sim[i, j] < SAME_SPEAKER_SIM:
                break
            lab[lab == keys[j]] = keys[i]
        if n <= 0:
            lab = _absorb_minor(E, lab, dur)
    else:
        from scipy.cluster.hierarchy import fcluster, linkage

        lab = fcluster(linkage(E, method="ward"), n, "maxclust") - 1
    # Уточнение: окна ≥1 с переназначаем к ближайшему итоговому центроиду.
    # Исправляет кластеры, в которых исходная диаризация смешала двух людей.
    reliable = dur >= 1.0
    for _ in range(2):
        keys = sorted(set(lab))
        new = np.array(keys)[(E @ _centroids(E, lab, dur, keys).T).argmax(1)]
        lab = np.where(reliable, new, lab)
    labels = np.full(len(units), -1)
    labels[idx] = lab
    # короткие окна без эмбеддинга — метка ближайшего по времени окна
    mids = np.array([(s + e) / 2 for s, e, _ in units])
    for i in np.flatnonzero(~has):
        labels[i] = labels[idx[np.abs(mids[idx] - mids[i]).argmin()]]
    return [(s, e, int(l)) for (s, e, _), l in zip(units, labels)]


def _diarize_worker(path: str, num_speakers: int, chunk_sec: float, frac, out) -> None:
    from . import logs

    logs.setup("diarize")
    parent = os.getppid()
    try:
        def prog(_stage, f):
            frac.value = f
            if os.getppid() != parent:
                # сервер упал или был убит — не тратим CPU впустую
                log.warning("Родительский процесс исчез — прекращаю диаризацию")
                os._exit(0)

        out.put((True, diarize(load_audio(path), num_speakers, prog, chunk_sec)))
    except Exception as e:  # noqa: BLE001 — передаём текст ошибки в родительский процесс
        log.exception("Ошибка в процессе диаризации")
        out.put((False, repr(e)))


def _run_diarization(path: str, num_speakers: int, chunk_sec: float, transcribe_fn, progress: Progress):
    """Запускает диаризацию в отдельном процессе, пока в этом идёт распознавание.

    В одном процессе они упираются в GIL и выполняются фактически по очереди.
    Возвращает (результат распознавания, сегменты или None, текст ошибки диаризации).
    """
    ctx = mp.get_context("spawn")
    frac, out = ctx.Value("d", 0.0), ctx.Queue()
    proc = ctx.Process(target=_diarize_worker, args=(path, num_speakers, chunk_sec, frac, out), daemon=True)
    proc.start()
    try:
        transcribed = transcribe_fn()
        while True:
            try:
                ok, payload = out.get(timeout=0.5)
                break
            except queue.Empty:
                if not proc.is_alive():
                    # код < 0 — убит сигналом (например, -9 при нехватке памяти)
                    msg = f"процесс диаризации аварийно завершился (код {proc.exitcode})"
                    log.error(msg)
                    return transcribed, None, msg
                progress("diarize", frac.value)
        return transcribed, (payload if ok else None), (None if ok else payload)
    finally:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.kill()


# ---------------------------------------------------------------- сборка реплик

def assign_speakers(words: list[Word], segments: list[Segment]) -> None:
    """Каждому слову — говорящий с максимальным перекрытием, иначе ближайший по времени."""
    if not segments:
        for w in words:
            w.speaker = 0
        return
    segments = sorted(segments)
    starts = [s for s, _, _ in segments]
    max_len = max(e - s for s, e, _ in segments)
    for w in words:
        j = bisect.bisect_right(starts, w.end)
        best, best_ov = -1, 0.0
        k = j - 1
        while k >= 0 and starts[k] >= w.start - max_len:
            s, e, spk = segments[k]
            ov = min(e, w.end) - max(s, w.start)
            if ov > best_ov:
                best, best_ov = spk, ov
            k -= 1
        if best < 0:
            mid = (w.start + w.end) / 2
            # ближайший по времени: соседние по началу сегменты
            near = [x for x in (j - 3, j - 2, j - 1, j) if 0 <= x < len(segments)]
            best = min((segments[x] for x in near), key=lambda sg: min(abs(sg[0] - mid), abs(sg[1] - mid)))[2]
        w.speaker = best
    _smooth(words)


SENTENCE_END = (".", "?", "!", "…")


def _smooth(words: list[Word], pause: float = 1.0, majority: float = 0.7) -> None:
    """Выравнивает смену говорящего по границам фраз.

    Диаризация часто ошибается на доли секунды на стыке реплик, из-за чего
    первое-второе слово новой реплики «прилипает» к предыдущему говорящему.
    Фраза (до знака конца предложения или паузы) почти всегда принадлежит
    одному человеку, поэтому если ≥70 % её звучания — один голос, отдаём ему
    всю фразу. Иначе оставляем пословную разметку (реальная смена внутри фразы).
    """
    start = 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        if last or w.text.strip().endswith(SENTENCE_END) or words[i + 1].start - w.end > pause:
            phrase = words[start:i + 1]
            dur: dict[int, float] = {}
            for x in phrase:
                dur[x.speaker] = dur.get(x.speaker, 0.0) + max(x.end - x.start, 0.05)
            spk, top = max(dur.items(), key=lambda kv: kv[1])
            if top >= majority * sum(dur.values()):
                for x in phrase:
                    x.speaker = spk
            start = i + 1
    # одиночное короткое слово другого говорящего внутри чужой реплики — дребезг
    for i in range(1, len(words) - 1):
        prev, cur, nxt = words[i - 1], words[i], words[i + 1]
        if prev.speaker == nxt.speaker != cur.speaker and cur.end - cur.start < 0.6:
            cur.speaker = prev.speaker


def build_turns(words: list[Word], max_gap: float = 2.5) -> list[Turn]:
    turns: list[Turn] = []
    for w in words:
        if turns and turns[-1].speaker == w.speaker and w.start - turns[-1].end < max_gap:
            t = turns[-1]
            t.text += w.text
            t.end = w.end
        else:
            turns.append(Turn(w.speaker, w.start, w.end, w.text))
    for t in turns:
        t.text = " ".join(t.text.split())
    return [t for t in turns if t.text]


def renumber(turns: list[Turn]) -> int:
    """Нумерует говорящих в порядке первого появления: 1, 2, 3…"""
    mapping: dict[int, int] = {}
    for t in turns:
        if t.speaker not in mapping:
            mapping[t.speaker] = len(mapping) + 1
        t.speaker = mapping[t.speaker]
    return len(mapping)


def run(path: str | Path, *, model: str = DEFAULT_WHISPER, language: Optional[str] = "ru",
        num_speakers: int = 0, progress: Optional[Progress] = None, chunk_scale: float = 1.0) -> Result:
    """num_speakers: 0 — определить автоматически, 1 — без разделения, N — ровно N человек.

    chunk_scale < 1 уменьшает части (используется при повторе после падения).
    """
    progress = progress or (lambda stage, frac: None)
    progress("decode", 0.0)
    t0 = time.time()
    audio = load_audio(path)
    duration = len(audio) / SAMPLE_RATE
    log.info("Файл %s: %.1f мин, декодирован за %.1f с", Path(path).name, duration / 60, time.time() - t0)

    tr_chunk = max(MIN_CHUNK_SEC * 2, TRANSCRIBE_CHUNK_SEC * chunk_scale)
    di_chunk = max(MIN_CHUNK_SEC * 2, DIARIZE_CHUNK_SEC * chunk_scale)
    warnings: list[str] = []

    def do_transcribe():
        return transcribe(audio, model, language, progress, tr_chunk)

    segments: list[Segment] = []
    if num_speakers == 1:
        words, lang = do_transcribe()
    else:
        (words, lang), segs, err = _run_diarization(str(path), num_speakers, di_chunk, do_transcribe, progress)
        if segs is None:
            # текст важнее разметки: отдаём транскрипт без деления на говорящих
            warnings.append(f"Не удалось разделить запись по говорящим ({err}). Текст распознан полностью.")
        else:
            segments = segs

    assign_speakers(words, segments)
    turns = build_turns(words)
    n = renumber(turns)
    progress("done", 1.0)
    log.info("Готово: слов %d, реплик %d, говорящих %d, %.0f с", len(words), len(turns), n, time.time() - t0)
    return Result(duration=duration, language=lang, turns=turns, num_speakers=n, warnings=warnings)


def fmt_time(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def merge_named(turns: list[dict], names: dict[str, str]) -> list[dict]:
    """Склеивает соседние реплики говорящих с одинаковым именем.

    Так пользователь может вручную объединить «двух» говорящих, если
    диаризация разделила одного человека: достаточно дать им одно имя.
    """
    out: list[dict] = []
    for t in turns:
        name = names.get(str(t["speaker"])) or f"Спикер {t['speaker']}"
        if out and out[-1]["name"] == name:
            out[-1] = {**out[-1], "end": t["end"], "text": out[-1]["text"] + " " + t["text"]}
        else:
            out.append({**t, "name": name})
    return out


def to_txt(turns: list[dict], names: dict[str, str], timestamps: bool = True, speakers: bool = True) -> str:
    out = []
    for t in merge_named(turns, names):
        parts = [f"[{fmt_time(t['start'])}]"] if timestamps else []
        if speakers:
            parts.append(f"{t['name']}:")
        out.append(f"{' '.join(parts)}\n{t['text']}\n" if parts else f"{t['text']}\n")
    return "\n".join(out)
