"""Recognising Notion pages that older app versions made for a recording.

Before v2.4.0 the app wrote pages without a Session ID: just a title, Tags,
and a body of ``<summary>\\n---\\n## Full Transcript\\n[mm:ss] **Speaker**:
text`` lines. When the local data is lost and the recordings are synced and
transcribed again, those pages can only be recognised by their content. This
module holds the content side of that: turning a page body or a local
transcript into a *fingerprint* (word shingles plus the last timestamp),
scoring two fingerprints against each other, deciding whether a set of scored
candidates is a confident match, a "needs review", or nothing, and the on-disk
cache of page fingerprints (``.notion_legacy_cache.json`` in the data dir).

It makes no Notion calls; ``app/routes/notion.py`` lists the database rows,
fetches pages and adopts them.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import struct
import tempfile
import threading
import time
import unicodedata
import zlib
from datetime import datetime

from app.utils.constants import DATA_DIR

CACHE_PATH = DATA_DIR / ".notion_legacy_cache.json"
CACHE_VERSION = 1

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
#
# The text score is the share of word 3-shingles two transcripts have in
# common, relative to the larger of the two (relative to the page alone when
# Notion truncated it). Two engines (Whisper then, Deepgram now) transcribing
# the same audio disagree on roughly one word in ten, which leaves about
# 0.4-0.75 of the 3-shingles shared after normalisation; unrelated recordings,
# even on the same topic with the same people, share well under 0.1 (stock
# phrases such as "I don't know" or "at the end of the day").

# Adopt the page automatically at or above this score (and only with a
# consistent duration and a clear lead over the runner-up). Set well above
# anything unrelated recordings reach, and below the bulk of real
# engine-vs-engine pairs.
CONFIDENT_SCORE = 0.35
# Below CONFIDENT_SCORE but at least this: plausibly the same recording with
# a much worse transcript, or the same text with no usable timestamps. The
# owner decides ("needs review"); nothing is created.
PLAUSIBLE_SCORE = 0.15
# A confident winner must beat the runner-up by this much; two pages that
# both match well (e.g. the old app pushed the recording twice) need review.
CLEAR_MARGIN = 0.15

# Durations: the page's last ``[mm:ss]`` stamp is the start of its last
# segment, so it must be within this much of the recording's last segment
# start or of its duration (the last segment can run up to ~30 s).
DURATION_ABS_TOLERANCE = 30.0  # seconds
DURATION_REL_TOLERANCE = 0.10  # of the reference duration

# The page can't have been written before the recording started. Allow a day
# for a device clock or time zone that was off.
CREATED_TIME_SLACK = 24 * 3600

# Too little text to judge by content: never matched (created as before).
MIN_SHINGLES = 12

SHINGLE_SIZE = 3
# Fingerprints keep at most this many shingle hashes (about two hours of
# speech). Longer ones keep only hashes divisible by a power-of-two ``mod``;
# both sides are sampled the same way before comparing, so the score stays an
# unbiased estimate.
MAX_SHINGLES = 16000

# Cache entries for rows that report no last-edited time are trusted this long.
CACHE_TTL_WITHOUT_EDIT_TIME = 24 * 3600

MATCH_CONFIDENT = "confident"
MATCH_REVIEW = "review"
MATCH_NONE = "none"

# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

TRANSCRIPT_HEADING_RE = re.compile(r"^#{1,6}[ \t]*Full Transcript[ \t]*$", re.M | re.I)
_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?::(\d{2}))?\]")
_SPEAKER_RE = re.compile(r"\*\*[^*\n]{1,80}\*\*:?")
# "Speaker 1: " / "Anna Smith: " at the start of a line (plain-text transcripts).
_LINE_SPEAKER_RE = re.compile(r"^[ \t]*(?:[Ss]peaker[ \t]*\d+|[A-Z][a-z]+(?:[ \t][A-Z][a-z]+){0,2}):[ \t]", re.M)
_NON_WORD_RE = re.compile(r"[^0-9a-z]+")

# fmt: off
# Hesitations one engine writes and the other drops.
_FILLERS = frozenset([
    "um", "umm", "uhm", "uh", "uhh", "er", "erm", "ah", "ahh", "hmm", "hm", "mm", "mmm", "mhm",
    "uhhuh", "uhuh", "huh", "eh", "oh",
])
_NUMBER_WORDS = {w: str(i) for i, w in enumerate([
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
])}
_NUMBER_WORDS.update({"thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "hundred": "100", "percent": "%"})
# Ordinals: "fifteenth" (one engine) and "15th" (the other) both become "15th".
_ORDINALS = [
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh",
    "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
    "twentieth",
]
# fmt: on
_NUMBER_WORDS.update({w: f"{i}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(i, 'th') }" for i, w in enumerate(_ORDINALS, 1)})
_CANONICAL = {"okay": "ok", "alright": "all right", "gonna": "going to", "wanna": "want to", "gotta": "got to"}


def normalise_words(text: str) -> list[str]:
    """Lower-case words of a transcript, with timestamps, speaker labels,
    punctuation, hesitations and spelling variants smoothed out."""
    if not text:
        return []
    text = _TIMESTAMP_RE.sub(" ", text)
    text = _SPEAKER_RE.sub(" ", text)
    text = _LINE_SPEAKER_RE.sub(" ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[’'`]", "", text)  # don't -> dont, either engine
    text = text.replace("%", " percent ")
    words = []
    for raw in _NON_WORD_RE.sub(" ", text).split():
        if raw in _FILLERS:
            continue
        mapped = _CANONICAL.get(raw) or _NUMBER_WORDS.get(raw) or raw
        words.extend(mapped.split())
    return words


def transcript_part(content: str) -> str:
    """The transcript part of a page body made by any app version.

    Legacy pages: everything after the last ``## Full Transcript`` heading
    (the summary comes before it). Current pages: the same heading inside the
    app section, up to the section's end rule. Without a heading the whole
    body is used.
    """
    if not content:
        return ""
    m = None
    for m in TRANSCRIPT_HEADING_RE.finditer(content):
        pass
    body = content[m.end() :] if m else content
    end = re.search(r"^\s*---\s*\n\s*\*End of Plaud V2 section\*", body, re.M)
    if end:
        body = body[: end.start()]
    return body


def last_timestamp(text: str) -> float | None:
    """Seconds of the latest ``[mm:ss]`` (or ``[h:mm:ss]``) stamp, or None.

    None too when there is at most one stamp: a single ``[00:00]`` line says
    nothing about the recording's length.
    """
    stamps = []
    for m in _TIMESTAMP_RE.finditer(text or ""):
        a, b, c = m.groups()
        stamps.append(int(a) * 3600 + int(b) * 60 + int(c) if c is not None else int(a) * 60 + int(b))
    if len(stamps) < 2:
        return None
    return float(max(stamps))


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def _shingle_hashes(words: list[str]) -> set[int]:
    k = SHINGLE_SIZE
    if len(words) < k:
        return {zlib.crc32(" ".join(words).encode())} if words else set()
    return {zlib.crc32(" ".join(words[i : i + k]).encode()) for i in range(len(words) - k + 1)}


def _sampled(hashes: set[int], mod: int) -> set[int]:
    return hashes if mod <= 1 else {h for h in hashes if h % mod == 0}


def _pack(hashes: set[int]) -> str:
    return base64.b64encode(struct.pack(f"<{len(hashes)}I", *sorted(hashes))).decode("ascii")


def _unpack(blob: str) -> set[int]:
    raw = base64.b64decode(blob.encode("ascii")) if blob else b""
    return set(struct.unpack(f"<{len(raw) // 4}I", raw[: len(raw) // 4 * 4]))


def fingerprint_text(text: str, last_ts: float | None = None) -> dict:
    """Fingerprint of transcript text: sampled shingle hashes, word count, mod."""
    words = normalise_words(text)
    hashes = _shingle_hashes(words)
    mod = 1
    while len(_sampled(hashes, mod)) > MAX_SHINGLES:
        mod *= 2
    sampled = _sampled(hashes, mod)
    return {
        "shingles": _pack(sampled),
        "count": len(hashes),
        "mod": mod,
        "words": len(words),
        "last_ts": last_ts,
    }


def fingerprint_page(content: str) -> dict:
    """Fingerprint of a Notion page body (its transcript part)."""
    part = transcript_part(content)
    return fingerprint_text(part, last_timestamp(part))


def local_text(t_data: dict) -> str:
    """The plain words of a local transcript (segments, else ``text``)."""
    segments = t_data.get("segments") if isinstance(t_data, dict) else None
    if isinstance(segments, list) and segments:
        return "\n".join(str(s.get("text", "")) for s in segments if isinstance(s, dict))
    return str((t_data or {}).get("text") or "")


def local_timing(t_data: dict) -> tuple[float | None, float | None]:
    """(last segment start, duration) of a local transcript, each or None."""
    last_start = None
    for seg in (t_data or {}).get("segments") or []:
        try:
            start = float(seg.get("start", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        last_start = start if last_start is None else max(last_start, start)
    try:
        duration = float((t_data or {}).get("duration") or 0) or None
    except (TypeError, ValueError):
        duration = None
    return last_start, duration


def fingerprint_local(t_data: dict) -> dict:
    fp = fingerprint_text(local_text(t_data))
    fp["last_start"], fp["duration"] = local_timing(t_data)
    return fp


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def text_score(a: dict, b: dict, b_truncated: bool = False) -> float:
    """Share of shingles in common, relative to the larger fingerprint.

    If ``b`` (a page) was truncated by Notion it is only a prefix, so the
    share is taken relative to ``b`` alone. 0.0 if either is too short to
    judge.
    """
    if min(a.get("count", 0), b.get("count", 0)) < MIN_SHINGLES:
        return 0.0
    mod = max(int(a.get("mod") or 1), int(b.get("mod") or 1))
    sa = _sampled(_unpack(a.get("shingles", "")), mod)
    sb = _sampled(_unpack(b.get("shingles", "")), mod)
    if not sa or not sb:
        return 0.0
    common = len(sa & sb)
    denom = len(sb) if b_truncated else max(len(sa), len(sb))
    return round(common / denom, 4) if denom else 0.0


def _within(value: float, ref: float) -> bool:
    return abs(value - ref) <= max(DURATION_ABS_TOLERANCE, DURATION_REL_TOLERANCE * ref)


def duration_consistent(page_last: float | None, refs) -> bool | None:
    """Whether a page's last stamp fits any of the reference times (seconds).

    None when either side is unknown. A page stamp far beyond the longest
    reference is never consistent.
    """
    refs = [float(r) for r in refs if isinstance(r, (int, float)) and r and r > 0]
    if page_last is None or not refs:
        return None
    return any(_within(page_last, r) for r in refs)


def parse_time(value) -> float | None:
    """Unix seconds from an ISO 8601 string (or a number), else None."""
    if isinstance(value, (int, float)) and value > 0:
        return float(value / 1000 if value > 1e11 else value)
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def session_start(session_id: str) -> float | None:
    """The recording's start time from its Session ID (a unix timestamp)."""
    if isinstance(session_id, str) and re.fullmatch(r"\d{9,10}", session_id):
        return float(session_id)
    return None


def score_candidate(local: dict, page: dict, session_id: str | None = None) -> dict | None:
    """Score one page fingerprint against a local one.

    Returns ``{"score", "duration_ok"}``, or None if the page is ruled out:
    a known duration that doesn't fit, or a page created before the recording.
    """
    if session_id:
        start = session_start(session_id)
        created = page.get("created")
        if start is not None and isinstance(created, (int, float)) and created < start - CREATED_TIME_SLACK:
            return None
    refs = [local.get("last_start"), local.get("duration")]
    if local.get("last_ts") is not None:  # comparing two pages
        refs.append(local["last_ts"])
    duration_ok = duration_consistent(page.get("last_ts"), refs)
    if duration_ok is False:
        return None
    return {"score": text_score(local, page, bool(page.get("truncated"))), "duration_ok": duration_ok}


def classify(scored: list[dict]) -> tuple[str, list[dict]]:
    """Decide on scored candidates (each with ``score`` and ``duration_ok``).

    Returns ``(MATCH_CONFIDENT, [winner])``, ``(MATCH_REVIEW, candidates)``
    (best first, at most three) or ``(MATCH_NONE, [])``.
    """
    plausible = sorted((c for c in scored if c["score"] >= PLAUSIBLE_SCORE), key=lambda c: -c["score"])
    if not plausible:
        return MATCH_NONE, []
    best = plausible[0]
    runner_up = plausible[1]["score"] if len(plausible) > 1 else 0.0
    if best["score"] >= CONFIDENT_SCORE and best["duration_ok"] is True and best["score"] - runner_up >= CLEAR_MARGIN:
        return MATCH_CONFIDENT, [best]
    return MATCH_REVIEW, plausible[:3]


# ---------------------------------------------------------------------------
# Fingerprint cache (``.notion_legacy_cache.json``)
# ---------------------------------------------------------------------------
#
# {"version": 1, "pages": {page_id: {"edited": <row last-edited time or
# null>, "fetched_at": ..., "db": ..., "url", "title", "created", "kind":
# "legacy" | "app", "session_id" (app pages), "truncated", "shingles", "count",
# "mod", "words", "last_ts"}}}. An entry is reused while the row's
# last-edited time is unchanged (or, if Notion reports none, for
# CACHE_TTL_WITHOUT_EDIT_TIME).

_cache_lock = threading.Lock()


def load_cache() -> dict:
    with _cache_lock:
        try:
            data = json.loads(CACHE_PATH.read_text())
        except (OSError, ValueError):
            return {"version": CACHE_VERSION, "pages": {}}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION or not isinstance(data.get("pages"), dict):
        return {"version": CACHE_VERSION, "pages": {}}
    return data


def save_cache(cache: dict) -> None:
    with _cache_lock:
        tmp_path = None
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".notion_legacy.", dir=str(CACHE_PATH.parent))
            with os.fdopen(fd, "w") as f:
                json.dump(cache, f)
            os.replace(tmp_path, CACHE_PATH)
        except OSError as e:
            logging.warning(f"Could not write {CACHE_PATH}: {e}")
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def cached_entry(cache: dict, page_id: str, edited: str | None, now: float | None = None) -> dict | None:
    """The cached fingerprint for ``page_id`` if still valid for ``edited``."""
    entry = cache.get("pages", {}).get(page_id)
    if not isinstance(entry, dict):
        return None
    if edited:
        return entry if entry.get("edited") == edited else None
    now = time.time() if now is None else now
    if entry.get("edited") is None and now - float(entry.get("fetched_at") or 0) < CACHE_TTL_WITHOUT_EDIT_TIME:
        return entry
    return None
