"""Laughter detection on the Mac with the CED-Tiny audio tagger (ONNX, Apache-2.0, mispeech).

The model is downloaded once from Hugging Face at a pinned revision, checked by
size and SHA-256 and kept in Application Support. Audio never leaves the Mac.

Audio is converted to 16 kHz mono and read in chunks, one batch of windows at a
time, so an hour never sits in memory. Each 2.0 s window (0.5 s step) is
loudness-normalised and scored as the highest of the model's laughter classes;
runs of windows at or above the threshold become events. Events longer than
8 s are dropped as steady background (TV, a crowd).

numpy, soundfile and onnxruntime come from the optional ``diarize`` extra, so
they are imported lazily and ``import app.coaching.laughter`` always works.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_REVISION = "ace276d29dd0bb3f3517b0fa8cf300738c409019"
MODEL_URL = f"https://huggingface.co/mispeech/ced-tiny/resolve/{MODEL_REVISION}/model.onnx"
MODEL_SHA256 = "3890f227660be71886ff48633d11a731993e0206db8a486781412c28a0873435"
MODEL_SIZE = 6_681_767
DOWNLOAD_TIMEOUT_S = 60

THRESHOLD = 0.15
MAX_EVENT_S = 8.0
SAMPLE_RATE = 16_000
WINDOW_S = 2.0
STEP_S = 0.5
EDGE_S = 0.5  # an event starts this far into its first window and ends this far before its last ends
BATCH = 64
FRAME = 400  # 25 ms at 16 kHz
TARGET_RMS = 0.1  # -20 dBFS
SILENCE_RMS = 10 ** (-60 / 20)  # windows quieter than -60 dBFS are left as they are
LAUGHTER_CLASSES = range(16, 22)  # Laughter, Baby laughter, Giggle, Snicker, Belly laugh, Chuckle/chortle
MAX_SECONDS = 3 * 3600

REASON_RUNTIME = "Laughter detection needs the speaker model (Settings › Transcription & AI)"
REASON_DOWNLOAD = "Couldn't download the laughter model"
REASON_NO_AUDIO = "No audio on this Mac"
REASON_TOO_LONG = "Too long to check for laughter"
REASON_READ = "Couldn't read the audio"
REASON_FAILED = "Laughter detection didn't run"

_download_lock = threading.Lock()

_WINDOW = int(WINDOW_S * SAMPLE_RATE)
_STEP = int(STEP_S * SAMPLE_RATE)


# --------------------------------------------------------------------- model


def model_path() -> Path:
    from app.mcp import access

    return access.support_dir() / "models" / "ced-tiny.onnx"


def runtime_available() -> bool:
    """True when onnxruntime, numpy and soundfile import (the ``diarize`` extra)."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        import soundfile  # noqa: F401
    except Exception:
        return False
    return True


def _file_ok(path: Path) -> bool:
    try:
        if path.stat().st_size != MODEL_SIZE:
            return False
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest() == MODEL_SHA256
    except OSError:
        return False


def _ssl_context():
    try:
        import ssl

        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _download(dest: Path) -> bool:
    """Fetch the pinned model to a temp file beside ``dest``, check it, then swap it in."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".ced-tiny-", suffix=".part")
    try:
        h = hashlib.sha256()
        size = 0
        with (
            os.fdopen(fd, "wb") as out,
            urllib.request.urlopen(MODEL_URL, timeout=DOWNLOAD_TIMEOUT_S, context=_ssl_context()) as resp,
        ):
            while size <= MODEL_SIZE:
                block = resp.read(1 << 16)
                if not block:
                    break
                out.write(block)
                h.update(block)
                size += len(block)
        if size != MODEL_SIZE or h.hexdigest() != MODEL_SHA256:
            log.warning("laughter model download failed its check (%d bytes)", size)
            return False
        os.replace(tmp, dest)
        tmp = None
        return True
    except Exception as e:
        log.warning("laughter model download failed: %s", e)
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def ensure_model() -> tuple[bool, str]:
    """Make sure the checked model file is present, downloading it if needed.

    Returns ``(True, "")`` or ``(False, reason)``. A file that fails its size or
    SHA-256 check is deleted and fetched again. One download at a time."""
    path = model_path()
    with _download_lock:
        if _file_ok(path):
            return True, ""
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not remove a bad laughter model: %s", e)
        if _download(path) and _file_ok(path):
            return True, ""
        try:
            path.unlink()
        except OSError:
            pass
        return False, REASON_DOWNLOAD


def _session():
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = min(4, os.cpu_count() or 1)
    return ort.InferenceSession(str(model_path()), opts, providers=["CPUExecutionProvider"])


# ----------------------------------------------------------------- windowing


def window_count(samples: int) -> int:
    """Windows needed to cover ``samples``; the last one is zero-padded."""
    if samples <= 0:
        return 0
    if samples <= _WINDOW:
        return 1
    return -(-(samples - _WINDOW) // _STEP) + 1


def iter_windows(wav_path: str):
    """Yield ``(windows, start_seconds)`` batches of up to BATCH windows from a 16 kHz WAV.

    Each batch reads only the samples it covers, so memory stays bounded."""
    import numpy as np
    import soundfile as sf

    total = sf.info(wav_path).frames
    n = window_count(total)
    offsets = np.arange(_WINDOW)[None, :]
    for first in range(0, n, BATCH):
        count = min(BATCH, n - first)
        start = first * _STEP
        frames = (count - 1) * _STEP + _WINDOW
        chunk, _ = sf.read(wav_path, start=start, frames=min(frames, total - start), dtype="float32")
        if chunk.ndim > 1:
            chunk = chunk.mean(axis=1)
        if len(chunk) < frames:
            chunk = np.pad(chunk, (0, frames - len(chunk)))
        idx = offsets + _STEP * np.arange(count)[:, None]
        starts = (first + np.arange(count)) * STEP_S
        yield np.ascontiguousarray(chunk[idx], dtype=np.float32), starts


def normalise(windows):
    """Scale each window so its loudest 25 ms frame is at -20 dBFS RMS.

    Windows whose loudest frame is under -60 dBFS are left as they are.
    Returns ``(scaled windows, raw loudest-frame dBFS per window)``."""
    import numpy as np

    frames = windows.reshape(len(windows), -1, FRAME)
    peak = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=2)).max(axis=1)
    gain = np.where(peak >= SILENCE_RMS, TARGET_RMS / np.maximum(peak, 1e-12), 1.0)
    raw_db = 20 * np.log10(np.maximum(peak, 1e-10))
    return (windows * gain[:, None]).astype(np.float32), raw_db


def laughter_scores(probs):
    """Per window, the highest probability among the laughter classes."""
    return probs[:, LAUGHTER_CLASSES.start : LAUGHTER_CLASSES.stop].max(axis=1)


def events_from_scores(scores, loudness_db, starts, duration: float | None = None) -> list[dict]:
    """Merge consecutive windows at or above THRESHOLD into events, dropping long ones."""
    events: list[dict] = []
    n = len(scores)
    i = 0
    while i < n:
        if scores[i] < THRESHOLD:
            i += 1
            continue
        j = i
        while j + 1 < n and scores[j + 1] >= THRESHOLD:
            j += 1
        start = float(starts[i]) + EDGE_S
        end = float(starts[j]) + WINDOW_S - EDGE_S
        if duration is not None:
            end = min(end, duration)
        if end - start <= MAX_EVENT_S:
            events.append(
                {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "score": round(float(max(scores[i : j + 1])), 3),
                    "loudness_db": round(float(max(loudness_db[i : j + 1])), 1),
                }
            )
        i = j + 1
    return events


# -------------------------------------------------------------------- detect


def _unavailable(reason: str) -> dict:
    return {"status": "unavailable", "events": [], "reason": reason}


def detect(audio_path: str, max_seconds: float = MAX_SECONDS) -> dict:
    """Laughter events in a recording. Never raises.

    Downloads the model on first need (only when the runtime is installed).
    Returns ``{"status": "done", "events": [...], "reason": ""}`` or
    ``{"status": "unavailable", "events": [], "reason": str}``."""
    try:
        if not runtime_available():
            return _unavailable(REASON_RUNTIME)
        if not audio_path or not os.path.isfile(audio_path):
            return _unavailable(REASON_NO_AUDIO)
        ok, reason = ensure_model()
        if not ok:
            return _unavailable(reason)
    except Exception as e:
        log.warning("laughter detection could not start: %s", e)
        return _unavailable(REASON_FAILED)

    wav = None
    try:
        import numpy as np
        import soundfile as sf

        from src.transcribe import _to_wav16k

        wav = _to_wav16k(audio_path)
        if not wav:
            return _unavailable(REASON_READ)
        info = sf.info(wav)
        if info.samplerate != SAMPLE_RATE:
            return _unavailable(REASON_READ)
        duration = info.frames / info.samplerate
        if duration > max_seconds:
            return _unavailable(REASON_TOO_LONG)

        session = _session()
        input_name = session.get_inputs()[0].name
        scores, loudness, starts = [], [], []
        for windows, batch_starts in iter_windows(wav):
            scaled, raw_db = normalise(windows)
            probs = np.asarray(session.run(None, {input_name: scaled})[0])
            if probs.ndim != 2 or probs.shape[1] < LAUGHTER_CLASSES.stop:
                raise ValueError(f"unexpected model output shape {probs.shape}")
            scores.append(laughter_scores(probs))
            loudness.append(raw_db)
            starts.append(batch_starts)
        if not scores:
            return {"status": "done", "events": [], "reason": ""}
        events = events_from_scores(
            np.concatenate(scores), np.concatenate(loudness), np.concatenate(starts), duration=duration
        )
        return {"status": "done", "events": events, "reason": ""}
    except Exception as e:
        log.warning("laughter detection failed: %s", e, exc_info=True)
        return _unavailable(REASON_FAILED)
    finally:
        if wav:
            try:
                os.unlink(wav)
            except OSError:
                pass
