"""Measured speaking metrics for a recording (pure computation, no I/O).

Inputs are the clean transcript segments and, optionally, Deepgram's verbatim
words (which include fillers). All target values come from targets.py.
"""

import re
import statistics

from app.coaching import targets as T

UNKNOWN = "Unknown"
QUOTE_CHARS = 120
RUN_GAP_S = 1.0  # word gaps up to this still count as speaking time
FAST_MIN_TURN_S = 10.0  # shortest owner turn judged for a "fast" stretch
FAST_FACTOR = 1.2  # a fast stretch exceeds the pace target's upper bound by this factor
BURST_FILLERS = 3  # fillers within BURST_WINDOW_S make a "filler_burst" moment
BURST_WINDOW_S = 10.0
SWITCH_WINDOW_S = 300.0
_SENTENCE_END = (".", "?", "!")
_PHRASES = [p.split() for p in T.FILLER_PHRASES]
_PAIRS = {tuple(p) for p in _PHRASES if len(p) == 2}
_SINGLES = {p[0] for p in _PHRASES if len(p) == 1 and p[0] != "like"}


def _norm(text: str) -> list[str]:
    return [w for w in re.sub(r"[^\w\s'-]", " ", text.lower()).split() if w]


def _is_backchannel(text: str) -> bool:
    ws = _norm(text)
    return bool(ws) and all(w in T.BACKCHANNELS for w in ws)


def _cut_off(text: str) -> bool:
    return not text.rstrip().endswith(_SENTENCE_END)


def _quote(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= QUOTE_CHARS else text[: QUOTE_CHARS - 1].rstrip() + "…"


def owner_turns(segments: list[dict], owner_labels: set[str]) -> list[dict]:
    """Return ALL turns in time order, both the owner's and everyone else's.

    Each turn is {"start", "end", "text", "speaker", "owner"}. Consecutive segments on the
    same side (owner vs not-owner) merge into one turn, so two owner labels back to back, or
    two different other speakers back to back, are each one turn. A same-side turn is split
    when the gap between its consecutive segments exceeds targets.TURN_SPLIT_GAP_S. "speaker" is the label of
    the turn's first segment; "owner" is True when that side is the owner. Segments with no
    speaker label count as not-owner.
    """
    turns: list[dict] = []
    for s in sorted(segments, key=lambda x: x["start"]):
        is_owner = s.get("speaker") in owner_labels
        if turns and turns[-1]["owner"] == is_owner and s["start"] - turns[-1]["end"] <= T.TURN_SPLIT_GAP_S:
            t = turns[-1]
            t["end"] = max(t["end"], s["end"])
            t["text"] = f"{t['text']} {s['text']}".strip()
        else:
            turns.append(
                {
                    "start": s["start"],
                    "end": s["end"],
                    "text": s["text"].strip(),
                    "speaker": s.get("speaker"),
                    "owner": is_owner,
                }
            )
    return turns


def _speaking_time(spans: list[tuple[float, float]]) -> float:
    """Total time covered by spans, joining spans whose gap is at most RUN_GAP_S."""
    total, run_start, run_end = 0.0, None, None
    for a, b in sorted(spans):
        if run_end is not None and a - run_end <= RUN_GAP_S:
            run_end = max(run_end, b)
            continue
        if run_end is not None:
            total += run_end - run_start
        run_start, run_end = a, b
    if run_end is not None:
        total += run_end - run_start
    return total


def _per_speaker(segments: list[dict], words: list[dict] | None) -> dict:
    spans: dict[str, list[tuple[float, float]]] = {}
    counts: dict[str, int] = {}
    if words:
        for w in words:
            label = w.get("speaker") or UNKNOWN
            spans.setdefault(label, []).append((w["start"], w["end"]))
            counts[label] = counts.get(label, 0) + 1
    else:
        for s in segments:
            label = s.get("speaker") or UNKNOWN
            spans.setdefault(label, []).append((s["start"], s["end"]))
            counts[label] = counts.get(label, 0) + len(s["text"].split())
    turns: dict[str, int] = {}
    prev = object()
    for s in sorted(segments, key=lambda x: x["start"]):
        label = s.get("speaker") or UNKNOWN
        if label != prev:
            turns[label] = turns.get(label, 0) + 1
        prev = label
    talk = {k: _speaking_time(v) if words else sum(b - a for a, b in v) for k, v in spans.items()}
    total = sum(talk.values())
    return {
        k: {
            "wpm": round(counts[k] / (talk[k] / 60), 2) if talk[k] > 0 else None,
            "talk_s": round(talk[k], 3),
            "talk_share": round(talk[k] / total, 4) if total > 0 else 0.0,
            "turns": turns.get(k, 0),
            "words": counts[k],
        }
        for k in talk
    }


def _filler_hits(owner_words: list[dict]) -> list[float]:
    """Start times of owner fillers, from verbatim words. Approximate: see targets.FILLER_PHRASES."""
    toks = []
    for w in owner_words:
        raw = (w.get("punctuated_word") or w.get("word") or "").strip()
        norm = _norm(raw)
        toks.append((norm[0] if norm else "", raw.endswith(","), w["start"]))
    hit = [False] * len(toks)
    starts: dict[int, float] = {}
    i = 0
    while i < len(toks):
        if i + 1 < len(toks) and (toks[i][0], toks[i + 1][0]) in _PAIRS:
            hit[i] = hit[i + 1] = True
            starts[i] = toks[i][2]
            i += 2
            continue
        if toks[i][0] in _SINGLES:
            hit[i] = True
            starts[i] = toks[i][2]
        i += 1
    # "like" counts after a comma, or between two other fillers.
    for i, (w, comma, t) in enumerate(toks):
        if w != "like":
            continue
        between = 0 < i < len(toks) - 1 and hit[i - 1] and hit[i + 1]
        if comma or between:
            starts[i] = t
    return [starts[i] for i in sorted(starts)]


def _bursts(times: list[float]) -> list[float]:
    out, i = [], 0
    while i + BURST_FILLERS - 1 < len(times):
        if times[i + BURST_FILLERS - 1] - times[i] <= BURST_WINDOW_S:
            out.append(times[i])
            i += BURST_FILLERS
        else:
            i += 1
    return out


def _interruptions(turns: list[dict]) -> tuple[list[dict], list[dict]]:
    """(made by owner, made by others) as the interrupting turns, with the cut-off turn before them."""
    made, by_others = [], []
    for prev, cur in zip(turns, turns[1:], strict=False):
        if prev["owner"] == cur["owner"]:
            continue
        gap = cur["start"] - prev["end"]
        if gap <= T.INTERRUPT_MAX_GAP_S and _cut_off(prev["text"]) and not _is_backchannel(cur["text"]):
            (made if cur["owner"] else by_others).append(cur)
    return made, by_others


def _switches(turns: list[dict]) -> int:
    kept = [t for t in turns if not _is_backchannel(t["text"])]
    sides = [t["owner"] for t in kept]
    return sum(1 for a, b in zip(sides, sides[1:], strict=False) if a != b)


def compute_metrics(
    segments: list[dict],
    words: list[dict] | None,
    owner_labels: set[str],
    duration: float,
    timestamps_estimated: bool,
) -> dict:
    """Compute per-speaker and owner speaking metrics, availability flags and notable moments.

    timestamps_estimated means segment times are guesses, so pace, interruptions and patience
    (which need real timing) are None and marked "unavailable". Without words, speaking time
    comes from segment durations and is marked "estimated", and filler metrics are unavailable.
    """
    per = _per_speaker(segments, words)
    own = [v for k, v in per.items() if k in owner_labels]
    own_talk = sum(v["talk_s"] for v in own)
    own_words = sum(v["words"] for v in own)
    all_talk = sum(v["talk_s"] for v in per.values())
    turns = owner_turns(segments, owner_labels)
    own_turns = [t for t in turns if t["owner"]]
    base = "exact" if words else "estimated"
    unavailable = "unavailable"

    o: dict = {}
    av: dict = {}
    moments: list[dict] = []

    if not own_turns:
        keys = [
            "pace_wpm",
            "talk_share",
            "longest_monologue_s",
            "interruptions_made",
            "interruption_rate",
            "interrupted_by_others",
            "patience_s",
            "questions",
            "questions_per_hour",
            "fillers",
            "fillers_per_min",
            "interactivity",
        ]
        return {
            "per_speaker": {
                k: {
                    "wpm": None if timestamps_estimated else v["wpm"],
                    "talk_s": v["talk_s"],
                    "talk_share": v["talk_share"],
                    "turns": v["turns"],
                }
                for k, v in per.items()
            },
            "owner": dict.fromkeys(keys),
            "availability": dict.fromkeys(keys, "unavailable"),
            "moments": [],
        }

    def put(key, value, availability):
        o[key], av[key] = value, (availability if value is not None else unavailable)

    put(
        "talk_share",
        round(own_talk / all_talk, 4) if all_talk > 0 else None,
        "estimated" if timestamps_estimated else base,
    )
    if timestamps_estimated or own_talk <= 0:
        put("pace_wpm", None, unavailable)
    else:
        put("pace_wpm", round(own_words / (own_talk / 60), 2), base)

    longest = max(own_turns, key=lambda t: t["end"] - t["start"], default=None)
    put(
        "longest_monologue_s",
        round(longest["end"] - longest["start"], 3) if longest else None,
        "estimated" if timestamps_estimated else "exact",
    )
    if longest:
        moments.append(
            {
                "t": longest["start"],
                "kind": "monologue",
                "speaker": longest["speaker"],
                "quote": _quote(longest["text"]),
                "likely": False,
            }
        )

    if timestamps_estimated:
        made, by_others = [], []
        for key in ("interruptions_made", "interruption_rate", "interrupted_by_others", "patience_s"):
            put(key, None, unavailable)
    else:
        made, by_others = _interruptions(turns)
        put("interruptions_made", len(made), "exact")
        put("interruption_rate", round(len(made) / len(own_turns), 4) if own_turns else None, "exact")
        put("interrupted_by_others", len(by_others), "exact")
        gaps = [
            max(0.0, cur["start"] - prev["end"])
            for prev, cur in zip(turns, turns[1:], strict=False)
            if cur["owner"] and not prev["owner"] and not _is_backchannel(cur["text"])
        ]
        put("patience_s", round(statistics.median(gaps), 3) if gaps else None, "exact")
    for t in made + by_others:
        moments.append(
            {
                "t": t["start"],
                "kind": "interruption",
                "speaker": t["speaker"],
                "quote": _quote(t["text"]),
                "likely": True,
            }
        )

    questions = sum(t["text"].count("?") for t in own_turns)
    o["questions"], av["questions"] = questions, "exact"
    put("questions_per_hour", round(questions / (duration / 3600), 2) if duration > 0 else None, "exact")

    owner_word_list = [w for w in (words or []) if w.get("speaker") in owner_labels]
    if words:
        times = _filler_hits(owner_word_list)
        o["fillers"], av["fillers"] = len(times), "exact"
        put("fillers_per_min", round(len(times) / (own_talk / 60), 2) if own_talk > 0 else None, "exact")
        for t in _bursts(times):
            moments.append(
                {"t": t, "kind": "filler_burst", "speaker": owner_word_list[0]["speaker"], "quote": "", "likely": False}
            )
    else:
        o["fillers"], av["fillers"] = None, unavailable
        put("fillers_per_min", None, unavailable)

    put(
        "interactivity",
        round(_switches(turns) * SWITCH_WINDOW_S / duration, 2) if duration > 0 else None,
        "estimated" if timestamps_estimated else base,
    )

    fast_limit = T.TARGETS["pace_wpm"]["high"] * FAST_FACTOR
    if not timestamps_estimated:
        for t in own_turns:
            span = t["end"] - t["start"]
            if span >= FAST_MIN_TURN_S and len(t["text"].split()) / (span / 60) > fast_limit:
                moments.append(
                    {
                        "t": t["start"],
                        "kind": "fast",
                        "speaker": t["speaker"],
                        "quote": _quote(t["text"]),
                        "likely": False,
                    }
                )

    moments.sort(key=lambda m: m["t"])
    return {
        "per_speaker": {
            k: {
                "wpm": None if timestamps_estimated else v["wpm"],
                "talk_s": v["talk_s"],
                "talk_share": v["talk_share"],
                "turns": v["turns"],
            }
            for k, v in per.items()
        },
        "owner": o,
        "availability": av,
        "moments": moments,
    }
