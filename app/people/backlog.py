"""The backlog: learn voices from names typed in past recordings, calibrate, then name them.

Runs once after the update (started at launch) and again from the People section.
Three phases, one recording at a time on the people worker's thread:

1. **learning**: every transcript, newest first, teaches its typed names (the same
   rules as naming a speaker live: not frozen, not mixed up, at least 15 s, your
   profile name teaches ``me`` except on a recording excluded from coaching).
2. **calibrating**: the match threshold is tested on those names (leave one
   recording out) and saved as ``people_match_threshold`` and ``people_calibration``.
3. **naming**: every transcript, newest first, is matched against the people learnt.

Nothing is matched, so nothing is named or sent to Notion, until phase 2 has saved a
threshold. Progress (the session ids done in each phase) is kept in
``PEOPLE_DIR/backlog.json`` (mode 0600), so a restart or a cancel resumes where it
stopped. Plain Python: nothing here imports numpy.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

from app.coaching import store as coaching_store
from app.people import apply, embeddings, matching, worker
from app.people import store as people_store
from app.state import speakers
from app.utils import changes
from app.utils import speaker_names as sn
from app.utils.config import load_config, update_config
from app.utils.constants import PEOPLE_DIR
from app.utils.recording_utils import extract_recording_date

log = logging.getLogger(__name__)

STATE_PATH = PEOPLE_DIR / "backlog.json"
_ACTIVE = ("learning", "calibrating", "naming")

_lock = threading.Lock()  # guards STATE_PATH read-modify-writes
cancel_event = threading.Event()


# ------------------------------------------------------------------- saved progress


def _fresh() -> dict:
    return {"phase": "idle", "cancelled": False, "learned": [], "named": []}


def _ids(value) -> list[str]:
    return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []


def _load() -> dict:
    """The saved progress; anything unreadable or oddly shaped counts as never started."""
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return _fresh()
    if not isinstance(data, dict) or data.get("phase") not in ("idle", "done", *_ACTIVE):
        return _fresh()
    return {
        "phase": data["phase"],
        "cancelled": data.get("cancelled") is True,
        "learned": _ids(data.get("learned")),
        "named": _ids(data.get("named")),
    }


def _save(st: dict) -> None:
    people_store._write_private(STATE_PATH, st)


def _update(fn: Callable[[dict], None]) -> dict:
    with _lock:
        st = _load()
        fn(st)
        _save(st)
        return st


def state() -> dict:
    """``{"phase", "done", "total", "running"}``.

    ``phase`` is idle, learning, calibrating, naming, done or cancelled. ``done`` and
    ``total`` count the recordings there are now (one deleted since doesn't count);
    while calibrating both are 0. ``running`` is whether a backlog job is running or
    queued, so a stopped partial run (after a quit) can be told apart from one in hand.
    """
    st = _load()
    phase = st["phase"]
    done = total = 0
    if phase in ("learning", "naming", "done"):
        ids = _sessions()
        total = len(ids)
        if phase == "done":
            done = total
        else:
            processed = set(st["learned" if phase == "learning" else "named"])
            done = sum(1 for sid in ids if sid in processed)
    if st["cancelled"] and phase in _ACTIVE:
        phase = "cancelled"
    return {"phase": phase, "done": done, "total": total, "running": worker.has_job("backlog")}


def is_done() -> bool:
    return _load()["phase"] == "done"


# ------------------------------------------------------------------- recordings


def _sessions() -> list[str]:
    """Session ids with a transcript, newest recording first (as the recordings list:
    the date in the session id, else the transcript file's age)."""
    folder = speakers.TRANSCRIPTS_DIR
    if not folder.is_dir():
        return []

    def when(path) -> float:
        date = extract_recording_date(path.stem)
        if date is not None:
            return date.timestamp()
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    paths = [p for p in folder.glob("*.json") if not p.name.startswith(".")]
    return [p.stem for p in sorted(paths, key=lambda p: (when(p), p.stem), reverse=True)]


def _typed_labels(transcript: dict) -> dict[str, str]:
    """``{label: typed name}`` for the labels that aren't frozen."""
    frozen = transcript.get("speaker_names_frozen")
    frozen = frozen if isinstance(frozen, list) else []
    return {label: name for label, name in sn.stored_names(transcript).items() if label not in frozen}


def _learn_recording(session_id: str) -> None:
    """Teach this recording's typed names (``worker._sample_for`` applies every rule)."""
    transcript = speakers._read(session_id)
    if transcript is None:
        return
    sidecar = coaching_store.load(session_id)
    if sidecar.get("mixed_up") is True:
        return
    segments = transcript.get("segments") or []
    seconds = embeddings.speech_seconds(segments)
    typed = _typed_labels(transcript)
    wanted = [*typed, *sn.confirmed_owner_labels(transcript, sidecar)]
    if not any(seconds.get(label, 0.0) >= matching.MIN_LEARN_SECONDS for label in wanted):
        return  # nothing it could teach or test: no audio work
    # Embed once (your coaching pick needs it for calibration even without typed names).
    if not embeddings.get(session_id, segments):
        return
    named = []
    for label, name in typed.items():
        sample = worker._sample_for(session_id, transcript, label, name)
        if sample is not None:
            named.append((name, sample))
    worker.add_samples(named)


def _instances() -> list[tuple[str, str, list[float], str]]:
    """Calibration instances from cached embeddings (never audio).

    Every typed, unfrozen name with at least 15 s of speech on a recording that isn't
    mixed up, as ``(session_id, label, vec, person id)``, plus the speakers you picked
    as yourself for coaching (as ``me``) where no name is typed.
    """
    profile = people_store._norm_name(speakers.profile_name() or "")
    people = {"people": people_store.load()["people"]}
    out = []
    for session_id in _sessions():
        try:  # one malformed recording never stops calibration
            out.extend(_instances_for(session_id, people, profile))
        except Exception as e:
            log.warning("calibration skipped %s: %s", session_id, e)
    return out


def instances() -> list[tuple[str, str, list[float], str]]:
    """Public name for the calibration instances (``scripts/voice_eval.py`` uses it)."""
    return _instances()


def _instances_for(session_id: str, people: dict, profile: str) -> list[tuple[str, str, list[float], str]]:
    transcript = speakers._read(session_id)
    if transcript is None:
        return []
    sidecar = coaching_store.load(session_id)
    if sidecar.get("mixed_up") is True:
        return []
    segments = transcript.get("segments") or []
    emb = embeddings.cached(session_id, segments)
    if not emb:
        return []
    seconds = embeddings.speech_seconds(segments)
    long_enough = {label for label, s in seconds.items() if s >= matching.MIN_LEARN_SECONDS and emb.get(label)}
    typed = sn.stored_names(transcript)
    out = []
    for label, name in _typed_labels(transcript).items():
        if label not in long_enough:
            continue
        if profile and people_store._norm_name(name) == profile:
            pid = people_store.ME
        else:
            pid = people_store.find_person(people, name)
        if pid is not None:
            out.append((session_id, label, emb[label], pid))
    for label in sn.confirmed_owner_labels(transcript, sidecar):
        if label in long_enough and label not in typed:
            out.append((session_id, label, emb[label], people_store.ME))
    return out


def _calibrate(instances: list | None = None) -> dict:
    """Test the threshold on your library and save it. Returns ``people_calibration``."""
    if instances is None:
        instances = _instances()
    result = matching.calibrate(instances, people_store.load()["people"])
    cal = {
        "instances": result["instances"],
        "accuracy": result["accuracy"],
        "threshold": result["threshold"],
        "named": result["named"],
        "at": people_store._now(),
    }
    update_config(lambda c: c.update(people_match_threshold=result["threshold"], people_calibration=cal))
    log.info("people threshold %.3f (%d instances, %d named)", cal["threshold"], cal["instances"], cal["named"])
    return cal


def maybe_recalibrate() -> None:
    """After a name teaches a voice: calibrate again once the instances have doubled.

    Does nothing until the backlog has calibrated once. Cached embeddings only.
    """
    cal = load_config().get("people_calibration")
    if not isinstance(cal, dict):
        return
    before = int(cal.get("instances") or 0)
    instances = _instances()
    if len(instances) > before and len(instances) >= 2 * before:
        _calibrate(instances)


# ------------------------------------------------------------------- running


def _phase(name: str, key: str, step: Callable[[str], None], cancel: threading.Event, should_yield) -> str:
    """Run ``step`` on every recording not yet in ``st[key]``, saving after each.

    Ids of recordings deleted since are dropped, and recordings added meanwhile are
    picked up before the phase finishes. Returns "finished", "cancelled" or "yielded".
    """
    while True:
        ids = _sessions()
        current = set(ids)
        st = _update(lambda s, keep=current: s.update({key: [sid for sid in s[key] if sid in keep]}))
        seen = set(st[key])
        pending = [sid for sid in ids if sid not in seen]
        if not pending:
            return "finished"
        for session_id in pending:
            if cancel.is_set():
                _update(lambda s: s.update(cancelled=True))
                return "cancelled"
            try:
                step(session_id)
            except Exception as e:  # one bad recording never stops the backlog
                log.warning("backlog %s of %s failed: %s", name, session_id, e, exc_info=True)
            _update(lambda s, sid=session_id: s[key].append(sid))
            if should_yield is not None and should_yield():
                return "yielded"


def run(cancel: threading.Event, should_yield: Callable[[], bool] | None = None) -> bool:
    """Carry the backlog on from where it stopped, until done, cancelled or asked to yield.

    ``should_yield`` is asked after each recording; when it says yes, this returns True
    (call again to carry on). Returns False when done, cancelled or not possible (the
    voice model isn't installed: the backlog stays pending).
    """
    from app.coaching import voice

    if not voice.model_available():
        return False
    st = _update(lambda s: s.update(cancelled=False, phase="learning" if s["phase"] == "idle" else s["phase"]))
    if st["phase"] == "done":
        return False
    if st["phase"] == "learning":
        outcome = _phase("learning", "learned", _learn_recording, cancel, should_yield)
        if outcome != "finished":
            return outcome == "yielded"
        _update(lambda s: s.update(phase="calibrating"))
    if _load()["phase"] == "calibrating":
        _calibrate()
        _update(lambda s: s.update(phase="naming"))
    people = people_store.load()["people"]
    named = []  # the window is told once per batch, not per recording (Notion still is)

    def name(sid: str) -> None:
        if apply.match_recording(sid, people=people, bump=False):
            named.append(sid)

    outcome = _phase("naming", "named", name, cancel, should_yield)
    if named:
        changes.bump()
    if outcome != "finished":
        return outcome == "yielded"
    _update(lambda s: s.update(phase="done"))
    return False


def start() -> bool:
    """Start the backlog on the people worker, or carry on from where it stopped.

    After a finished run it starts again from the beginning. False (nothing queued,
    nothing changed) when the voice model isn't installed.
    """
    from app.coaching import voice

    if not voice.model_available():
        return False

    def begin(st: dict) -> None:
        if st["phase"] == "done":
            st.update(_fresh())
        st["cancelled"] = False

    _update(begin)
    cancel_event.clear()
    worker.enqueue("backlog")
    return True


def cancel() -> None:
    """Stop after the recording in hand; ``start()`` carries on from there."""
    cancel_event.set()
    _update(lambda s: s.update(cancelled=s["phase"] in _ACTIVE))


def start_on_launch() -> threading.Thread:
    """At app launch: start the backlog in the background unless it is done or you cancelled it.

    Returns at once (checking for the voice model is slow). Without the model it stays
    pending and the People section offers it.
    """

    t = threading.Thread(target=start_if_pending, name="people-backlog-launch", daemon=True)
    t.start()
    return t


def start_if_pending() -> bool:
    """``start()`` unless the backlog is done or you cancelled it (at launch, and once the
    voice model is installed). Never raises; True when it was queued."""
    try:
        st = _load()
        if st["phase"] == "done" or st["cancelled"]:
            return False
        return start()
    except Exception as e:
        log.warning("could not start learning from past recordings: %s", e)
        return False
