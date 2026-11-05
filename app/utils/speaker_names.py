"""Speaker names: a {label: name} layer over a transcript's speaker labels.

Stored text never changes: segments keep their labels ("Speaker 2") and summaries
are stored in label form. Names are applied whenever text is shown, exported,
pushed to Notion or read by AI tools, by replacing whole-word occurrences of each
named label, so renaming is always reversible ("Speaker 1" never touches
"Speaker 10").

Your own name (the profile name) is filled in on your speaker only once you have
picked that speaker yourself in coaching, never from a voice match alone, and
never over a name you typed. See ``effective_names``.
"""

import re

MAX_NAME_CHARS = 80
_FORBIDDEN = (":", "*")  # break the "Speaker: text" lines and Markdown bold


def validate_name(name) -> str:
    """Return the trimmed name, or raise ValueError with a message the user can read."""
    if not isinstance(name, str):
        raise ValueError("A speaker name must be text")
    name = name.strip()
    if not name:
        raise ValueError("A speaker name can't be empty")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"A speaker name can be at most {MAX_NAME_CHARS} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("A speaker name can't contain control characters")
    if any(ch in name for ch in _FORBIDDEN):
        raise ValueError("A speaker name can't contain : or *")
    return name


def _whole_word(alternatives) -> re.Pattern:
    """One regex matching any of ``alternatives`` as a whole word, longest first."""
    ordered = sorted(alternatives, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(a) for a in ordered) + r")(?!\w)")


def apply_names(text: str, names: dict[str, str]) -> str:
    """Replace each named label with its name, whole words only, in one pass."""
    names = {k: v for k, v in (names or {}).items() if k and v}
    if not text or not names:
        return text
    return _whole_word(names).sub(lambda m: names[m.group(0)], text)


def names_to_labels(text: str, names: dict[str, str]) -> str:
    """The reverse of ``apply_names``: each name back to its label, whole words, longest names first.

    A name that two speakers share is left as typed, since it can't be told which one is meant.
    """
    names = {k: v for k, v in (names or {}).items() if k and v}
    if not text or not names:
        return text
    by_name: dict[str, list[str]] = {}
    for label, name in names.items():
        by_name.setdefault(name, []).append(label)
    reverse = {name: labels[0] for name, labels in by_name.items() if len(labels) == 1}
    if not reverse:
        return text
    return _whole_word(reverse).sub(lambda m: reverse[m.group(0)], text)


def speaker_labels(segments) -> list[str]:
    """Speaker labels in order of first appearance."""
    seen: dict[str, None] = {}
    for s in segments or []:
        label = s.get("speaker") if isinstance(s, dict) else None
        if label and isinstance(label, str):
            seen.setdefault(label, None)
    return list(seen)


def stored_names(transcript: dict) -> dict[str, str]:
    """The names saved on the transcript, for labels it still has."""
    raw = transcript.get("speaker_names")
    if not isinstance(raw, dict):
        return {}
    labels = speaker_labels(transcript.get("segments"))
    return {label: raw[label] for label in labels if isinstance(raw.get(label), str) and raw[label]}


def _owner_default_labels(transcript: dict, sidecar: dict | None) -> list[str]:
    """Labels the user picked as themselves for this labelling, if the pick still counts."""
    from app.coaching.runner import labelling_fingerprint

    sidecar = sidecar or {}
    owner = sidecar.get("owner")
    if not isinstance(owner, dict) or owner.get("source") != "user":
        return []
    if sidecar.get("mixed_up") is True or sidecar.get("excluded"):
        return []
    segments = transcript.get("segments") or []
    if owner.get("labelling") != labelling_fingerprint(segments):
        return []
    picked = owner.get("labels") or []
    return [label for label in speaker_labels(segments) if label in picked]


def effective_names(transcript: dict, sidecar: dict | None, profile_name: str | None) -> dict[str, str]:
    """The names to show: the stored ones, plus your profile name on your own speaker.

    The profile name goes on each owner label only when you picked that speaker yourself
    (owner ``source`` "user") for the current labelling, the recording isn't marked
    speakers mixed up or excluded from coaching, and you haven't typed a name for it.
    Ordered by each label's first appearance.
    """
    stored = stored_names(transcript)
    default = None
    if profile_name:
        try:
            default = validate_name(profile_name)
        except ValueError:
            default = None
    mine = set(_owner_default_labels(transcript, sidecar)) if default else set()
    out: dict[str, str] = {}
    for label in speaker_labels(transcript.get("segments")):
        if label in stored:
            out[label] = stored[label]
        elif label in mine:
            out[label] = default
    return out


def drop_if_relabelled(data: dict, old_segments) -> bool:
    """After a transcript's segments were replaced: drop the names if the speakers were detected again.

    ``data`` is the transcript dict with its new segments; ``old_segments`` the segments before.
    Names are kept when the labelling (``labelling_fingerprint``) is unchanged. When they are
    dropped, ``speaker_names_cleared`` is set so the UI can say so once. Returns True if dropped.
    """
    from app.coaching.runner import labelling_fingerprint

    if not data.get("speaker_names"):
        return False
    new = labelling_fingerprint(data.get("segments") or [])
    reference = data.get("speaker_names_labelling") or labelling_fingerprint(old_segments or [])
    if new == reference:
        data["speaker_names_labelling"] = new
        return False
    data["speaker_names"] = {}
    data.pop("speaker_names_labelling", None)
    data["speaker_names_cleared"] = True
    return True
