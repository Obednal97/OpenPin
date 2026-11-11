"""Speaker names: a {label: name} layer over a transcript's speaker labels.

Stored text never changes: segments keep their labels ("Speaker 2") and summaries
are stored in label form. Names are applied whenever text is shown, exported,
pushed to Notion or read by AI tools, by replacing whole-word occurrences of each
named label, so renaming is always reversible ("Speaker 1" never touches
"Speaker 10").

A label's name comes, in order, from: a name you typed; your own pick in coaching
(your profile name goes on the speaker you picked); a voice match recorded on the
transcript for the current labelling (a person's name, or your profile name for
you), unless the recording is marked speakers mixed up; otherwise nothing, and the
label shows. See ``effective_names``.
"""

import re

MAX_NAME_CHARS = 80
_FORBIDDEN = (":", "*")  # break the "Speaker: text" lines and Markdown bold
# A name that looks like a label ("Speaker 2") would be read back as that label when a
# summary is saved, so it would silently point at another speaker.
_LABEL_LIKE = re.compile(r"speaker\s*\d+", re.IGNORECASE)


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
    if _LABEL_LIKE.fullmatch(name):
        raise ValueError("A speaker name can't look like a speaker label, such as Speaker 2")
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


def confirmed_owner_labels(transcript: dict, sidecar: dict | None) -> list[str]:
    """The labels you picked as yourself, while that pick still counts (never a voice match alone)."""
    return _owner_default_labels(transcript, sidecar)


def _voice_matches(transcript: dict, sidecar: dict | None) -> dict:
    """The recorded voice matches ``{label: {person, score}}`` if they still count, else {}."""
    from app.coaching.runner import labelling_fingerprint

    block = transcript.get("speaker_voice_matches")
    if not isinstance(block, dict) or (sidecar or {}).get("mixed_up") is True:
        return {}
    segments = transcript.get("segments") or []
    if block.get("labelling") != labelling_fingerprint(segments):
        return {}
    matches = block.get("matches")
    return matches if isinstance(matches, dict) else {}


def _voice_name(match, profile_name: str | None, people_names: dict | None) -> str | None:
    """The valid name a voice match gives, or None (deleted person, no name)."""
    person = match.get("person") if isinstance(match, dict) else None
    if not isinstance(person, str):
        return None
    if person == "me":
        name = profile_name
    elif person in (people_names or {}):
        name = people_names[person]
    else:
        return None
    if not isinstance(name, str):
        return None
    try:
        return validate_name(name)
    except ValueError:
        return None


def _profile_default(profile_name: str | None) -> str | None:
    if not profile_name:
        return None
    try:
        return validate_name(profile_name)
    except ValueError:
        return None


def effective_names(
    transcript: dict,
    sidecar: dict | None,
    profile_name: str | None,
    people_names: dict[str, str | None] | None = None,
) -> dict[str, str]:
    """The names to show, per label in order of first appearance.

    1. a name you typed;
    2. your profile name on a label you picked as yourself (owner ``source`` "user") for
       the current labelling, when the recording isn't mixed up or excluded from coaching;
    3. a voice match valid for the current labelling, when the recording isn't marked
       mixed up: the person's name (your profile name for ``me``). A match to a deleted
       person, or to someone with no name, gives nothing. Excluding a recording from
       coaching does not stop this: naming isn't coaching.
    """
    stored = stored_names(transcript)
    default = _profile_default(profile_name)
    mine = set(_owner_default_labels(transcript, sidecar)) if default else set()
    matches = _voice_matches(transcript, sidecar)
    out: dict[str, str] = {}
    for label in speaker_labels(transcript.get("segments")):
        if label in stored:
            out[label] = stored[label]
        elif label in mine:
            out[label] = default
        else:
            name = _voice_name(matches.get(label), default, people_names)
            if name:
                out[label] = name
    return out


def voice_matched(
    transcript: dict,
    sidecar: dict | None,
    profile_name: str | None,
    people_names: dict[str, str | None] | None = None,
) -> dict[str, float]:
    """Labels whose effective name came from a voice match, with the match score."""
    default = _profile_default(profile_name)
    stored = stored_names(transcript)
    mine = set(_owner_default_labels(transcript, sidecar)) if default else set()
    matches = _voice_matches(transcript, sidecar)
    out: dict[str, float] = {}
    for label in speaker_labels(transcript.get("segments")):
        if label in stored or label in mine:
            continue
        m = matches.get(label)
        if _voice_name(m, default, people_names):
            score = m.get("score")
            out[label] = float(score) if isinstance(score, (int, float)) else 0.0
    return out


def drop_if_relabelled(data: dict, old_segments) -> bool:
    """After a transcript's segments were replaced: drop the names if the speakers were detected again.

    ``data`` is the transcript dict with its new segments; ``old_segments`` the segments before.
    Names are kept when the labelling (``labelling_fingerprint``) matches the one they were
    given for. Segments without any labels drop them too: callers whose labels are still
    coming (background diarisation pending) must not call this until they arrive. When names
    are dropped, ``speaker_names_cleared`` is set so the UI can say so once. Returns True if
    dropped.
    """
    from app.coaching.runner import labelling_fingerprint

    if not data.get("speaker_names"):
        return False
    segments = data.get("segments") or []
    new = labelling_fingerprint(segments)
    reference = data.get("speaker_names_labelling") or labelling_fingerprint(old_segments or [])
    if speaker_labels(segments) and new == reference:
        data["speaker_names_labelling"] = new
        return False
    data["speaker_names"] = {}
    data.pop("speaker_names_labelling", None)
    data["speaker_names_cleared"] = True
    return True
