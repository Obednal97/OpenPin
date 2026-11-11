"""Coaching runner: decide whether a recording can be coached, then measure and review it.

Runs after transcription and speaker labels are final (the ``transcribe`` webhook points
in app/routes/transcribe.py), in a daemon thread. The outcome is written to the coaching
sidecar as ``status``: ``running``, then ``done``, ``needs_owner``, ``skipped`` or ``failed``.
A recording the user excluded ("Don't coach this recording") has ``excluded: true`` and
status ``excluded``: no run starts for it and a run already going drops its result.

Who the owner is, in order: the user's own pick for this recording, a confident voice
match, otherwise ``needs_owner`` with every speaker's first line. Nothing is analysed
against a guessed owner. The voice model is only used if already installed; this module
never installs anything.

With ``coaching_ai_review`` off, the AI review is never asked for: the run ends ``done``
with the measured metrics only.

Every run with a report also stores ``engagement`` (app/coaching/engagement.py): laughter is
detected before the review (so the review is told about the laughs), and the block is
computed after it (so "built on" can use the review's reactions). Its three parts are copied
into ``metrics.owner`` so Progress tracks them like any other measure. Laughter detection
never stops a run: if it can't run, that part is "unavailable" with a reason.

Coaching a finished report again never costs it its AI review: if the new review fails (a
provider error, a bad key, Ollama not running), the previous review is kept, with status
``done`` and ``REVIEW_KEPT``, as long as it was made for the same transcript and the same
owner. The new metrics and engagement are saved either way.

A result is dropped if the transcript text changed while the run was going, if a newer
run for the same recording started meanwhile, or if the recording was excluded meanwhile.
A trigger that arrives during a run is queued and runs once the current run finishes.

"Coach them again" (Progress) re-runs every finished report saved before Engagement existed
(``start_rerun_all``): one recording at a time on one daemon thread, each through
``run_coaching(sid, None, force=True)``, so the rules above apply to each. One bulk run at a
time; cancelling stops it after the recording in progress.
"""

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock, Thread

from app.coaching import engagement, laughter, store, voice
from app.coaching.metrics import compute_metrics
from app.coaching.review import review_conversation
from app.coaching.targets import LAUGHTER_MAX_S, MIN_COACHING_SECONDS
from app.providers.llm_json import ollama_unavailable
from app.utils.config import load_config
from app.utils.constants import TRANSCRIPTS_DIR
from app.utils.paths import InvalidSessionIdError, validate_session_id
from app.utils.transcript_locks import get_transcript_lock

log = logging.getLogger(__name__)

NO_LABELS = "no speaker labels"
ONLY_YOU = "only you spoke"
TOO_SHORT = "too short"
NO_AUDIO = "no audio file"
LOCAL_NEEDS_OLLAMA = "AI feedback needs Ollama; showing measured metrics only"
INTERRUPTED = "Coaching was interrupted. Try again."
REVIEW_OFF = "AI review is off"
# A re-run of a finished report whose new AI review failed: the previous review is kept.
REVIEW_KEPT = "The AI review couldn’t be refreshed, so this is the previous one."
EXCLUDED = "excluded"
# The detector's own wording, so a reason reads the same wherever it was decided.
LAUGHTER_NO_AUDIO = laughter.REASON_NO_AUDIO
LAUGHTER_TOO_LONG = laughter.REASON_TOO_LONG
LAUGHTER_FAILED = laughter.REASON_FAILED
FIRST_LINE_CHARS = 200

# Keys that describe one run's outcome; cleared when a run ends without a report.
_REPORT_KEYS = ("metrics", "review", "engagement", "report_stale", "candidates")
# Removed when the user excludes a recording: everything a run or an owner pick wrote.
_EXCLUDE_DROPS = ("metrics", "review", "engagement", "owner", "candidates", "report_stale", "mixed_up", "created_at")

_lock = Lock()
_running: dict[str, int] = {}  # runs in progress per session in this process
_pending: dict[str, dict] = {}  # one queued rerun per session: {"audio_path", "force"}
_generation: dict[str, int] = {}  # latest run number per session; older runs drop their result

# The one bulk re-run ("Coach them again"): progress, the recording in progress, and a stop request
_bulk_lock = Lock()
_bulk: dict = {"running": False, "done": 0, "total": 0, "current": None, "cancel": False}


# ---------------------------------------------------------------- inputs


def has_transcript(session_id: str) -> bool:
    """True when the recording has a transcript JSON (coaching needs one)."""
    return (TRANSCRIPTS_DIR / f"{session_id}.json").is_file()


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


def labelling_fingerprint(segments: list[dict]) -> str:
    """sha1 of the sorted (start rounded to 0.1 s, speaker) pairs: changes when speakers are re-labelled."""
    pairs = sorted((round(float(s.get("start") or 0), 1), s.get("speaker") or "") for s in segments)
    return store.text_hash(json.dumps(pairs))


def _resolve_owner(
    sidecar: dict, labels: list[str], audio: str | None, segments: list[dict], session_id: str | None = None
):
    """Return ``(owner, reason)``: owner is None when the user must pick, with reason saying why.

    A saved owner (the user's pick first, then an earlier voice match) is reused only while the
    speaker labelling it was made for is unchanged; after re-labelling, "Speaker 2" may be
    someone else, so the voice is matched again.
    """
    saved = sidecar.get("owner") or {}
    saved_labels = saved.get("labels") or []
    if (
        saved.get("source") in ("user", "voice")
        and saved_labels
        and set(saved_labels) <= set(labels)
        and saved.get("labelling") == labelling_fingerprint(segments)
    ):
        return saved, ""
    if not audio:
        return None, NO_AUDIO
    match = voice.match_owner(audio, segments, session_id)
    if match.get("labels"):
        owner = {"labels": list(match["labels"]), "source": "voice", "confidence": float(match["confidence"])}
        return {**owner, "labelling": labelling_fingerprint(segments)}, ""
    return None, match.get("reason") or "no confident match"


def _candidates(labels: list[str], segments: list[dict], audio: str | None) -> list[dict]:
    loud = voice.loudest_label(audio, segments) if audio and voice.model_available() else None
    lines = _first_lines(segments)
    return [{"label": label, "first_line": lines.get(label, ""), "loud": label == loud} for label in labels]


def _review(segments, owner_labels, metrics, config, laughs) -> tuple[dict | None, list[str]]:
    try:
        result = review_conversation(segments, owner_labels, metrics, config, laughs=laughs)
    except Exception as e:
        log.warning("coaching review failed: %s", e, exc_info=True)
        return None, [str(e)]
    return result.get("review"), list(result.get("errors") or [])


def _detect_laughter(audio: str | None, duration: float) -> tuple[str, str, list, str | None]:
    """``(status, reason, events, wav)`` from the laughter detector; never raises.

    ``wav`` is the detector's converted audio, kept so the loudness check needn't convert
    again; the caller must delete it (``_remove``). Recordings over LAUGHTER_MAX_S are
    skipped before any audio is converted."""
    if not audio:
        return "unavailable", LAUGHTER_NO_AUDIO, [], None
    if duration > LAUGHTER_MAX_S:
        return "unavailable", LAUGHTER_TOO_LONG, [], None
    try:
        result = laughter.detect(audio, keep_wav=True)
    except Exception as e:
        log.warning("laughter detection failed: %s", e, exc_info=True)
        return "unavailable", LAUGHTER_FAILED, [], None
    if not isinstance(result, dict):
        return "unavailable", LAUGHTER_FAILED, [], None
    wav = result.get("wav") if isinstance(result.get("wav"), str) else None
    if result.get("status") != "done":
        _remove(wav)
        return "unavailable", str(result.get("reason") or LAUGHTER_FAILED), [], None
    events = result.get("events")
    return "done", "", events if isinstance(events, list) else [], wav


def _remove(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _laughs_and_loudness(audio, segments, owner_labels, duration) -> tuple[str, str, list, dict | None]:
    """Laughter events and the loudness to tell them apart from you: ``(status, reason, events,
    loudness)``. Never raises: if a step fails, laughter is "unavailable"."""
    status, reason, events, wav = _detect_laughter(audio, duration)
    try:
        # One conversion of the audio (the detector's) measures your voice and each candidate.
        candidates = engagement.laugh_candidates(segments, owner_labels, events) if events else []
        windows = [c["window"] for c in candidates]
        loudness = engagement.measure_loudness(audio, segments, owner_labels, windows, wav=wav) if candidates else None
        return status, reason, events, loudness
    except Exception as e:
        log.warning("measuring laughs failed: %s", e, exc_info=True)
        return "unavailable", LAUGHTER_FAILED, [], None
    finally:
        _remove(wav)


def _engagement_block(segments, owner_labels, laughs, review, duration) -> dict | None:
    """The engagement block, never raising: metrics and the review must never be lost to it.

    If computing it fails, it is tried again with laughter "unavailable", then also without
    the review, so every part that can be worked out still is. None if even that fails."""
    status, reason, events, loudness = laughs
    attempts = (
        (status, reason, events, review, loudness),
        ("unavailable", LAUGHTER_FAILED, [], review, None),
        ("unavailable", LAUGHTER_FAILED, [], None, None),
    )
    for st, why, evs, rev, loud in attempts:
        try:
            return engagement.compute(segments, owner_labels, evs, st, why, rev, duration, loud)
        except Exception as e:
            log.warning("engagement failed: %s", e, exc_info=True)
    return None


def _with_engagement(metrics: dict, block: dict | None) -> dict:
    """Copy the engagement parts into the metrics, so Progress series pick them up."""
    if block is None:
        return metrics
    owner, availability = metrics.setdefault("owner", {}), metrics.setdefault("availability", {})
    for key, kind in (("laughs_per_hour", "estimated"), ("follow_up_share", "exact"), ("built_on_share", "estimated")):
        owner[key] = block[key]
        availability[key] = kind if block[key] is not None else "unavailable"
    return metrics


def _local_only(config: dict) -> bool:
    return config.get("summarization_provider") in ("local", "ollama")


def ai_review_on(config: dict) -> bool:
    """The "Include an AI review" setting: off means coaching never contacts any provider."""
    return config.get("coaching_ai_review", True) is not False


def is_excluded(session_id: str) -> bool:
    """True when the user chose "Don't coach this recording"."""
    return bool(store.load(session_id).get("excluded"))


def _analyse(
    sidecar: dict,
    words: list | None,
    transcript: dict,
    audio: str | None,
    force: bool,
    config: dict,
    previous_review: dict | None = None,
    session_id: str | None = None,
) -> tuple[dict, set]:
    """Work out this run's outcome. Returns (fields to set, keys to remove) for the sidecar.

    ``previous_review`` is the finished report's review when it was made for this transcript;
    it is kept if the new review fails and the owner is unchanged."""
    segments = transcript.get("segments") or []
    labels = _speaker_labels(segments)
    if not labels:
        return {"status": "skipped", "status_reason": NO_LABELS}, set(_REPORT_KEYS)
    if len(labels) == 1:
        return {"status": "skipped", "status_reason": ONLY_YOU}, set(_REPORT_KEYS)
    duration = _duration(transcript, segments)
    if not force and duration < MIN_COACHING_SECONDS:
        return {"status": "skipped", "status_reason": TOO_SHORT}, set(_REPORT_KEYS)

    owner, reason = _resolve_owner(sidecar, labels, audio, segments, session_id)
    if owner is None:
        fields = {"status": "needs_owner", "status_reason": reason, "candidates": _candidates(labels, segments, audio)}
        return fields, {"owner", "metrics", "review", "engagement", "report_stale"}
    owner_labels = sorted(owner["labels"])
    if set(owner_labels) >= set(labels):
        return {"status": "skipped", "status_reason": ONLY_YOU, "owner": owner}, set(_REPORT_KEYS)

    metrics = compute_metrics(
        segments,
        words or None,
        set(owner_labels),
        duration,
        bool(transcript.get("timestamps_estimated")),
    )
    laughs = _laughs_and_loudness(audio, segments, owner_labels, duration)

    def _engagement(review: dict | None) -> dict | None:
        return _engagement_block(segments, owner_labels, laughs, review, duration)

    def _fields(review: dict | None, block: dict | None, reason: str) -> dict:
        fields = {"owner": owner, "metrics": _with_engagement(metrics, block), "review": review}
        if block is not None:
            fields["engagement"] = block
        return {**fields, "status": "done", "status_reason": reason}

    drop = {"report_stale", "candidates"}
    if not ai_review_on(config):
        # The user chose measured results only: the transcript never leaves the Mac.
        block = _engagement(None)
        return _fields(None, block, REVIEW_OFF), drop | ({"engagement"} if block is None else set())
    before_review = _engagement(None)
    review, errors = _review(segments, owner_labels, metrics, config, (before_review or {}).get("laughs") or [])
    previous_owner = sorted((sidecar.get("owner") or {}).get("labels") or [])
    if review is None and previous_review is not None and previous_owner == owner_labels:
        log.warning("AI review failed (%s); keeping the previous one", "; ".join(errors) or "no answer")
        block = _engagement(previous_review)
        return _fields(previous_review, block, REVIEW_KEPT), drop | ({"engagement"} if block is None else set())
    block = _engagement(review)
    fields = _fields(review, block, "")
    if block is None:
        drop = drop | {"engagement"}
    if review is None:
        # Local users without a usable Ollama still get their measured metrics. Any other
        # error (broken JSON, a timeout) is a real failure, for them too.
        if _local_only(config) and errors and all(ollama_unavailable(e) for e in errors):
            fields["status_reason"] = LOCAL_NEEDS_OLLAMA
        else:
            fields["status"] = "failed"
            fields["status_reason"] = "AI feedback failed: " + ("; ".join(errors) or "no AI provider answered")
    return fields, drop


# ---------------------------------------------------------------- runs in progress
#
# ``_running`` counts the runs in progress per session in this process (background and
# synchronous). A trigger that arrives while one is going is not dropped: it is kept in
# ``_pending`` and the last run to finish starts it, so a re-transcribed recording is
# always coached against its newest transcript.


def _enter(session_id: str) -> None:
    with _lock:
        _running[session_id] = _running.get(session_id, 0) + 1


def _leave(session_id: str) -> dict | None:
    """End one run. Returns a pending rerun to do now (the session stays registered), or None."""
    with _lock:
        if _running.get(session_id) == 1 and session_id in _pending:
            return _pending.pop(session_id)
        count = _running.get(session_id, 1) - 1
        if count > 0:
            _running[session_id] = count
        else:
            _running.pop(session_id, None)
        return None


def _spawn(session_id: str, audio_path: str | None, force: bool) -> None:
    """Start a background run for a session already registered by ``_enter``/``_leave``."""
    try:
        Thread(
            target=_run_in_background,
            kwargs={"session_id": session_id, "audio_path": audio_path, "force": force},
            daemon=True,
        ).start()
    except BaseException:
        _leave(session_id)
        raise


def _run_in_background(session_id: str, audio_path: str | None, force: bool) -> None:
    while True:
        try:
            _run_once(session_id, audio_path, force)
        except Exception as e:
            log.warning("background coaching for %s failed: %s", session_id, e, exc_info=True)
        rerun = _leave(session_id)
        if rerun is None:
            return
        audio_path, force = rerun["audio_path"] or audio_path, rerun["force"]


def _finish_sync(session_id: str, audio_path: str | None) -> None:
    """End a synchronous run; a trigger that arrived meanwhile runs in the background."""
    rerun = _leave(session_id)
    if rerun is not None:
        _spawn(session_id, rerun["audio_path"] or audio_path, rerun["force"])


def is_running(session_id: str) -> bool:
    """True while this process is coaching the recording."""
    with _lock:
        return session_id in _running


# ---------------------------------------------------------------- public API


def run_coaching(session_id: str, audio_path: str | None, force: bool = False) -> dict:
    """Coach one recording now (synchronously) and return its sidecar.

    ``force`` (the "Coach this recording" action) ignores the ``coaching_auto`` setting and
    the minimum length; it never coaches a recording without speaker labels or where only
    the owner spoke, nor an excluded one. Without a transcript nothing is written.
    """
    _enter(session_id)
    try:
        return _run_once(session_id, audio_path, force)
    finally:
        _finish_sync(session_id, audio_path)


def _run_once(session_id: str, audio_path: str | None, force: bool) -> dict:
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

    def _mark_running(d: dict) -> bool | None:
        if d.get("excluded"):
            before["excluded"] = True
            return False
        before.update(status=d.get("status"), status_reason=d.get("status_reason"))
        review = d.get("review")
        if (
            d.get("status") == "done"
            and isinstance(review, dict)
            and not d.get("report_stale")
            and d.get("transcript_text_hash") == start_hash
        ):
            before["review"] = review  # kept if this run's review fails
        d.update(status="running", status_reason="", mixed_up=bool(d.get("mixed_up", False)))
        return None

    sidecar = store.update(session_id, _mark_running)
    if before.get("excluded"):
        return sidecar
    try:
        audio = _resolve_audio(session_id, audio_path)
        words = store.load_words(session_id)
        fields, drop = _analyse(sidecar, words, transcript, audio, force, config, before.get("review"), session_id)
    except Exception as e:
        log.warning("coaching run for %s failed: %s", session_id, e, exc_info=True)
        fields, drop = {"status": "failed", "status_reason": f"Coaching failed: {e}"}, set()

    return store.update(session_id, _commit(session_id, generation, start_hash, before, fields, drop))


def _commit(session_id, generation, start_hash, before, fields, drop) -> Callable[[dict], None]:
    """The sidecar mutation that saves a run's outcome, unless the run is out of date.

    Runs under the sidecar lock. Re-reading the transcript there is safe (nothing takes the
    sidecar lock while holding a transcript lock) and closes the gap with ``mark_stale``: a
    transcript write after this check marks the new report stale once the lock is released.
    If the transcript is gone (the recording was deleted meanwhile), nothing is written, so a
    deleted recording's coaching data is not recreated.
    """

    def mutate(d: dict) -> bool | None:
        if d.get("excluded"):
            log.info("coaching for %s was turned off while it ran; result dropped", session_id)
            return False
        with _lock:
            superseded = _generation.get(session_id) != generation
        if superseded:
            log.info("coaching run for %s superseded by a newer run; result dropped", session_id)
            return None
        current = _read_transcript(session_id)
        if current is None and not has_transcript(session_id):
            log.info("transcript %s deleted while coaching ran; result dropped", session_id)
            return False
        if current is None or store.text_hash(current.get("text")) != start_hash:
            log.info("transcript %s changed while coaching ran; result dropped", session_id)
            if before["status"] in (None, "running"):
                d.pop("status", None)
                d.pop("status_reason", None)
            else:
                d["status"], d["status_reason"] = before["status"], before["status_reason"]
            return None
        for key in drop:
            d.pop(key, None)
        d.update(fields)
        d["mixed_up"] = bool(d.get("mixed_up", False))
        d["transcript_text_hash"] = start_hash
        d["created_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        return None

    return mutate


def start_coaching_async(session_id: str, audio_path: str | None, force: bool = False) -> None:
    """Coach a recording in a daemon thread.

    Does nothing when automatic coaching is off (unless ``force``) or the recording is
    excluded (even with ``force``: clear the exclusion first). If the recording is
    already being coached in this process, the request is queued as one rerun (``force``
    if any queued request asked for it) that starts when the current run finishes.
    """
    if not force and not load_config().get("coaching_auto", True):
        return
    if is_excluded(session_id):
        return
    with _lock:
        if session_id in _running:
            queued = _pending.get(session_id) or {"audio_path": None, "force": False}
            _pending[session_id] = {"audio_path": audio_path or queued["audio_path"], "force": queued["force"] or force}
            return
        _running[session_id] = 1
    _spawn(session_id, audio_path, force)


def current(session_id: str) -> dict:
    """The sidecar as the UI should see it.

    A "running" status with no run in this process means the app quit mid-run: it is
    rewritten as "failed" so the report doesn't spin forever.
    """
    data = store.load(session_id)
    if data.get("status") != "running" or is_running(session_id):
        return data

    def _interrupted(d: dict) -> None:
        if d.get("status") == "running" and not is_running(session_id):
            d.update(status="failed", status_reason=INTERRUPTED)

    return store.update(session_id, _interrupted)


def _owner_pick(session_id: str, labels) -> tuple[list[dict], list[str], dict]:
    """Validate a pick against the transcript. Returns (segments, chosen labels, owner)."""
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
    owner = {"labels": chosen, "source": "user", "confidence": 1.0, "labelling": labelling_fingerprint(segments)}
    return segments, chosen, owner


def _save_pick(session_id: str, owner: dict) -> dict:
    """Save the owner and status "running" in one write, before any learning or run starts.

    The caller must have registered the run (``_enter``) first and keep it registered until
    the run ends, or ``current`` would rewrite this "running" as interrupted. Raises
    ValueError, writing nothing, if the recording is excluded.
    """

    before: dict = {}

    def _pick(d: dict) -> None:
        if d.get("excluded"):
            raise ValueError("this recording isn't coached")
        before.update(d)
        d.update(owner=owner, status="running", status_reason="")

    saved = store.update(session_id, _pick)
    _owner_changed(session_id, before, saved)
    return saved


def _owner_changed(session_id: str, before: dict, after: dict) -> None:
    """Your own pick puts your profile name on your speaker; a change reaches the window and Notion."""
    from app.state import speakers

    speakers.owner_changed(session_id, before, after)


def _learn_and_run(session_id: str, segments: list[dict], chosen: list[str], learn: bool) -> tuple[str | None, dict]:
    audio = _resolve_audio(session_id, None)
    # An excluded recording never teaches the voice profile (it may have been excluded since the pick).
    if learn and audio and voice.model_available() and not is_excluded(session_id):
        try:
            voice.add_exemplars(audio, segments, chosen, session_id=session_id)
        except Exception as e:
            log.warning("could not add voice exemplars for %s: %s", session_id, e, exc_info=True)
    return audio, _run_once(session_id, audio, force=True)


def set_owner(session_id: str, labels: list[str], learn: bool = True) -> dict:
    """Save the user's pick of which speaker label(s) are them, then coach the recording again.

    Raises ValueError for an empty pick, a label not in the transcript or an excluded
    recording. The owner and status "running" are saved together first. With ``learn``, the
    pick also teaches the voice profile, but only if the voice model is already installed and
    the recording's audio is on this Mac. The pick holds only while the speaker labelling
    stays the same (see ``labelling_fingerprint``).
    """
    segments, chosen, owner = _owner_pick(session_id, labels)
    _enter(session_id)
    audio = None
    try:
        _save_pick(session_id, owner)
        audio, out = _learn_and_run(session_id, segments, chosen, learn)
        return out
    finally:
        _finish_sync(session_id, audio)


def start_set_owner(session_id: str, labels: list[str], learn: bool = True) -> dict:
    """``set_owner`` for the routes: save the pick now, coach in a daemon thread.

    Returns the sidecar as saved, already with status "running", so the UI never sees the
    old status after a pick. Raises ValueError like ``set_owner``.
    """
    segments, chosen, owner = _owner_pick(session_id, labels)
    _enter(session_id)
    try:
        saved = _save_pick(session_id, owner)
        Thread(
            target=_set_owner_in_background,
            kwargs={"session_id": session_id, "segments": segments, "chosen": chosen, "learn": learn},
            daemon=True,
        ).start()
    except BaseException:
        _finish_sync(session_id, None)
        raise
    return saved


def _set_owner_in_background(session_id: str, segments: list[dict], chosen: list[str], learn: bool) -> None:
    audio = None
    try:
        audio, _ = _learn_and_run(session_id, segments, chosen, learn)
    except Exception as e:
        log.warning("coaching after an owner pick for %s failed: %s", session_id, e, exc_info=True)
    finally:
        _finish_sync(session_id, audio)


def set_excluded(session_id: str, excluded: bool) -> dict:
    """Exclude a recording from coaching ("Don't coach this recording"), or undo it. Returns the sidecar.

    Excluding deletes the report (metrics, review, owner pick, candidates, the mixed-up flag)
    and sets ``excluded: true`` with status ``excluded``; the verbatim words file is kept, as
    it belongs to the transcription. It also counts as a newer run, so a run in progress drops
    its result, and drops any queued rerun. Re-transcribing never clears it (``mark_stale``
    and ``save_words`` leave other keys alone). Undoing it clears the flag and the status,
    leaving the recording "not coached yet"; it does not start a run. Excluding removes
    your profile name from your speaker (it needs a coached pick).
    """
    if excluded:
        with _lock:
            _generation[session_id] = _generation.get(session_id, 0) + 1
            _pending.pop(session_id, None)

    before: dict = {}

    def mutate(d: dict) -> bool | None:
        before.update(d)
        if excluded:
            for key in _EXCLUDE_DROPS:
                d.pop(key, None)
            d.update(excluded=True, status=EXCLUDED, status_reason="")
            return None
        if not d.get("excluded") and d.get("status") != EXCLUDED:
            return False
        d.pop("excluded", None)
        if d.get("status") == EXCLUDED:
            d.pop("status", None)
            d.pop("status_reason", None)
        return None

    saved = store.update(session_id, mutate)
    _owner_changed(session_id, before, saved)
    return saved


# ---------------------------------------------------------------- coach older reports again


def _needs_rerun(session_id: str, sidecar: dict) -> bool:
    """A finished report saved before Engagement existed, for a recording that can still be coached."""
    return (
        sidecar.get("status") == "done"
        and "engagement" not in sidecar
        and not sidecar.get("excluded")
        and has_transcript(session_id)
    )


def rerun_candidates() -> list[str]:
    """Every recording "Coach them again" would coach now, in session id order."""
    directory = store.COACHING_DIR
    out = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        sid = path.stem
        if not store.is_sidecar_stem(sid):  # the words files and the voice profile
            continue
        try:
            validate_session_id(sid)
        except InvalidSessionIdError:
            continue
        if _needs_rerun(sid, store.load(sid)):
            out.append(sid)
    return out


def rerun_all_status() -> dict:
    """``{"running", "done", "total", "current"}`` for the bulk re-run (the last one's, once it ends)."""
    with _bulk_lock:
        return {key: _bulk[key] for key in ("running", "done", "total", "current")}


def start_rerun_all() -> bool:
    """Coach every ``rerun_candidates`` recording again, one at a time, in a daemon thread.

    Returns False, starting nothing, if a bulk re-run is already going.
    """
    if rerun_all_status()["running"]:
        return False
    session_ids = rerun_candidates()  # before "running" shows, so its total is never 0 of 0
    with _bulk_lock:
        if _bulk["running"]:
            return False
        _bulk.update(running=True, done=0, total=len(session_ids), current=None, cancel=False)
    try:
        Thread(target=_rerun_all, kwargs={"session_ids": session_ids}, daemon=True).start()
    except BaseException:
        with _bulk_lock:
            _bulk.update(running=False, current=None)
        raise
    return True


def cancel_rerun_all() -> bool:
    """Stop the bulk re-run after the recording in progress. False if none is going."""
    with _bulk_lock:
        if not _bulk["running"]:
            return False
        _bulk["cancel"] = True
        return True


def _rerun_all(session_ids: list[str]) -> None:
    try:
        for sid in session_ids:
            with _bulk_lock:
                if _bulk["cancel"]:
                    return
                _bulk["current"] = sid
            try:
                # Checked again: since the list was made, it may have been excluded or coached again.
                # Never learns the voice (only an owner pick does) and keeps the saved owner.
                if _needs_rerun(sid, store.load(sid)):
                    run_coaching(sid, None, force=True)
            except Exception as e:
                log.warning("coaching %s again failed: %s", sid, e, exc_info=True)
            with _bulk_lock:
                _bulk["done"] += 1
    finally:
        with _bulk_lock:
            _bulk.update(running=False, current=None, cancel=False)
