"""Per-recording speaker embeddings, computed once and cached on disk.

Converting audio and embedding it is slow, so each recording's embeddings
(one vector per speaker label) are saved in ``people/embeddings/<id>.json``
and recomputed only when the speaker labelling changes. Plain Python at
import time: numpy and the model are only touched when computing.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from app.utils import paths
from app.utils.constants import PEOPLE_DIR

log = logging.getLogger(__name__)

MIN_SEGMENT_SECONDS = 1.0

_locks: dict[str, threading.Lock] = {}  # one per recording, so it is embedded once at a time
_locks_guard = threading.Lock()


def _dir() -> Path:
    return PEOPLE_DIR / "embeddings"


def _path(session_id: str) -> Path:
    return _dir() / f"{paths.validate_session_id(session_id)}.json"


def speech_seconds(segments: list[dict]) -> dict[str, float]:
    """Per label, the summed length of segments of at least one second."""
    out: dict[str, float] = {}
    for s in segments:
        label = s.get("speaker")
        if not label:
            continue
        length = float(s.get("end") or 0) - float(s.get("start") or 0)
        if length >= MIN_SEGMENT_SECONDS:
            out[label] = out.get(label, 0.0) + length
    return out


def _fingerprint(segments: list[dict]) -> str:
    from app.coaching.runner import labelling_fingerprint

    return labelling_fingerprint(segments)


def _read(session_id: str, segments: list[dict]) -> dict | None:
    try:
        data = json.loads(_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("labelling") != _fingerprint(segments):
        return None
    emb = data.get("embeddings")
    return emb if isinstance(emb, dict) else None


def cached(session_id: str, segments: list[dict]) -> dict[str, list[float]] | None:
    """The cached embeddings for the current labelling; never touches audio."""
    try:
        return _read(session_id, segments)
    except Exception as e:
        log.warning("embedding cache read failed for %s: %s", session_id, e)
        return None


def _write(session_id: str, payload: dict) -> None:
    target = _path(session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(session_id: str, segments: list[dict], audio_path: str | None = None) -> dict[str, list[float]] | None:
    """Cached embeddings, else computed (needs the model and audio) and saved. Never raises.

    Two callers at once for one recording (coaching and the people worker) embed it once:
    the second waits on the recording's lock and then reads the cache. Never call it while
    holding a transcript or voice-store lock.
    """
    try:
        hit = _read(session_id, segments)
        if hit is not None:
            return hit
        with _locks_guard:
            lock = _locks.setdefault(session_id, threading.Lock())
        with lock:
            hit = _read(session_id, segments)
            if hit is not None:
                return hit
            from app.coaching import runner, voice

            if not voice.model_available():
                return None
            audio = runner._resolve_audio(session_id, audio_path)
            if not audio:
                return None
            emb = voice.speaker_embeddings(audio, segments)
            _write(
                session_id,
                {"labelling": _fingerprint(segments), "embeddings": emb, "seconds": speech_seconds(segments)},
            )
            return emb
    except Exception as e:
        log.warning("speaker embeddings failed for %s: %s", session_id, e, exc_info=True)
        return None


def delete(session_id: str) -> None:
    try:
        _path(session_id).unlink(missing_ok=True)
    except Exception as e:
        log.warning("embedding cache delete failed for %s: %s", session_id, e)
