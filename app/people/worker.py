"""One background thread for voice work: learning from names and matching recordings.

Jobs run one at a time, oldest first; an identical job already waiting isn't queued
twice, and a failing job is logged and never stops the thread. The thread starts on
the first job. Kinds:

- ``match(session_id, audio_path)``: match a recording's speakers to known people.
- ``learn(session_id, label, name)``: the speaker was named; teach that voice.
- ``unlearn(session_id, label)``: the name was cleared; forget that sample.
- ``forget_recording(session_id)``: the recording was marked mixed up; forget every
  sample it taught (yours too) and re-match.
- ``rematch()``: samples changed; match every recording with cached embeddings again
  (maths only, no audio). Any number of these waiting collapse into one.
- ``backlog()``: learn from past recordings, calibrate, name them (``app.people.backlog``).
  It hands the thread back after any recording while other jobs (or a directory write)
  wait, and queues itself again to carry on.

Every job runs holding the job lock, and the people directory's writes (rename, merge,
delete) hold it throughout via ``exclusive()``, so a job never sees a person renamed,
merged or deleted halfway through (a learn job reads the typed name, embeds, then
stores the sample under that name). Lock order: the job lock first, then the transcript
and store locks one at a time; never take the job lock while holding either.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager

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
_current: str | None = None  # the kind of the job running now
_thread: threading.Thread | None = None
_job_lock = threading.RLock()  # held by each job, and by directory writes (``exclusive``)
_exclusive_waiting = 0  # directory writes waiting for the job lock (guarded by _cond)


def _norm(name: str | None) -> str:
    return people_store._norm_name(name or "")


# ------------------------------------------------------------------- jobs


def _match(session_id: str, audio_path: str | None = None) -> None:
    apply.match_recording(session_id, audio_path)


def _learn(session_id: str, label: str, name: str) -> None:
    """Add this speaker's voice to the person they were named as.

    Only while that is still the label's typed name (otherwise a later job handles
    it), the label isn't frozen, the recording isn't mixed up, and the label has
    enough speech. Your profile name teaches ``me``, except on a recording excluded
    from coaching. When it can't learn, the speaker's earlier sample is still removed,
    since the name it was learnt under is no longer theirs.
    """
    transcript = speakers._read(session_id)
    if transcript is None or _norm(sn.stored_names(transcript).get(label)) != _norm(name):
        return
    sample = _sample_for(session_id, transcript, label, name)
    if sample is None:
        _forget(session_id, label)
        return
    add_samples([(name, sample)])
    enqueue("rematch")  # first: a failing recalibration must not cost the re-match
    from app.people import backlog

    backlog.maybe_recalibrate()


def add_samples(named: list[tuple[str, dict]]) -> None:
    """Store each ``(typed name, sample)`` in one store write.

    Your profile name goes to ``me``; any other name to the person with that name
    (case and spacing ignored), created if new. Never merges different spellings.
    """
    if not named:
        return
    profile = _norm(speakers.profile_name())

    def add(data: dict) -> None:
        for name, sample in named:
            pid = people_store.ME if profile and _norm(name) == profile else people_store.find_person(data, name)
            if pid is None:
                pid = people_store.create_person(data, name)
            people_store.add_sample(data, pid, sample)

    people_store.mutate(add)


def _sample_for(session_id: str, transcript: dict, label: str, name: str) -> dict | None:
    """The sample this named speaker gives, or None when it mustn't or can't teach."""
    frozen = transcript.get("speaker_names_frozen")
    if isinstance(frozen, list) and label in frozen:
        return None
    if coaching_store.load(session_id).get("mixed_up") is True:
        return None
    segments = transcript.get("segments") or []
    seconds = embeddings.speech_seconds(segments).get(label, 0.0)
    if seconds < matching.MIN_LEARN_SECONDS:
        return None
    if _norm(name) == _norm(speakers.profile_name()) and runner.is_excluded(session_id):
        return None
    vec = (embeddings.get(session_id, segments) or {}).get(label)
    if not vec:
        return None
    return {
        "vec": vec,
        "session_id": session_id,
        "label": label,
        "seconds": round(seconds, 1),
        "added_at": people_store._now(),
    }


def _forget(session_id: str, label: str) -> None:
    """Remove the sample this recording speaker gave, if any, and re-match."""
    had = any(
        (s.get("session_id"), s.get("label")) == (session_id, label)
        for person in people_store.load()["people"].values()
        for s in person["samples"]
    )
    if had:
        people_store.mutate(lambda data: people_store.remove_sample(data, session_id, label))
        enqueue("rematch")


def _unlearn(session_id: str, label: str) -> None:
    """Forget the sample this speaker gave, unless the label has been named again since."""
    transcript = speakers._read(session_id)
    if transcript is not None and sn.stored_names(transcript).get(label):
        return
    _forget(session_id, label)


def _forget_recording(session_id: str) -> None:
    def drop(data: dict) -> None:
        for person in data["people"].values():
            person["samples"] = [s for s in person["samples"] if s.get("session_id") != session_id]

    people_store.mutate(drop)
    enqueue("rematch")


def _rematch() -> None:
    """Match every recording with cached embeddings again (any one may now name or drop someone)."""
    if matching.current_threshold() is None or not speakers.TRANSCRIPTS_DIR.is_dir():
        return
    people = people_store.load()["people"]
    for path in sorted(speakers.TRANSCRIPTS_DIR.glob("*.json"), reverse=True):
        try:
            apply.match_recording(path.stem, cached_only=True, people=people)
        except Exception as e:
            log.warning("re-matching %s failed: %s", path.stem, e)


def _backlog() -> None:
    from app.people import backlog

    if backlog.run(backlog.cancel_event, should_yield=_others_waiting):
        enqueue("backlog")


def _others_waiting() -> bool:
    with _cond:
        return bool(_queue) or _exclusive_waiting > 0


@contextmanager
def exclusive() -> Iterator[None]:
    """Hold the job lock: waits for the job in hand to finish, and no job starts meanwhile.

    A running backlog hands back after its current recording. Don't call it while holding
    a transcript or store lock.
    """
    global _exclusive_waiting
    with _cond:
        _exclusive_waiting += 1
    try:
        _job_lock.acquire()
    finally:
        with _cond:
            _exclusive_waiting -= 1
    try:
        yield
    finally:
        _job_lock.release()


_HANDLERS = {
    "match": _match,
    "learn": _learn,
    "unlearn": _unlearn,
    "forget_recording": _forget_recording,
    "rematch": _rematch,
    "backlog": _backlog,
}


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
    global _busy, _current
    while True:
        with _cond:
            while not _queue:
                _cond.wait()
            kind, kw = _queue.popleft()
            _busy, _current = True, kind
        try:
            with _job_lock:
                _HANDLERS[kind](**dict(kw))
        except Exception as e:
            log.warning("people job %s failed: %s", kind, e, exc_info=True)
        finally:
            with _cond:
                _busy, _current = False, None
                _cond.notify_all()


def has_job(kind: str) -> bool:
    """Whether a job of this kind is running or waiting."""
    with _cond:
        return _current == kind or any(k == kind for k, _ in _queue)


def wait_idle(timeout: float | None = None) -> bool:
    """Block until no job is waiting or running. False if ``timeout`` ran out first."""
    with _cond:
        return _cond.wait_for(lambda: not _queue and not _busy, timeout)
