"""Engagement: how well you keep people engaged, measured from one recording.

Three parts, each scored 0-100 against its target in targets.TARGETS, and a score that is
the rounded mean of the parts available (a missing part never counts as 0):

- Laughs after your lines: laughter events (from app/coaching/laughter.py) that start within
  LAUGH_AFTER_S after one of your lines (transcript segments) ends, or overlap its last
  LAUGH_OVERLAP_S. Lines, not merged turns: a laugh between two of your lines has no segment of
  its own, so the turn merges straight over it. Each is a candidate for that line. Its loudness
  is measured again from the audio over just the part after your line: from max(event start -
  LAUGH_MEASURE_PAD_S, your line's end) to event end + LAUGH_MEASURE_PAD_S, cut at your next
  line. (The detector's own loudness covers its 2 s windows, which reach back into your last
  words, so it would make every laugh straight after you look like yours.) A candidate with
  under LAUGH_MIN_MEASURE_S of audio left isn't counted. A laugh at least as loud as your median
  speech loudness minus OWN_LAUGH_MARGIN_DB is probably yours (your clip-on mic hears you
  loudest) and is not counted. At most one laugh per line.
- Follow-up questions to you: the other person's turn straight after one of yours has a "?"
  within its first FOLLOW_UP_WORDS words. Your turns under FOLLOW_UP_MIN_TURN_WORDS words
  (backchannels) don't count.
- Points others built on (AI review only): the share of the review's reactions to your points
  whose response is "engaged" or "follow_up".

Everything here is pure except ``measure_loudness``, which reads the audio in chunks.
All times are seconds.
"""

import logging
import math
import os
import statistics

from app.coaching import targets as T
from app.coaching.metrics import _quote, owner_turns

log = logging.getLogger(__name__)

DONE = "done"
UNAVAILABLE = "unavailable"
NO_VOICE_LEVEL = "Couldn't measure your voice level, so laughs can't be told apart from yours"
FRAME_S = 0.025  # loudness frame: the loudest 25 ms frame's RMS, for your lines and for laughs
READ_CHUNK_FRAMES = 16000 * 60  # samples read at a time (a minute at 16 kHz)
PARTS = ("laughs_per_hour", "follow_up_share", "built_on_share")


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _events(events) -> list[dict]:
    """Well-formed laughter events, by start time. Anything malformed is skipped."""
    out = [
        e
        for e in (events if isinstance(events, list) else [])
        if isinstance(e, dict) and _number(e.get("start")) and _number(e.get("end")) and e["end"] >= e["start"]
    ]
    return sorted(out, key=lambda e: e["start"])


def _your_lines(segments: list[dict], owner_labels) -> list[dict]:
    owners = set(owner_labels)
    lines = [
        s
        for s in segments
        if isinstance(s, dict) and s.get("speaker") in owners and _number(s.get("start")) and _number(s.get("end"))
    ]
    return sorted(lines, key=lambda s: s["start"])


def laugh_candidates(segments: list[dict], owner_labels, events) -> list[dict]:
    """Every (line of yours, laughter event) pair that could be a laugh after your line.

    Anchored on your transcript segments, not merged turns. In line order, then event order.
    Each is ``{"line", "event", "t", "line_start", "line_text", "window"}``: ``line`` counts
    your lines, ``event`` indexes the well-formed events by start, and ``window`` is
    ``(start, end)``, the stretch to measure the laugh's loudness over (see the module
    docstring). Pairs with under LAUGH_MIN_MEASURE_S to measure are left out. Pure: no audio
    is read.
    """
    lines = _your_lines(segments, owner_labels)
    evs = _events(events)
    out = []
    for n, line in enumerate(lines):
        end = line["end"]
        next_line = lines[n + 1]["start"] if n + 1 < len(lines) else math.inf
        for i, e in enumerate(evs):
            if e["start"] > end + T.LAUGH_AFTER_S or e["end"] < end - T.LAUGH_OVERLAP_S:
                continue
            window = (max(e["start"] - T.LAUGH_MEASURE_PAD_S, end), min(e["end"] + T.LAUGH_MEASURE_PAD_S, next_line))
            if window[1] - window[0] < T.LAUGH_MIN_MEASURE_S:
                continue
            out.append(
                {
                    "line": n,
                    "event": i,
                    "t": e["start"],
                    "line_start": line["start"],
                    "line_text": _quote(str(line.get("text") or "")),
                    "window": window,
                }
            )
    return out


def _laughs(candidates: list[dict], window_dbs: list, owner_db: float) -> list[dict]:
    """At most one laugh per line of yours, each event used once, your own laughs left out.

    Only the loudness measured over each candidate's window counts; a window that couldn't
    be measured is never counted."""
    used_lines: set[int] = set()
    used_events: set[int] = set()
    out = []
    for c, db in zip(candidates, window_dbs, strict=True):
        if c["line"] in used_lines or c["event"] in used_events or not _number(db):
            continue
        if db >= owner_db - T.OWN_LAUGH_MARGIN_DB:
            continue  # probably you laughing
        used_lines.add(c["line"])
        used_events.add(c["event"])
        out.append({"t": c["t"], "line_start": c["line_start"], "line_text": c["line_text"]})
    return sorted(out, key=lambda x: x["t"])


def _follow_ups(turns: list[dict]) -> tuple[list[dict], int]:
    """(follow-up questions to you, your qualifying turns)."""
    out, qualifying = [], 0
    for i, turn in enumerate(turns):
        if not turn["owner"] or len(turn["text"].split()) < T.FOLLOW_UP_MIN_TURN_WORDS:
            continue
        qualifying += 1
        nxt = turns[i + 1] if i + 1 < len(turns) else None
        if nxt and not nxt["owner"] and "?" in " ".join(nxt["text"].split()[: T.FOLLOW_UP_WORDS]):
            out.append({"t": nxt["start"], "text": _quote(nxt["text"])})
    return out, qualifying


def _built_on(review) -> dict | None:
    reactions = review.get("reactions") if isinstance(review, dict) else None
    reactions = [r for r in reactions if isinstance(r, dict)] if isinstance(reactions, list) else []
    if len(reactions) < T.ENGAGEMENT_MIN_BASE:
        return None
    # Only string responses count: a malformed one (a list, a dict) is never "built on" and never raises.
    count = sum(1 for r in reactions if isinstance(r.get("response"), str) and r["response"] in T.BUILT_ON_RESPONSES)
    return {"count": count, "of": len(reactions)}


def _part_score(key: str, value: float | None) -> float | None:
    """0-100: 100 at or above the target's "low", linear down to 0 at 0."""
    if value is None:
        return None
    target = T.TARGETS[key]["low"]
    return max(0.0, min(1.0, value / target)) * 100


def compute(
    segments: list[dict],
    owner_labels,
    laughs: list[dict],
    laughter_status: str,
    laughter_reason: str,
    review: dict | None,
    duration: float,
    loudness: dict | None,
) -> dict:
    """The sidecar's engagement block.

    ``follow_up_turns`` is the number of your turns that could be followed up (the share's
    denominator), so the UI can say "N of M of your turns".

    ``laughs`` are the laughter detector's events; they count only when ``laughter_status``
    is "done". ``review`` is the grounded AI review, or None when it is off or failed.
    ``loudness`` is ``measure_loudness`` for the windows of ``laugh_candidates`` (same
    arguments): your median speech loudness and each candidate's. Without it laughs can't be
    told apart from yours, so the laughter part is unavailable (unless there were no
    candidates at all).
    """
    turns = owner_turns(segments, set(owner_labels))
    status = DONE if laughter_status == DONE else UNAVAILABLE
    reason = "" if status == DONE else (laughter_reason or "")
    candidates = laugh_candidates(segments, owner_labels, laughs) if status == DONE else []
    owner_db = loudness.get("owner_db") if isinstance(loudness, dict) else None
    window_dbs = loudness.get("windows") if isinstance(loudness, dict) else None
    if candidates and (not _number(owner_db) or not isinstance(window_dbs, list) or len(window_dbs) != len(candidates)):
        status, reason, candidates = UNAVAILABLE, NO_VOICE_LEVEL, []

    counted = _laughs(candidates, window_dbs, owner_db) if candidates else []
    if status == DONE:
        laughs_count = len(counted)
        laughs_per_hour = round(laughs_count / (duration / 3600), 2) if duration > 0 else None
    else:
        laughs_count = laughs_per_hour = None

    follow_ups, qualifying = _follow_ups(turns)
    follow_up_share = round(len(follow_ups) / qualifying, 4) if qualifying >= T.ENGAGEMENT_MIN_BASE else None

    built_on = _built_on(review)
    built_on_share = round(built_on["count"] / built_on["of"], 4) if built_on else None

    parts = [
        _part_score(k, v)
        for k, v in zip(PARTS, (laughs_per_hour, follow_up_share, built_on_share), strict=True)
        if v is not None
    ]
    return {
        "score": round(statistics.mean(parts)) if parts else None,
        "laughs": counted,
        "laughs_count": laughs_count,
        "laughs_per_hour": laughs_per_hour,
        "follow_ups": follow_ups,
        "follow_up_share": follow_up_share,
        "follow_up_turns": qualifying,
        "built_on": built_on,
        "built_on_share": built_on_share,
        "laughter_status": status,
        "laughter_reason": reason,
    }


def _peak_db(sf, np, wav: str, rate: int, start_s: float, end_s: float, frame: int, chunk: int) -> float | None:
    """The loudest 25 ms frame's RMS in dBFS between two times, read in chunks. None if silent or empty."""
    start, stop = max(0, int(start_s * rate)), int(end_s * rate)
    peak = 0.0
    while start < stop:
        n = min(chunk, stop - start)
        data, _ = sf.read(wav, start=start, frames=n, dtype="float32")
        start += n
        if not len(data):
            break
        if data.ndim > 1:
            data = data.mean(axis=1)
        usable = len(data) // frame * frame
        if usable:
            rms = np.sqrt(np.mean(np.square(data[:usable].reshape(-1, frame), dtype=np.float64), axis=1))
        else:
            rms = np.sqrt(np.mean(np.square(data, dtype=np.float64), keepdims=True))
        peak = max(peak, float(rms.max()))
    return 20 * math.log10(peak) if peak > 0 else None


def measure_loudness(
    audio_path: str, segments: list[dict], owner_labels, windows, wav: str | None = None
) -> dict | None:
    """Your median speech loudness and each laugh window's, from one conversion of the audio.

    Returns ``{"owner_db": float | None, "windows": [float | None per window]}`` in dBFS:
    the loudest 25 ms frame's RMS, per line of yours (then the median) and per window
    (``laugh_candidates``' windows). Reads in chunks of READ_CHUNK_FRAMES samples, so an
    hour-long recording never sits in memory whole. None when the audio can't be converted
    or read.

    ``wav`` is a 16 kHz WAV already converted from ``audio_path`` (the laughter detector's,
    with ``keep_wav``): it is read instead of converting again, and the caller deletes it.
    Otherwise the audio is converted here and the temporary WAV deleted.
    """
    mine = _your_lines(segments, owner_labels)
    given = wav
    try:
        import numpy as np
        import soundfile as sf

        if not wav:
            from src.transcribe import _to_wav16k

            wav = _to_wav16k(audio_path)
        if not wav:
            return None
        rate = sf.info(wav).samplerate
        frame = max(1, int(rate * FRAME_S))
        chunk = max(frame, READ_CHUNK_FRAMES // frame * frame)
        lines = [_peak_db(sf, np, wav, rate, float(s["start"]), float(s["end"]), frame, chunk) for s in mine]
        lines = [db for db in lines if db is not None]
        dbs = [_peak_db(sf, np, wav, rate, float(a), float(b), frame, chunk) for a, b in windows]
        return {
            "owner_db": round(statistics.median(lines), 2) if lines else None,
            "windows": [None if db is None else round(db, 2) for db in dbs],
        }
    except Exception as e:
        log.warning("measuring loudness for laughter failed: %s", e)
        return None
    finally:
        if wav and not given:
            try:
                os.unlink(wav)
            except OSError:
                pass
