"""Speaker names on disk: the ``speaker_names`` map in each recording's transcript JSON.

Writes run under the transcript lock like every other transcript edit. A change bumps
the library change counter (so an open window reloads) and, when the recording has a
Notion page, queues a full Notion update so the page shows the new names. No AI call
is ever made here.

The pure rules (validation, applying names, the profile default) are in
``app.utils.speaker_names``.
"""

import json
import logging
import os
from threading import Lock

from app.coaching import store as coaching_store
from app.people import store as people_store
from app.utils import changes
from app.utils import speaker_names as sn
from app.utils.config import load_config
from app.utils.constants import TRANSCRIPTS_DIR
from app.utils.paths import validate_session_id
from app.utils.transcript_locks import get_transcript_lock

log = logging.getLogger(__name__)


def _path(session_id: str):
    return TRANSCRIPTS_DIR / f"{session_id}.json"


def _read(session_id: str) -> dict | None:
    """The transcript JSON, or None when there is none (or it is unreadable)."""
    try:
        with get_transcript_lock(session_id):
            data = json.loads(_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_atomic(path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.names.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def profile_name() -> str | None:
    """Your name from Settings › Profile, if set."""
    profile = load_config().get("profile")
    name = profile.get("name") if isinstance(profile, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else None


def people_names(profile: str | None) -> dict[str, str | None]:
    """``{person id: name}`` for voice matches, ``me`` being your profile name.

    Never takes the voice-store lock (callers may hold the transcript lock), and never
    reads the config: ``profile`` is passed in.
    """
    names = people_store.read_names()
    names[people_store.ME] = profile or None
    return names


def names_for(session_id: str, transcript: dict, *, profile: str | None) -> dict[str, str]:
    """The effective names for an already-read transcript (stored names plus your own).

    ``profile`` is your name (``profile_name()``, read by the caller beforehand; None for
    none). It is a required keyword so this never reads the config itself: it only reads
    the coaching sidecar, which takes no lock (it is written atomically), so it is safe to
    call while holding the transcript lock.
    """
    return sn.effective_names(transcript, coaching_store.load(session_id), profile, people_names(profile))


def names_and_sources(session_id: str, transcript: dict, *, profile: str | None) -> tuple[dict, dict]:
    """``names_for``, plus ``{label: {"source": "voice", "score": float}}`` for the labels
    named by a voice match, reading the sidecar and voices once."""
    sidecar = coaching_store.load(session_id)
    people = people_names(profile)
    names = sn.effective_names(transcript, sidecar, profile, people)
    scores = sn.voice_matched(transcript, sidecar, profile, people)
    return names, {label: {"source": "voice", "score": score} for label, score in scores.items()}


def get_names(session_id: str) -> dict[str, str]:
    """The effective names for a recording, or {} without a transcript."""
    transcript = _read(session_id)
    if transcript is None:
        return {}
    return names_for(session_id, transcript, profile=profile_name())


def _notion_linked(session_id: str) -> bool:
    """Notion is connected and the recording has a page there (as tags.enqueue_notion_sync)."""
    try:
        from app.routes.notion import _get_notion_page_url
        from app.state import notion

        return notion.is_connected() and bool(_get_notion_page_url(session_id))
    except Exception as e:
        log.warning("Could not check the Notion page for %s: %s", session_id, e)
        return False


def _names_changed(session_id: str, *, bump: bool = True) -> None:
    """Tell the window (unless ``bump`` is False: the caller bumps once for many) and
    Notion that a recording's names changed. Never raises."""
    if bump:
        changes.bump()
    try:
        if _notion_linked(session_id):
            from app.utils import notion_queue

            notion_queue.enqueue(session_id, notion_queue.KIND_FULL)
    except Exception as e:
        log.warning("Could not queue a Notion update for %s: %s", session_id, e)


def set_names(session_id: str, changes_map: dict, *, from_ai: bool = False) -> dict[str, str]:
    """Set, change or clear speaker names (``{label: name or None}``). Returns the stored map.

    Raises KeyError when the recording has no transcript, ValueError for a label that isn't
    in the transcript or a bad name (nothing is saved then). ``from_ai`` (the MCP route):
    a name equal to the label's current voice-matched name changes nothing, since AI tools
    see names without their source and may send a match back; in the app, saving it
    accepts the match as typed.
    """
    validate_session_id(session_id)
    if not isinstance(changes_map, dict):
        raise ValueError("names must map speaker labels to names")
    from app.coaching.runner import labelling_fingerprint

    profile = profile_name()
    people = people_names(profile)
    path = _path(session_id)
    with get_transcript_lock(session_id):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(session_id) from None
        if not isinstance(data, dict):
            raise KeyError(session_id)
        segments = data.get("segments") or []
        labels = set(sn.speaker_labels(segments))
        cleaned: dict[str, str | None] = {}
        for label, name in changes_map.items():
            if not isinstance(label, str) or label not in labels:
                raise ValueError(f"No speaker called {label} in this recording")
            cleaned[label] = None if name is None else sn.validate_name(name)
        if from_ai:
            sidecar = coaching_store.load(session_id)
            matched = sn.voice_matched(data, sidecar, profile, people)
            shown = sn.effective_names(data, sidecar, profile, people)
            norm = people_store._norm_name
            cleaned = {
                label: name
                for label, name in cleaned.items()
                if not (name and label in matched and norm(name) == norm(shown[label]))
            }
        before = sn.stored_names(data)
        after = dict(before)
        for label, name in cleaned.items():
            if name is None:
                after.pop(label, None)
            else:
                after[label] = name
        # Keep the labels' order of appearance.
        after = {label: after[label] for label in sn.speaker_labels(segments) if label in after}
        moved = [label for label in cleaned if after.get(label) != before.get(label)]
        voice_before = _voice_fields(data)
        _corrections(data, moved, before, after, people, labelling_fingerprint(segments))
        changed = after != before or data.get("speaker_names") != after or _voice_fields(data) != voice_before
        if changed:
            data["speaker_names"] = after
            data["speaker_names_labelling"] = labelling_fingerprint(segments)
            _write_atomic(path, data)
    if after != before:
        _names_changed(session_id)
    _queue_learning(session_id, moved, after)
    return after


def _voice_fields(data: dict) -> str:
    return json.dumps([data.get("speaker_names_frozen"), data.get("speaker_voice_matches")], sort_keys=True)


def _corrections(data: dict, moved: list[str], before: dict, after: dict, people: dict, labelling: str) -> None:
    """Voice bookkeeping for each label whose typed name changed.

    Its frozen mark goes, and so does its voice match (a cleared name must not fall back
    to it). Clearing a known person's name (yours included) rejects them for the label,
    so their voice doesn't name it again. Typing a person's name lifts a rejection of
    them there. Naming a speaker that had no typed name, over a current match to someone
    else, rejects that person for the label (typing the matched person's own name
    accepts the match instead).
    """
    from app.people import apply

    def same(person, name) -> bool:
        known = people.get(person)
        return isinstance(known, str) and people_store._norm_name(known) == people_store._norm_name(name)

    frozen = data.get("speaker_names_frozen")
    if isinstance(frozen, list) and any(label in frozen for label in moved):
        data["speaker_names_frozen"] = [label for label in frozen if label not in moved]
    for label in moved:
        if after.get(label) is None and before.get(label):
            for person in [p for p in people if same(p, before[label])]:
                apply.reject_in(data, label, person)
    block = data.get("speaker_voice_matches")
    if not isinstance(block, dict):
        return
    matches = block.get("matches") if isinstance(block.get("matches"), dict) else {}
    rejected = block.get("rejected") if isinstance(block.get("rejected"), dict) else {}
    current = block.get("labelling") == labelling

    for label in moved:
        match = matches.pop(label, None)
        name = after.get(label)
        if name is None:
            continue
        if isinstance(rejected.get(label), list) and any(same(p, name) for p in rejected[label]):
            rejected[label] = [p for p in rejected[label] if not same(p, name)]
            if not rejected[label]:
                del rejected[label]
        person = match.get("person") if isinstance(match, dict) else None
        if current and before.get(label) is None and isinstance(person, str) and not same(person, name):
            apply.reject_in(data, label, person)


def _queue_learning(session_id: str, labels: list[str], after: dict) -> None:
    """Queue teaching (or forgetting) the voices of renamed speakers. Never raises."""
    try:
        from app.people import worker

        for label in labels:
            if after.get(label):
                worker.enqueue("learn", session_id=session_id, label=label, name=after[label])
            else:
                worker.enqueue("unlearn", session_id=session_id, label=label)
    except Exception as e:
        log.warning("Could not queue voice learning for %s: %s", session_id, e)


def ack_cleared(session_id: str) -> None:
    """Forget the "names were cleared" notice once the UI has shown it. KeyError without a transcript."""
    validate_session_id(session_id)
    path = _path(session_id)
    with get_transcript_lock(session_id):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(session_id) from None
        if isinstance(data, dict) and data.pop("speaker_names_cleared", None) is not None:
            _write_atomic(path, data)


def list_people() -> list[dict]:
    """Every name typed for a speaker across the library, with how many recordings use it.

    Only stored names count (your profile default isn't a person you named). Sorted by
    count, most first, then by name.
    """
    counts: dict[str, int] = {}
    if not TRANSCRIPTS_DIR.is_dir():
        return []
    for f in TRANSCRIPTS_DIR.glob("*.json"):
        data = _read(f.stem)
        if data is None:
            continue
        for name in set(sn.stored_names(data).values()):
            counts[name] = counts.get(name, 0) + 1
    return [{"name": n, "recordings": c} for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))]


# ---------------------------------------------------------------- participants for the list
#
# ``/recordings`` shows each recording's participants. Reading every transcript on every
# listing would be slow for a large library, so the answer is cached per recording and
# reused while the transcript, the coaching sidecar and the profile name are unchanged.

_cache_lock = Lock()
_participants_cache: dict[str, tuple[tuple, list[str]]] = {}


def _mtime(path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def participants(session_id: str, profile: str | None = None) -> list[str]:
    """The recording's named speakers (you included once confirmed), each once, in order of appearance."""
    if profile is None:
        profile = profile_name() or ""
    t_path = _path(session_id)
    key = (
        str(t_path),
        _mtime(t_path),
        _mtime(coaching_store.coaching_path(session_id)),
        profile,
        _mtime(people_store.VOICES_PATH),
    )
    with _cache_lock:
        hit = _participants_cache.get(session_id)
        if hit and hit[0] == key:
            return list(hit[1])
    if key[1] is None:
        result: list[str] = []
    else:
        transcript = _read(session_id)
        names = names_for(session_id, transcript, profile=profile) if transcript else {}
        result = list(dict.fromkeys(names.values()))
    with _cache_lock:
        _participants_cache[session_id] = (key, result)
    return list(result)


def owner_changed(session_id: str, before: dict | None, after: dict | None) -> None:
    """After a coaching owner change (a pick, mixed up, excluded): react if your name moved.

    Compares the effective names under the old and new sidecar; if they differ, the window
    and an already-linked Notion page are updated. Never raises. Call it without holding
    the transcript or coaching locks.
    """
    try:
        profile = profile_name()
        transcript = _read(session_id)
        if transcript is None:
            return
        people = people_names(profile)
        old = sn.effective_names(transcript, before or {}, profile, people)
        new = sn.effective_names(transcript, after or {}, profile, people)
        if old != new:
            _names_changed(session_id)
    except Exception as e:
        log.warning("Could not update speaker names after an owner change for %s: %s", session_id, e)
