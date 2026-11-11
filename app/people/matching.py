"""Decide which known person each speaker label is, and calibrate the threshold.

Pure: no file or config writes. A wrong name is worse than none, so every rule
leans towards leaving a speaker unnamed.
"""

from app.coaching.voice import _cosine
from app.people.store import ME
from app.utils.config import load_config

SAMPLE_CAP = 20
MIN_LEARN_SECONDS = 15.0
AMBIGUITY_MARGIN = 0.05
DEFAULT_THRESHOLD = 0.70
CALIBRATION_MIN_INSTANCES = 20
MAX_WRONG_RATE = 0.02
MIN_THRESHOLD = 0.55
MIN_DISTINCT_PEOPLE = 2
MIN_NAMED = 10
THRESHOLD_GRID = [t for t in (round(0.45 + i * 0.025, 3) for i in range(15)) if t >= MIN_THRESHOLD]
FALLBACK_THRESHOLD = 0.80


def current_threshold() -> float | None:
    return load_config().get("people_match_threshold")


def _scores(vec: list[float], people: dict, exclude_session: str | None) -> list[tuple[float, str]]:
    """(best cosine over the person's samples, person id), best first."""
    out = []
    for pid, person in people.items():
        sims = [
            _cosine(vec, s["vec"])
            for s in person.get("samples", [])
            if exclude_session is None or s.get("session_id") != exclude_session
        ]
        if sims:
            out.append((max(sims), pid))
    out.sort(reverse=True)
    return out


def match(
    embeddings: dict[str, list[float]],
    people: dict,
    threshold: float,
    rejected: dict[str, list[str]] | None = None,
    exclude_session: str | None = None,
) -> dict[str, dict]:
    result = {}
    for label, vec in embeddings.items():
        ranked = _scores(vec, people, exclude_session)
        if not ranked:
            continue
        best, pid = ranked[0]
        if best < threshold:
            continue
        if len(ranked) > 1 and best - ranked[1][0] < AMBIGUITY_MARGIN:
            continue
        if pid in (rejected or {}).get(label, []):
            continue
        result[label] = {"person": pid, "score": best}
    return result


def threshold_table(
    instances: list[tuple[str, str, list[float], str]], people: dict, thresholds: list[float] | None = None
) -> list[dict]:
    """Leave-one-recording-out tally per threshold: ``{"threshold", "named", "right",
    "others_named", "others_right"}`` rows (``others_``: instances that aren't you).

    Samples from no recording (your migrated v1 ones) can't be held out, so they're left out.
    """
    people = {
        pid: {"samples": [s for s in p.get("samples", []) if s.get("session_id") is not None]}
        for pid, p in people.items()
    }
    # Rank once per instance; thresholds only change which are named.
    ranked = [(_scores(vec, people, sid), true) for sid, _label, vec, true in instances]
    rows = []
    for t in thresholds if thresholds is not None else THRESHOLD_GRID:
        row = {"threshold": t, "named": 0, "right": 0, "others_named": 0, "others_right": 0}
        for scores, true in ranked:
            if not scores or scores[0][0] < t:
                continue
            if len(scores) > 1 and scores[0][0] - scores[1][0] < AMBIGUITY_MARGIN:
                continue
            ok = scores[0][1] == true
            row["named"] += 1
            row["right"] += ok
            if true != ME:
                row["others_named"] += 1
                row["others_right"] += ok
        rows.append(row)
    return rows


def _safe(named: int, right: int) -> bool:
    return (named - right) / named < MAX_WRONG_RATE


def calibrate(instances: list[tuple[str, str, list[float], str]], people: dict) -> dict:
    """Leave-one-recording-out: pick the lowest threshold with a wrong-name rate under the cap,
    overall and (once at least ``MIN_NAMED`` of them are named) for the instances that aren't you.

    ``named`` is how many instances that threshold names (0 when too few to test).
    """
    n = len(instances)
    if n < CALIBRATION_MIN_INSTANCES or len({inst[3] for inst in instances}) < MIN_DISTINCT_PEOPLE:
        return {"threshold": DEFAULT_THRESHOLD, "accuracy": None, "instances": n, "named": 0}
    table = threshold_table(instances, people, [*THRESHOLD_GRID, FALLBACK_THRESHOLD])
    for row in table[:-1]:
        named, right, others = row["named"], row["right"], row["others_named"]
        if named >= MIN_NAMED and _safe(named, right) and (others < MIN_NAMED or _safe(others, row["others_right"])):
            return {"threshold": row["threshold"], "accuracy": right / named, "instances": n, "named": named}
    named, right = table[-1]["named"], table[-1]["right"]
    accuracy = right / named if named else None
    return {"threshold": FALLBACK_THRESHOLD, "accuracy": accuracy, "instances": n, "named": named}
