"""Write voice matches onto transcripts, and match a recording against the people store.

The transcript keeps ``speaker_voice_matches = {"labelling", "matches", "rejected"}``.
Matches are only ever written for the labelling they were computed on, and a person
in ``rejected[label]`` is never matched to that label again. The transcript lock and
the store lock are never held together, and neither is held while embedding.
"""

from __future__ import annotations

import json
import logging

from app.coaching import store as coaching_store
from app.people import embeddings, matching
from app.people import store as people_store
from app.state import speakers
from app.utils import speaker_names as sn
from app.utils.transcript_locks import get_transcript_lock

log = logging.getLogger(__name__)


def _block(data: dict, labelling: str) -> dict:
    """The transcript's voice-match block for ``labelling``, normalised in place.

    A block made for another labelling starts again empty, ``rejected`` included:
    after re-labelling, "Speaker 2" may be someone else.
    """
    block = data.get("speaker_voice_matches")
    if not isinstance(block, dict) or block.get("labelling") != labelling:
        block = {}
    matches = block.get("matches")
    rejected = block.get("rejected")
    block = {
        "labelling": labelling,
        "matches": matches if isinstance(matches, dict) else {},
        "rejected": rejected if isinstance(rejected, dict) else {},
    }
    data["speaker_voice_matches"] = block
    return block


def reject_in(data: dict, label: str, person_id: str) -> None:
    """Mark ``person_id`` as not ``label`` on a transcript dict, dropping that match."""
    from app.coaching.runner import labelling_fingerprint

    block = _block(data, labelling_fingerprint(data.get("segments") or []))
    people = block["rejected"].setdefault(label, [])
    if person_id not in people:
        people.append(person_id)
    match = block["matches"].get(label)
    if isinstance(match, dict) and match.get("person") == person_id:
        del block["matches"][label]


def _edit(session_id: str, edit, bump: bool = True) -> bool:
    """Run ``edit(data) -> bool`` (False: no write) under the transcript lock.

    Returns whether the effective names changed; if so the window (unless ``bump`` is
    False) and Notion are told, after the lock is released.
    """
    profile = speakers.profile_name()
    path = speakers._path(session_id)
    with get_transcript_lock(session_id):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        before = speakers.names_for(session_id, data, profile=profile)
        if edit(data) is False:
            return False
        speakers._write_atomic(path, data)
        changed = speakers.names_for(session_id, data, profile=profile) != before
    if changed:
        speakers._names_changed(session_id, bump=bump)
    return changed


def apply_matches(session_id: str, labelling: str, matches: dict, bump: bool = True) -> bool:
    """Save ``matches`` for ``labelling``; dropped if the speakers were re-labelled since.

    Keeps ``rejected`` for the same labelling (a new labelling starts it again), and never
    saves a match to a person rejected for that label meanwhile. Returns whether the
    effective names changed.
    """
    from app.coaching.runner import labelling_fingerprint

    def edit(data: dict) -> bool:
        if labelling_fingerprint(data.get("segments") or []) != labelling:
            return False
        old = data.get("speaker_voice_matches")
        if not isinstance(old, dict) and not matches:
            return False
        block = _block(data, labelling)
        rejected = block["rejected"]
        block["matches"] = {
            label: m
            for label, m in matches.items()
            if not (isinstance(rejected.get(label), list) and m.get("person") in rejected[label])
        }
        return block != old

    return _edit(session_id, edit, bump)


def add_rejection(session_id: str, label: str, person_id: str) -> None:
    """Never match ``person_id`` to ``label`` on this recording again."""

    def edit(data: dict) -> bool:
        before = json.dumps(data.get("speaker_voice_matches"), sort_keys=True)
        reject_in(data, label, person_id)
        return json.dumps(data["speaker_voice_matches"], sort_keys=True) != before

    _edit(session_id, edit)


def match_recording(
    session_id: str,
    audio_path: str | None = None,
    *,
    cached_only: bool = False,
    people: dict | None = None,
    bump: bool = True,
) -> bool:
    """Match a recording's speakers to known people and save the result.

    A recording is never named from its own samples (they came from its typed names).
    Skipped before the threshold is calibrated, on a recording marked mixed up, and
    without labels or embeddings. ``cached_only`` uses the embedding cache and never
    audio; ``people`` (the store's people, loaded once by a caller matching many
    recordings) saves re-reading the store; ``bump`` False leaves telling the window to
    the caller. Returns whether the effective names changed.
    """
    from app.coaching.runner import labelling_fingerprint

    threshold = matching.current_threshold()
    if threshold is None:
        return False
    transcript = speakers._read(session_id)
    if transcript is None or coaching_store.load(session_id).get("mixed_up") is True:
        return False
    segments = transcript.get("segments") or []
    if not sn.speaker_labels(segments):
        return False
    labelling = labelling_fingerprint(segments)
    if cached_only:
        emb = embeddings.cached(session_id, segments)
    else:
        emb = embeddings.get(session_id, segments, audio_path)
    if not emb:
        return False
    rejected = _block(dict(transcript), labelling)["rejected"]
    rejected = {k: v for k, v in rejected.items() if isinstance(v, list)}
    if people is None:
        people = people_store.load()["people"]
    found = matching.match(emb, people, threshold, rejected, exclude_session=session_id)
    found = {label: {"person": m["person"], "score": round(m["score"], 4)} for label, m in found.items()}
    return apply_matches(session_id, labelling, found, bump)
