"""The people directory: list, rename, merge and delete the people Open Pin knows by voice.

Changes to a person reach every transcript directly, one at a time under its transcript
lock, and never through ``speakers.set_names``: a rename is not a correction, so nothing
is learnt, forgotten or rejected. Each write holds the people worker's job lock
(``worker.exclusive``) throughout, so no voice job runs halfway through it. It checks
the store, edits the transcripts, then writes the store last (checking again), so one
stopped by a quit can simply be run again. The store lock and a transcript lock are
never held together. Nothing here returns a vector.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from app.coaching import runner, voice
from app.coaching import store as coaching_store
from app.people import apply, backlog, worker
from app.people import store as people_store
from app.state import speakers
from app.utils import changes
from app.utils import speaker_names as sn
from app.utils.config import load_config
from app.utils.transcript_locks import get_transcript_lock

ME = people_store.ME
_PERSON_ID = re.compile(r"^(me|p_[0-9a-f]{8})$")
_norm = people_store._norm_name


class DirectoryError(Exception):
    """A refused change, with the HTTP status and a message the user can read."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _check_id(person_id) -> str:
    if not isinstance(person_id, str) or not _PERSON_ID.match(person_id):
        raise DirectoryError("Invalid person id")
    return person_id


def _others(person_id) -> str:
    """A valid id that isn't you (your name is in Settings, your voice in "Your voice & coaching")."""
    if _check_id(person_id) == ME:
        raise DirectoryError("Your own name and voice are changed in Settings, not here")
    return person_id


def _person(data: dict, person_id: str) -> dict:
    person = data["people"].get(person_id)
    if person is None:
        raise DirectoryError("No such person", 404)
    return person


def _sessions() -> list[str]:
    folder = speakers.TRANSCRIPTS_DIR
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.json") if not p.name.startswith("."))


# ------------------------------------------------------------------- reading


def directory() -> dict:
    """You, the people, storage, the model, the backlog and the calibration. No vectors."""
    data = people_store.load()
    profile = speakers.profile_name()
    names = {pid: p.get("name") for pid, p in data["people"].items()}
    names[ME] = profile
    counts = _recording_counts(names, profile)
    rows = people_store.person_summary(data)
    for row in rows:
        row["recordings"] = counts.get(row["id"], 0)
    me = next(r for r in rows if r["id"] == ME)
    me["name"] = profile
    people = sorted((r for r in rows if r["id"] != ME), key=lambda r: (_norm(r["name"] or ""), r["id"]))
    return {
        "me": me,
        "people": people,
        "total_bytes": sum(r["bytes"] for r in rows),
        "model": voice.model_available(),
        "backlog": backlog.state(),
        "calibration": load_config().get("people_calibration") or None,
    }


def _recording_counts(names: dict[str, str | None], profile: str | None) -> dict[str, int]:
    """``{person id: transcripts where some label shows their name}``, in one pass."""
    by_name: dict[str, list[str]] = {}
    for pid, name in names.items():
        if name:
            by_name.setdefault(_norm(name), []).append(pid)
    people = speakers.people_names(profile)
    counts: dict[str, int] = {}
    for sid in _sessions():
        transcript = speakers._read(sid)
        if transcript is None:
            continue
        shown = sn.effective_names(transcript, coaching_store.load(sid), profile, people)
        for key in {_norm(n) for n in shown.values()}:
            for pid in by_name.get(key, ()):
                counts[pid] = counts.get(pid, 0) + 1
    return counts


def suggestions() -> list[dict]:
    """Names typed across the library (``speakers.list_people``) plus everyone known by voice.

    Each item is ``{name, recordings, person_id, has_voice}``: people with a voice first,
    then the rest, each most used first.
    """
    data = people_store.load()
    profile = speakers.profile_name()
    known: dict[str, tuple[str, bool]] = {}
    for pid, p in data["people"].items():
        name = profile if pid == ME else p.get("name")
        if name:
            known[_norm(name)] = (pid, bool(p["samples"]))
    items = []
    for item in speakers.list_people():
        pid, voiced = known.pop(_norm(item["name"]), (None, False))
        items.append({**item, "person_id": pid, "has_voice": voiced})
    for pid, voiced in known.values():
        if pid != ME:
            items.append({"name": data["people"][pid]["name"], "recordings": 0, "person_id": pid, "has_voice": voiced})
    return sorted(items, key=lambda i: (not i["has_voice"], -i["recordings"], i["name"].lower()))


# ------------------------------------------------------------------- transcripts


def _each_transcript(edit: Callable[[str, dict], bool], old_people: dict, new_people: dict) -> list[str]:
    """Run ``edit(session_id, data) -> changed`` on every transcript, one at a time under its lock.

    ``old_people`` and ``new_people`` are ``{id: name}`` for the store before and after the
    change the caller writes once this returns. A transcript whose shown names changed
    through the edit alone is told at once (``_names_changed``, after its lock); the ids of
    those whose shown names change only with the store write are returned, for the caller
    to tell after it.
    """
    profile = speakers.profile_name()
    old_people = {**old_people, ME: profile}
    new_people = {**new_people, ME: profile}
    later = []
    for sid in _sessions():
        path = speakers._path(sid)
        with get_transcript_lock(sid):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            sidecar = coaching_store.load(sid)
            before = sn.effective_names(data, sidecar, profile, old_people)
            if edit(sid, data):
                speakers._write_atomic(path, data)
            now = sn.effective_names(data, sidecar, profile, old_people)
            after = sn.effective_names(data, sidecar, profile, new_people)
        if now != before:
            speakers._names_changed(sid)
        if after != now:  # told again after the store write (the Notion queue collapses repeats)
            later.append(sid)
    return later


def _retype(data: dict, old_name: str, new_name: str) -> bool:
    """Typed names equal to ``old_name`` (case and spacing ignored) become ``new_name``."""
    typed = data.get("speaker_names")
    if not isinstance(typed, dict):
        return False
    hit = [label for label, name in typed.items() if isinstance(name, str) and _norm(name) == _norm(old_name)]
    for label in hit:
        typed[label] = new_name
    return bool(hit)


def _current_block(data: dict) -> dict | None:
    """The voice-match block if it is for the current labelling, else None."""
    from app.coaching.runner import labelling_fingerprint

    block = data.get("speaker_voice_matches")
    if not isinstance(block, dict) or block.get("labelling") != labelling_fingerprint(data.get("segments") or []):
        return None
    if not isinstance(block.get("matches"), dict):
        block["matches"] = {}
    if not isinstance(block.get("rejected"), dict):
        block["rejected"] = {}
    return block


def _repoint(data: dict, source: str, target: str) -> bool:
    """Matches and rejections of ``source`` become ``target``'s."""
    block = data.get("speaker_voice_matches")
    if not isinstance(block, dict):
        return False
    before = json.dumps(block, sort_keys=True)
    matches = block.get("matches") if isinstance(block.get("matches"), dict) else {}
    rejected = block.get("rejected") if isinstance(block.get("rejected"), dict) else {}
    for match in matches.values():
        if isinstance(match, dict) and match.get("person") == source:
            match["person"] = target
    for label, ids in rejected.items():
        if isinstance(ids, list) and source in ids:
            rejected[label] = list(dict.fromkeys(target if p == source else p for p in ids))
    # A label that turned down either of them turns down the merged person.
    for label, ids in rejected.items():
        match = matches.get(label)
        if isinstance(ids, list) and isinstance(match, dict) and match.get("person") in ids:
            del matches[label]
    return json.dumps(block, sort_keys=True) != before


def _drop_rejections(block: dict, person_id: str) -> None:
    rejected = block.get("rejected") if isinstance(block.get("rejected"), dict) else {}
    for label in list(rejected):
        ids = rejected[label]
        if isinstance(ids, list) and person_id in ids:
            ids = [p for p in ids if p != person_id]
            if ids:
                rejected[label] = ids
            else:
                del rejected[label]


# ------------------------------------------------------------------- changes


def _clean_name(name) -> str:
    try:
        name = " ".join(sn.validate_name(name).split())
    except ValueError as e:
        raise DirectoryError(str(e)) from None
    profile = speakers.profile_name()
    if profile and _norm(profile) == _norm(name):
        raise DirectoryError(f"{name} is your name. Your voice is in Your voice & coaching.", 409)
    return name


def _names(data: dict) -> dict[str, str | None]:
    return {pid: p.get("name") for pid, p in data["people"].items()}


def _check_rename(data: dict, person_id: str, name: str) -> dict:
    person = _person(data, person_id)
    other = people_store.find_person(data, name)
    if other and other != person_id:
        raise DirectoryError(f"{data['people'][other]['name']} is already in People. Merge them instead.", 409)
    return person


def rename(person_id: str, name) -> None:
    """Rename someone everywhere: the directory, and every typed name with their old name."""
    _others(person_id)
    name = _clean_name(name)
    with worker.exclusive():
        data = people_store.load()
        old_name = _check_rename(data, person_id, name).get("name")
        old_people = _names(data)

        def edit(sid: str, t: dict) -> bool:
            return bool(old_name) and _retype(t, old_name, name)

        later = _each_transcript(edit, old_people, {**old_people, person_id: name})
        people_store.mutate(lambda d: _check_rename(d, person_id, name).update(name=name))
        for sid in later:
            speakers._names_changed(sid)
    changes.bump()


def _check_merge(data: dict, person_id: str, into: str) -> tuple[dict, dict]:
    source, target = _person(data, person_id), _person(data, into)
    if target.get("name"):
        _clean_name(target["name"])  # refuses merging into your own name
    return source, target


def merge(person_id: str, into) -> None:
    """Fold ``person_id`` into ``into`` (``me`` for you): samples combine (trimmed to 20),
    matches and rejections move over, typed names take ``into``'s name (your profile
    name for you, which must be set), and ``person_id`` goes."""
    _others(person_id)
    if _check_id(into) == ME and not speakers.profile_name():
        raise DirectoryError("Add your name in Settings first", 409)
    if into == person_id:
        raise DirectoryError("Pick someone else to merge into")
    _merge(person_id, into, retype=True)


def fold_into_me() -> None:
    """After your profile name is saved: someone in People with that name is you, so fold
    them into ``me``. Typed names already read as you, so they stay as typed."""
    profile = speakers.profile_name()
    if not profile or not any(
        pid != ME and name and _norm(name) == _norm(profile) for pid, name in people_store.read_names().items()
    ):
        return
    with worker.exclusive():
        person_id = people_store.find_person(people_store.load(), profile)
        if person_id:
            _merge(person_id, ME, retype=False)


def _merge(person_id: str, into: str, *, retype: bool) -> None:
    with worker.exclusive():
        data = people_store.load()
        source, target = _check_merge(data, person_id, into)
        source_name = source.get("name")
        target_name = speakers.profile_name() if into == ME else target.get("name")
        retype = retype and bool(source_name and target_name)
        old_people = _names(data)

        def edit(sid: str, t: dict) -> bool:
            retyped = retype and _retype(t, source_name, target_name)
            return _repoint(t, person_id, into) or retyped

        new_people = {pid: n for pid, n in old_people.items() if pid != person_id}
        later = _each_transcript(edit, old_people, new_people)

        # Decided out here: is_excluded reads a sidecar, which mustn't happen under the store lock.
        excluded = {
            sid for s in source["samples"] if (sid := s.get("session_id")) and into == ME and runner.is_excluded(sid)
        }

        def combine(d: dict) -> None:
            source, target = _check_merge(d, person_id, into)
            target["samples"].extend(s for s in source["samples"] if s.get("session_id") not in excluded)
            del d["people"][person_id]

        people_store.mutate(combine)
        for sid in later:
            speakers._names_changed(sid)
    worker.enqueue("rematch")
    changes.bump()


def delete(person_id: str) -> None:
    """Forget someone's voice. Names their voice gave in past transcripts become typed,
    so what each transcript shows stays the same; those and the names typed with their
    name are frozen, so they never teach the voice back."""
    from app.coaching.runner import labelling_fingerprint

    _others(person_id)
    with worker.exclusive():
        data = people_store.load()
        name = _person(data, person_id).get("name")
        people = _names(data)
        profile = speakers.profile_name()

        def edit(sid: str, t: dict) -> bool:
            block = _current_block(t)
            before = json.dumps([t.get("speaker_names"), t.get("speaker_names_frozen"), block], sort_keys=True)
            typed = sn.stored_names(t)
            frozen = t.get("speaker_names_frozen") if isinstance(t.get("speaker_names_frozen"), list) else []
            hit = [label for label, n in typed.items() if name and _norm(n) == _norm(name)]
            if block is not None:
                shown = sn.voice_matched(t, coaching_store.load(sid), profile, {**people, ME: profile})
                for label, match in list(block["matches"].items()):
                    if not isinstance(match, dict) or match.get("person") != person_id:
                        continue
                    del block["matches"][label]
                    if label in shown and name and label not in typed:
                        typed[label] = name
                        hit.append(label)
                _drop_rejections(block, person_id)
            if typed != sn.stored_names(t):
                segments = t.get("segments") or []
                t["speaker_names"] = {label: typed[label] for label in sn.speaker_labels(segments) if label in typed}
                t["speaker_names_labelling"] = labelling_fingerprint(segments)
            if any(label not in frozen for label in hit):
                t["speaker_names_frozen"] = [*frozen, *(label for label in hit if label not in frozen)]
            return json.dumps([t.get("speaker_names"), t.get("speaker_names_frozen"), block], sort_keys=True) != before

        _each_transcript(edit, people, people)  # the shown names stay the same: Notion isn't told
        people_store.mutate(lambda d: d["people"].pop(person_id, None))
    changes.bump()


def reject(session_id: str, label) -> tuple[dict, dict]:
    """Not them: the matched person is never matched to this label on this recording again.

    Returns the transcript's ``(speaker_names, speaker_name_sources)`` afterwards. KeyError
    without a transcript; DirectoryError when the label has no current voice match.
    """
    transcript = speakers._read(session_id)
    if transcript is None:
        raise KeyError(session_id)
    block = _current_block(transcript) or {"matches": {}}
    match = block["matches"].get(label) if isinstance(label, str) else None
    person = match.get("person") if isinstance(match, dict) else None
    if not isinstance(person, str):
        raise DirectoryError("That speaker wasn't named by voice")
    apply.add_rejection(session_id, label, person)
    transcript = speakers._read(session_id) or {}
    return speakers.names_and_sources(session_id, transcript, profile=speakers.profile_name())
