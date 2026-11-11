"""Decide which known person each speaker label is, and calibrate the threshold.

Pure: no file or config writes. A wrong name is worse than none, so every rule
leans towards leaving a speaker unnamed.
"""

from app.coaching.voice import _cosine
from app.utils.config import load_config

SAMPLE_CAP = 20
MIN_LEARN_SECONDS = 15.0
AMBIGUITY_MARGIN = 0.05
DEFAULT_THRESHOLD = 0.70
CALIBRATION_MIN_INSTANCES = 20
MAX_WRONG_RATE = 0.02
THRESHOLD_GRID = [round(0.45 + i * 0.025, 3) for i in range(15)]
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


def calibrate(instances: list[tuple[str, str, list[float], str]], people: dict) -> dict:
    """Leave-one-recording-out: pick the lowest threshold with a wrong-name rate under the cap."""
    n = len(instances)
    if n < CALIBRATION_MIN_INSTANCES:
        return {"threshold": DEFAULT_THRESHOLD, "accuracy": None, "instances": n}
    # Rank once per instance; thresholds only change which are named.
    ranked = [(_scores(vec, people, sid), true) for sid, _label, vec, true in instances]

    def tally(t: float) -> tuple[int, int]:
        named = right = 0
        for scores, true in ranked:
            if not scores or scores[0][0] < t:
                continue
            if len(scores) > 1 and scores[0][0] - scores[1][0] < AMBIGUITY_MARGIN:
                continue
            named += 1
            right += scores[0][1] == true
        return named, right

    for t in THRESHOLD_GRID:
        named, right = tally(t)
        if named and (named - right) / named < MAX_WRONG_RATE:
            return {"threshold": t, "accuracy": right / named, "instances": n}
    named, right = tally(FALLBACK_THRESHOLD)
    return {"threshold": FALLBACK_THRESHOLD, "accuracy": right / named if named else None, "instances": n}
