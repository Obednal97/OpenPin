"""Engagement: how well you keep people engaged, measured from one recording.

Three parts, each scored 0-100 against its target in targets.TARGETS, and a score that is
the rounded mean of the parts available (a missing part never counts as 0):

- Laughs after your lines: laughter events (from app/coaching/laughter.py) that start within
  LAUGH_AFTER_S after one of your turns ends, or overlap its last LAUGH_OVERLAP_S. A laugh at
  least as loud as your median speech loudness minus OWN_LAUGH_MARGIN_DB is probably yours
  (your clip-on mic hears you loudest) and is not counted. At most one laugh per turn.
- Follow-up questions to you: the other person's turn straight after one of yours has a "?"
  within its first FOLLOW_UP_WORDS words. Your turns under FOLLOW_UP_MIN_TURN_WORDS words
  (backchannels) don't count.
- Points others built on (AI review only): the share of the review's reactions to your points
  whose response is "engaged" or "follow_up".

Everything here is pure except ``owner_loudness_db``, which reads the audio in chunks.
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
FRAME_S = 0.025  # loudness frame, as the laughter detector's peak-frame loudness
READ_CHUNK_FRAMES = 16000 * 60  # samples read at a time (a minute at 16 kHz)
PARTS = ("laughs_per_hour", "follow_up_share", "built_on_share")


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _events(events) -> list[dict]:
    """Well-formed laughter events, by start time. Anything malformed is skipped."""
    out = [
        e
        for e in (events if isinstance(events, list) else [])
        if isinstance(e, dict) and _number(e.get("start")) and _number(e.get("end")) and _number(e.get("loudness_db"))
    ]
    return sorted(out, key=lambda e: e["start"])


def _laughs(turns: list[dict], events: list[dict], owner_db: float) -> list[dict]:
    """At most one laugh per turn of yours, each event used once, your own laughs left out."""
    used: set[int] = set()
    out = []
    for turn in turns:
        if not turn["owner"]:
            continue
        end = turn["end"]
        for i, e in enumerate(events):
            if i in used or e["start"] > end + T.LAUGH_AFTER_S or e["end"] < end - T.LAUGH_OVERLAP_S:
                continue
            if e["loudness_db"] >= owner_db - T.OWN_LAUGH_MARGIN_DB:
                continue  # probably you laughing
            used.add(i)
            out.append({"t": e["start"], "line_start": turn["start"], "line_text": _quote(turn["text"])})
            break
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
    count = sum(1 for r in reactions if r.get("response") in T.BUILT_ON_RESPONSES)
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
    owner_loudness_db: float | None,
) -> dict:
    """The sidecar's engagement block.

    ``follow_up_turns`` is the number of your turns that could be followed up (the share's
    denominator), so the UI can say "N of M of your turns".

    ``laughs`` are the laughter detector's events; they count only when ``laughter_status``
    is "done". ``review`` is the grounded AI review, or None when it is off or failed.
    ``owner_loudness_db`` is your median speech loudness (``owner_loudness_db``); without it
    laughs can't be told apart from yours, so the laughter part is unavailable (unless
    there were no laughs at all).
    """
    turns = owner_turns(segments, set(owner_labels))
    events = _events(laughs)
    status = DONE if laughter_status == DONE else UNAVAILABLE
    reason = "" if status == DONE else (laughter_reason or "")
    if status == DONE and events and not _number(owner_loudness_db):
        status, reason = UNAVAILABLE, NO_VOICE_LEVEL

    counted = _laughs(turns, events, owner_loudness_db) if status == DONE and events else []
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


def owner_loudness_db(audio_path: str, segments: list[dict], owner_labels) -> float | None:
    """Your median speech loudness in dBFS: per line of yours, its loudest 25 ms frame (RMS).

    Measured like the laughter detector's event loudness (peak-frame RMS), so the two compare.
    Reads only your lines, in chunks of READ_CHUNK_FRAMES samples, so an hour-long recording
    never sits in memory whole. None when there is nothing to measure or the audio can't be read.
    """
    owners = set(owner_labels)
    mine = [s for s in segments if s.get("speaker") in owners]
    if not mine:
        return None
    wav = None
    try:
        import numpy as np
        import soundfile as sf

        from src.transcribe import _to_wav16k

        wav = _to_wav16k(audio_path)
        if not wav:
            return None
        rate = sf.info(wav).samplerate
        frame = max(1, int(rate * FRAME_S))
        chunk_frames = max(frame, READ_CHUNK_FRAMES // frame * frame)
        peaks = []
        for s in mine:
            start = int(float(s["start"]) * rate)
            stop = int(float(s["end"]) * rate)
            peak = 0.0
            while start < stop:
                n = min(chunk_frames, stop - start)
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
            if peak > 0:
                peaks.append(20 * math.log10(peak))
        return round(statistics.median(peaks), 2) if peaks else None
    except Exception as e:
        log.warning("measuring your voice level failed: %s", e)
        return None
    finally:
        if wav:
            try:
                os.unlink(wav)
            except OSError:
                pass
