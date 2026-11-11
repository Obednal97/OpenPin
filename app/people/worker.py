"""One background thread for voice work: learning from names and matching recordings.

Jobs run one at a time, oldest first; an identical job already waiting isn't queued
twice, and a failing job is logged and never stops the thread. The thread starts on
the first job. Kinds:

- ``match(session_id, audio_path)``: match a recording's speakers to known people.
- ``learn(session_id, label, name)``: the speaker was named; teach that voice.
- ``unlearn(session_id, label)``: the name was cleared; forget that sample.
- ``rematch_person(person_id)``: someone's samples changed; match every recording
  with cached embeddings again (maths only, no audio).
"""

from __future__ import annotations

import logging
import threading
from collections import deque

from app.coaching import runner
from app.coaching import store as coaching_store
from app.people import apply, embeddings, matching
from app.people import store as people_store
from app.state import speakers
from app.utils import speaker_names as sn

log = logging.getLogger(__name__)

_cond = threading.Condition()
_queue: deque[tuple[str, tuple]] = deque()
_busy = False
_thread: threading.Thread | None = None


def _norm(name: str | None) -> str:
    return people_store._norm_name(name or "")


# ------------------------------------------------------------------- jobs


def _match(session_id: str, audio_path: str | None = None) -> None:
    apply.match_recording(session_id, audio_path)


def _learn(session_id: str, label: str, name: str) -> None:
    """Add this speaker's voice to the person they were named as.

    Only while that is still the label's typed name, the label isn't frozen, the
    recording isn't mixed up, and the label has enough speech. Your profile name
    teaches ``me``, except on a recording excluded from coaching (then nothing).
    """
    transcript = speakers._read(session_id)
    if transcript is None or _norm(sn.stored_names(transcript).get(label)) != _norm(name):
        return
    frozen = transcript.get("speaker_names_frozen")
    if isinstance(frozen, list) and label in frozen:
        return
    if coaching_store.load(session_id).get("mixed_up") is True:
        return
    segments = transcript.get("segments") or []
    seconds = embeddings.speech_seconds(segments).get(label, 0.0)
    if seconds < matching.MIN_LEARN_SECONDS:
        return
    is_me = _norm(name) == _norm(speakers.profile_name())
    if is_me and runner.is_excluded(session_id):
        return
    emb = embeddings.get(session_id, segments)
    vec = (emb or {}).get(label)
    if not vec:
        return
    sample = {
        "vec": vec,
        "session_id": session_id,
        "label": label,
        "seconds": round(seconds, 1),
        "added_at": people_store._now(),
    }
    out = {}

    def add(data: dict) -> None:
        pid = people_store.ME if is_me else people_store.find_person(data, name)
        if pid is None:
            pid = people_store.create_person(data, name)
        people_store.add_sample(data, pid, sample)
        out["person"] = pid

    people_store.mutate(add)
    enqueue("rematch_person", person_id=out["person"])


def _unlearn(session_id: str, label: str) -> None:
    """Forget the sample this speaker gave, unless the label has been named again since."""
    transcript = speakers._read(session_id)
    if transcript is not None and sn.stored_names(transcript).get(label):
        return
    had = any(
        (s.get("session_id"), s.get("label")) == (session_id, label)
        for person in people_store.load()["people"].values()
        for s in person["samples"]
    )
    if not had:
        return
    out = {}
    people_store.mutate(lambda data: out.update(person=people_store.remove_sample(data, session_id, label)))
    if out.get("person"):
        enqueue("rematch_person", person_id=out["person"])


def _rematch_person(person_id: str) -> None:
    """Match every recording with cached embeddings again (any one may now name or drop someone)."""
    if matching.current_threshold() is None or not speakers.TRANSCRIPTS_DIR.is_dir():
        return
    people = people_store.load()["people"]
    for path in sorted(speakers.TRANSCRIPTS_DIR.glob("*.json"), reverse=True):
        try:
            apply.match_recording(path.stem, cached_only=True, people=people)
        except Exception as e:
            log.warning("re-matching %s failed: %s", path.stem, e)


_HANDLERS = {"match": _match, "learn": _learn, "unlearn": _unlearn, "rematch_person": _rematch_person}


# ------------------------------------------------------------------- the queue


def enqueue(kind: str, **kw) -> None:
    """Queue a job (skipped if an identical one is already waiting). ValueError for an unknown kind."""
    global _thread
    if kind not in _HANDLERS:
        raise ValueError(f"unknown people job: {kind}")
    job = (kind, tuple(sorted(kw.items())))
    with _cond:
        if job not in _queue:
            _queue.append(job)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run, name="people-worker", daemon=True)
            _thread.start()
        _cond.notify_all()


def _run() -> None:
    global _busy
    while True:
        with _cond:
            while not _queue:
                _cond.wait()
            kind, kw = _queue.popleft()
            _busy = True
        try:
            _HANDLERS[kind](**dict(kw))
        except Exception as e:
            log.warning("people job %s failed: %s", kind, e, exc_info=True)
        finally:
            with _cond:
                _busy = False
                _cond.notify_all()


def wait_idle(timeout: float | None = None) -> bool:
    """Block until no job is waiting or running. False if ``timeout`` ran out first."""
    with _cond:
        return _cond.wait_for(lambda: not _queue and not _busy, timeout)
