"""Coaching runner: decide whether a recording can be coached, then measure and review it.

Runs after transcription and speaker labels are final (the ``transcribe`` webhook points
in app/routes/transcribe.py), in a daemon thread. The outcome is written to the coaching
sidecar as ``status``: ``running``, then ``done``, ``needs_owner``, ``skipped`` or ``failed``.

Who the owner is, in order: the user's own pick for this recording, a confident voice
match, otherwise ``needs_owner`` with every speaker's first line. Nothing is analysed
against a guessed owner. The voice model is only used if already installed; this module
never installs anything.

A result is dropped if the transcript text changed while the run was going, or if a newer
run for the same recording started meanwhile.
"""

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock, Thread

from app.coaching import store, voice
from app.coaching.metrics import compute_metrics
from app.coaching.review import review_conversation
from app.coaching.targets import MIN_COACHING_SECONDS
from app.utils.config import load_config
from app.utils.constants import TRANSCRIPTS_DIR
from app.utils.transcript_locks import get_transcript_lock

log = logging.getLogger(__name__)

NO_LABELS = "no speaker labels"
ONLY_YOU = "only you spoke"
TOO_SHORT = "too short"
NO_AUDIO = "no audio file"
LOCAL_NEEDS_OLLAMA = "AI feedback needs Ollama; showing measured metrics only"
FIRST_LINE_CHARS = 200

# Keys that describe one run's outcome; cleared when a run ends without a report.
_REPORT_KEYS = ("metrics", "review", "report_stale", "candidates")

_lock = Lock()
_running: set[str] = set()  # sessions with a background run in this process
_generation: dict[str, int] = {}  # latest run number per session; older runs drop their result


# ---------------------------------------------------------------- inputs


def _read_transcript(session_id: str) -> dict | None:
    path = TRANSCRIPTS_DIR / f"{session_id}.json"
    try:
        with get_transcript_lock(session_id):
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _find_audio(session_id: str) -> str | None:
    from app.routes.recordings import _find_recording

    path = _find_recording(session_id)
    return str(path) if path else None


def _resolve_audio(session_id: str, audio_path: str | None) -> str | None:
    if audio_path and os.path.isfile(audio_path):
        return audio_path
    return _find_audio(session_id)


def _speaker_labels(segments: list[dict]) -> list[str]:
    """Speaker labels in order of first appearance."""
    seen: dict[str, None] = {}
    for s in segments:
        if s.get("speaker"):
            seen.setdefault(s["speaker"], None)
    return list(seen)


def _first_lines(segments: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in segments:
        label = s.get("speaker")
        text = (s.get("text") or "").strip()
        if label and text and label not in out:
            out[label] = text[:FIRST_LINE_CHARS]
    return out


def _duration(transcript: dict, segments: list[dict]) -> float:
    try:
        duration = float(transcript.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return duration or max((float(s.get("end") or 0) for s in segments), default=0.0)


# ---------------------------------------------------------------- analysis


def _resolve_owner(sidecar: dict, labels: list[str], audio: str | None, segments: list[dict]):
    """Return ``(owner, reason)``: owner is None when the user must pick, with reason saying why."""
    saved = sidecar.get("owner") or {}
    saved_labels = saved.get("labels") or []
    if saved.get("source") == "user" and saved_labels and set(saved_labels) <= set(labels):
        return saved, ""
    if not audio:
        return None, NO_AUDIO
    match = voice.match_owner(audio, segments)
    if match.get("labels"):
        return {"labels": list(match["labels"]), "source": "voice", "confidence": float(match["confidence"])}, ""
    return None, match.get("reason") or "no confident match"


def _candidates(labels: list[str], segments: list[dict], audio: str | None) -> list[dict]:
    loud = voice.loudest_label(audio, segments) if audio and voice.model_available() else None
    lines = _first_lines(segments)
    return [{"label": label, "first_line": lines.get(label, ""), "loud": label == loud} for label in labels]


def _review(segments, owner_labels, metrics, config) -> tuple[dict | None, list[str]]:
    try:
        result = review_conversation(segments, owner_labels, metrics, config)
    except Exception as e:
        log.warning("coaching review failed: %s", e, exc_info=True)
        return None, [str(e)]
    return result.get("review"), list(result.get("errors") or [])


def _analyse(sidecar: dict, transcript: dict, audio: str | None, force: bool, config: dict) -> tuple[dict, set]:
    """Work out this run's outcome. Returns (fields to set, keys to remove) for the sidecar."""
    segments = transcript.get("segments") or []
    labels = _speaker_labels(segments)
    if not labels:
        return {"status": "skipped", "status_reason": NO_LABELS}, set(_REPORT_KEYS)
    if len(labels) == 1:
        return {"status": "skipped", "status_reason": ONLY_YOU}, set(_REPORT_KEYS)
    duration = _duration(transcript, segments)
    if not force and duration < MIN_COACHING_SECONDS:
        return {"status": "skipped", "status_reason": TOO_SHORT}, set(_REPORT_KEYS)

    owner, reason = _resolve_owner(sidecar, labels, audio, segments)
    if owner is None:
        fields = {"status": "needs_owner", "status_reason": reason, "candidates": _candidates(labels, segments, audio)}
        return fields, {"owner", "metrics", "review", "report_stale"}
    owner_labels = sorted(owner["labels"])
    if set(owner_labels) >= set(labels):
        return {"status": "skipped", "status_reason": ONLY_YOU, "owner": owner}, set(_REPORT_KEYS)

    words = sidecar.get("words")
    metrics = compute_metrics(
        segments,
        words if isinstance(words, list) and words else None,
        set(owner_labels),
        duration,
        bool(transcript.get("timestamps_estimated")),
    )
    review, errors = _review(segments, owner_labels, metrics, config)
    fields = {"owner": owner, "metrics": metrics, "review": review, "status": "done", "status_reason": ""}
    if review is None:
        if config.get("summarization_provider") == "local":
            fields["status_reason"] = LOCAL_NEEDS_OLLAMA
        else:
            fields["status"] = "failed"
            fields["status_reason"] = "AI feedback failed: " + ("; ".join(errors) or "no AI provider answered")
    return fields, {"report_stale", "candidates"}


# ---------------------------------------------------------------- public API


def run_coaching(session_id: str, audio_path: str | None, force: bool = False) -> dict:
    """Coach one recording now (synchronously) and return its sidecar.

    ``force`` (the "Coach this recording" action) ignores the ``coaching_auto`` setting and
    the minimum length; it never coaches a recording without speaker labels or where only
    the owner spoke. Without a transcript nothing is written.
    """
    config = load_config()
    if not force and not config.get("coaching_auto", True):
        return store.load(session_id)
    transcript = _read_transcript(session_id)
    if transcript is None:
        return store.load(session_id)
    start_hash = store.text_hash(transcript.get("text"))
    with _lock:
        generation = _generation.get(session_id, 0) + 1
        _generation[session_id] = generation

    before: dict = {}

    def _mark_running(d: dict) -> None:
        before.update(status=d.get("status"), status_reason=d.get("status_reason"))
        d.update(status="running", status_reason="", mixed_up=bool(d.get("mixed_up", False)))

    sidecar = store.update(session_id, _mark_running)
    try:
        audio = _resolve_audio(session_id, audio_path)
        fields, drop = _analyse(sidecar, transcript, audio, force, config)
    except Exception as e:
        log.warning("coaching run for %s failed: %s", session_id, e, exc_info=True)
        fields, drop = {"status": "failed", "status_reason": f"Coaching failed: {e}"}, set()

    return store.update(session_id, _commit(session_id, generation, start_hash, before, fields, drop))


def _commit(session_id, generation, start_hash, before, fields, drop) -> Callable[[dict], None]:
    """The sidecar mutation that saves a run's outcome, unless the run is out of date.

    Runs under the sidecar lock. Re-reading the transcript there is safe (nothing takes the
    sidecar lock while holding a transcript lock) and closes the gap with ``mark_stale``: a
    transcript write after this check marks the new report stale once the lock is released.
    """

    def mutate(d: dict) -> None:
        with _lock:
            superseded = _generation.get(session_id) != generation
        if superseded:
            log.info("coaching run for %s superseded by a newer run; result dropped", session_id)
            return
        current = _read_transcript(session_id)
        if current is None or store.text_hash(current.get("text")) != start_hash:
            log.info("transcript %s changed while coaching ran; result dropped", session_id)
            if before["status"] in (None, "running"):
                d.pop("status", None)
                d.pop("status_reason", None)
            else:
                d["status"], d["status_reason"] = before["status"], before["status_reason"]
            return
        for key in drop:
            d.pop(key, None)
        d.update(fields)
        d["mixed_up"] = bool(d.get("mixed_up", False))
        d["transcript_text_hash"] = start_hash
        d["created_at"] = datetime.now(UTC).isoformat(timespec="seconds")

    return mutate


def _run_in_background(session_id: str, audio_path: str | None, force: bool) -> None:
    try:
        run_coaching(session_id, audio_path, force=force)
    except Exception as e:
        log.warning("background coaching for %s failed: %s", session_id, e, exc_info=True)
    finally:
        with _lock:
            _running.discard(session_id)


def start_coaching_async(session_id: str, audio_path: str | None, force: bool = False) -> None:
    """Coach a recording in a daemon thread.

    Does nothing when automatic coaching is off (unless ``force``) or when this recording
    is already being coached in this process.
    """
    if not force and not load_config().get("coaching_auto", True):
        return
    with _lock:
        if session_id in _running:
            return
        _running.add(session_id)
    try:
        Thread(
            target=_run_in_background,
            kwargs={"session_id": session_id, "audio_path": audio_path, "force": force},
            daemon=True,
        ).start()
    except BaseException:
        with _lock:
            _running.discard(session_id)
        raise


def set_owner(session_id: str, labels: list[str], learn: bool = True) -> dict:
    """Save the user's pick of which speaker label(s) are them, then coach the recording again.

    Raises ValueError for an empty pick or a label not in the transcript. With ``learn``,
    the pick also teaches the voice profile, but only if the voice model is already
    installed and the recording's audio is on this Mac.
    """
    transcript = _read_transcript(session_id)
    if transcript is None:
        raise ValueError("no transcript for this recording")
    if not isinstance(labels, list) or not labels or not all(isinstance(x, str) for x in labels):
        raise ValueError("pick at least one speaker")
    segments = transcript.get("segments") or []
    chosen = sorted(set(labels))
    unknown = [x for x in chosen if x not in set(_speaker_labels(segments))]
    if unknown:
        raise ValueError(f"unknown speaker label: {', '.join(unknown)}")

    store.update(session_id, lambda d: d.update(owner={"labels": chosen, "source": "user", "confidence": 1.0}))
    audio = _resolve_audio(session_id, None)
    if learn and audio and voice.model_available():
        try:
            voice.add_exemplars(audio, segments, chosen)
        except Exception as e:
            log.warning("could not add voice exemplars for %s: %s", session_id, e, exc_info=True)
    return run_coaching(session_id, audio, force=True)
