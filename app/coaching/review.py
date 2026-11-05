"""AI coaching review of one conversation, with timestamp grounding.

One structured-output LLM call per recording. The model sees the transcript with the owner
marked, plus the measured metrics, and returns scores, a summary, actions and timestamped
reactions and moments. Each score cites the times it is based on (``evidence``). ``ground``
then drops anything citing a time that is not in the transcript, and any score left with no
real time, so every judgement the user sees points at a real moment.

Laughs measured after the owner's lines (app/coaching/engagement.py) are given as extra lines;
their exact times are the only other times a score or moment may cite.
"""

import json
import math

from app.providers.llm_json import call_llm_json
from app.providers.summarize import _fmt_ts

from .metrics import owner_turns

MAX_TURNS = 400
SCORE_KEYS = ("clarity", "warmth", "charisma", "listening", "reception")
RESPONSES = ["engaged", "agreed", "pushed_back", "deflected", "short", "follow_up"]
MAX_NEXT_TIME = 3
LAUGH_LINE_CHARS = 80


def _obj(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_SCORE = _obj(
    {
        "score": {"type": "integer"},
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "number"}},
    }
)

REVIEW_SCHEMA = _obj(
    {
        "scores": _obj(dict.fromkeys(SCORE_KEYS, _SCORE)),
        "summary": {"type": "string"},
        "next_time": {"type": "array", "items": {"type": "string"}},
        "reactions": {
            "type": "array",
            "items": _obj(
                {
                    "t": {"type": "number"},
                    "owner_point": {"type": "string"},
                    "response": {"type": "string", "enum": RESPONSES},
                    "evidence": {"type": "string"},
                }
            ),
        },
        "moments": {
            "type": "array",
            "items": _obj({"t": {"type": "number"}, "kind": {"type": "string"}, "note": {"type": "string"}}),
        },
        "interruptions_confirmed": {"type": "array", "items": {"type": "number"}},
    }
)


def _system_prompt(owner_name, british, truncated, laughs=False):
    if owner_name:
        who = f"The person you are coaching is {owner_name}; their lines are marked (you)."
    else:
        who = "The person you are coaching is the one whose lines are marked (you)."
    parts = [
        "You are a conversation coach reviewing one recorded conversation. " + who,
        "Be blunt, specific and kind but honest. Coach the (you) speaker only; never judge anyone else.",
        "Scores are integers from 0 to 100 with a one-line reason each: clarity, warmth, charisma, "
        "listening, and reception (how the other people responded to them). For each score, list in "
        "evidence the times t (in seconds) of the lines it is based on. Give 2 to 3 concrete "
        "next_time actions. In reactions, for the (you) speaker's key points, say what the other "
        "person did next. In moments, note things like a strong question, a point that landed, "
        "rambling or hedging.",
        "Every reaction, moment and score evidence time must be a time t in seconds taken from a line you were given "
        "(the [mm:ss] at the start of that line, converted to seconds). Never invent a timestamp or "
        "quote words that are not in the transcript.",
        "The recording is single-channel audio, so the interruptions listed in the metrics are only "
        "likely interruptions. In interruptions_confirmed, list the t of each listed candidate you "
        "agree is a real interruption by the (you) speaker; reject the rest by leaving them out. "
        "Never add a time that is not listed.",
        "The measured metrics are exact where marked and estimated otherwise; use them, do not recompute them.",
    ]
    if laughs:
        parts.append(
            "Laughs after the (you) speaker's lines are listed separately. They were measured from the audio "
            "on a single microphone and may miss soft chuckles, so treat them as a lower bound. Charisma may "
            "consider them; a laugh's time is a valid evidence or moment time."
        )
    if truncated:
        parts.append(f"The transcript was cut to the first {MAX_TURNS} turns around the (you) speaker's turns.")
    if british:
        parts.append("Use British English spelling and phrasing (colour, organisation, summarise).")
    return "\n\n".join(parts)


def _select_turns(turns):
    """First MAX_TURNS turns that are the owner's or next to the owner's."""
    keep = [
        t
        for i, t in enumerate(turns)
        if t["owner"] or (i > 0 and turns[i - 1]["owner"]) or (i + 1 < len(turns) and turns[i + 1]["owner"])
    ]
    return keep[:MAX_TURNS], len(keep) > MAX_TURNS


def _line(turn):
    who = turn.get("speaker") or "Unknown"
    mark = " (you)" if turn["owner"] else ""
    return f"[{_fmt_ts(turn['start'])}] {who}{mark}: {turn['text']}"


def _near(t, times, tol):
    return any(abs(t - s) <= tol for s in times)


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _list(v):
    return v if isinstance(v, list) else []


def ground(
    review: dict,
    segments: list[dict],
    tolerance_s: float = 2.0,
    interruption_candidates=None,
    allowed_times=None,
) -> dict:
    """Return a copy of ``review`` with ungrounded content removed and values tidied.

    Reactions and moments whose ``t`` is not within ``tolerance_s`` of a segment start are
    dropped. Each score keeps only the ``evidence`` times within tolerance of a segment start
    and is dropped when none are left; kept scores are clamped to 0-100. ``allowed_times``
    (the measured laugh times) are valid too, with the same tolerance, for score evidence and
    moments only: reactions are about the owner's lines, and interruptions about turns. ``next_time`` loses
    empty strings and keeps at most 3.
    When ``interruption_candidates`` (times) is given, ``interruptions_confirmed`` keeps only
    entries within tolerance of a candidate: the model can confirm candidates, not invent them.
    Malformed shapes (non-dict entries, non-list arrays, non-finite times) are skipped, never raised.
    """
    starts = [s["start"] for s in segments]
    cited = starts + [t for t in _list(allowed_times) if _number(t)]
    out = dict(review)

    scores = {}
    raw_scores = review.get("scores")
    for k, v in raw_scores.items() if isinstance(raw_scores, dict) else []:
        if not isinstance(v, dict):
            continue
        v = dict(v)
        v["evidence"] = [t for t in _list(v.get("evidence")) if _number(t) and _near(t, cited, tolerance_s)]
        if not v["evidence"]:
            continue
        try:
            v["score"] = max(0, min(100, int(round(float(v.get("score", 0))))))
        except (TypeError, ValueError, OverflowError):
            v["score"] = 0
        scores[k] = v
    out["scores"] = scores

    for key, times in (("reactions", starts), ("moments", cited)):
        out[key] = [
            x
            for x in _list(review.get(key))
            if isinstance(x, dict) and _number(x.get("t")) and _near(x["t"], times, tolerance_s)
        ]

    nxt = [s.strip() for s in _list(review.get("next_time")) if isinstance(s, str) and s.strip()]
    out["next_time"] = nxt[:MAX_NEXT_TIME]

    confirmed = [
        t for t in _list(review.get("interruptions_confirmed")) if _number(t) and _near(t, starts, tolerance_s)
    ]
    if interruption_candidates is not None:
        confirmed = [t for t in confirmed if _near(t, interruption_candidates, tolerance_s)]
    out["interruptions_confirmed"] = confirmed
    return out


def _laugh_line(laugh: dict) -> str:
    text = " ".join(str(laugh.get("line_text") or "").split())[:LAUGH_LINE_CHARS]
    return f'[{_fmt_ts(laugh["t"])}] laugh after your line: "{text}"'


def review_conversation(segments, owner_labels, metrics, config, owner_name: str = "", laughs=None) -> dict:
    """Ask the configured LLM for a coaching review and ground it.

    ``laughs`` are the laughs counted after the owner's lines (``{t, line_start, line_text}``);
    each becomes one prompt line, and their times are valid citations.
    Returns ``{"review": dict | None, "provider": str, "errors": [str]}``.
    """
    name = owner_name or (config.get("profile") or {}).get("name") or ""
    laughs = [x for x in _list(laughs) if isinstance(x, dict) and _number(x.get("t"))]
    turns, truncated = _select_turns(owner_turns(segments, set(owner_labels)))
    system = _system_prompt(name, config.get("english_variant") == "gb", truncated, laughs=bool(laughs))
    user = "Transcript (times are [mm:ss] from the start):\n" + "\n".join(_line(t) for t in turns)
    if laughs:
        user += "\n\nLaughs measured after your lines:\n" + "\n".join(_laugh_line(x) for x in laughs)
    user += "\n\nMeasured metrics and flagged moments (JSON):\n" + json.dumps(metrics, ensure_ascii=False, default=str)
    parsed, provider, errors = call_llm_json(system, user, REVIEW_SCHEMA, config, schema_name="coaching_review")
    if parsed is None:
        return {"review": None, "provider": provider, "errors": errors}
    # Only interruptions the owner made are put up for confirmation (interruption moments also
    # cover the owner being interrupted; "speaker" is the interrupting turn's label).
    owners = set(owner_labels)
    candidates = [
        m["t"] for m in metrics.get("moments", []) if m.get("kind") == "interruption" and m.get("speaker") in owners
    ]
    try:
        grounded = ground(parsed, segments, interruption_candidates=candidates, allowed_times=[x["t"] for x in laughs])
    except Exception as e:  # broken model output must never crash the caller
        return {"review": None, "provider": provider, "errors": [*errors, f"invalid review shape: {e}"]}
    return {"review": grounded, "provider": provider, "errors": errors}
