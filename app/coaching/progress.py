"""Progress over weeks: pure aggregation of coaching sidecars (no I/O, no AI).

Input is a list of (session_id, recorded_at_epoch, sidecar). Only sidecars with
status "done" and mixed_up False count. Every key of targets.TARGETS is a series,
read from sidecar["metrics"]["owner"] (the units there already match the target
bands, e.g. talk_share is a 0-1 fraction). One extra series, "overall_score", is the
mean of the review scores present (ungrounded ones are dropped); it has no band and is left out of most_improved,
most_stuck and focus. None values are skipped, never counted as zero.

Weeks are ISO weeks starting Monday, in UTC.
"""

import math
import statistics
from datetime import UTC, datetime, timedelta

from app.coaching import targets as T

OVERALL = "overall_score"
REVIEW_KEYS = ("clarity", "warmth", "charisma", "listening", "reception")
DAY_S = 86400.0
BASELINE_DAYS = 30
RECENT_DAYS = 28  # "last 4 weeks"; compared with the 28 days before
FOCUS_DAYS = 14  # "last 2 weeks"

# How each metric reads in the focus sentence:
# (multiplier, decimals, opening words, unit after the value, unit after the target band).
# In the value unit, "{s}" becomes "s" unless the value reads exactly "1".
_FOCUS = {
    "pace_wpm": (1, 0, "Your pace was", " word{s} per minute", " words per minute"),
    "talk_share": (100, 0, "Your share of the talking was", "%", "%"),
    "longest_monologue_s": (1, 0, "Your longest monologue was", " second{s}", " seconds"),
    "interruption_rate": (10, 1, "You made about", " likely interruption{s} in 10 turns", " in 10 turns"),
    "patience_s": (1, 1, "Your usual pause before you spoke was", " second{s}", " seconds"),
    "questions_per_hour": (1, 0, "You asked", " question{s} an hour", " questions an hour"),
    "fillers_per_min": (1, 1, "You used", " filler word{s} a speaking minute", " a speaking minute"),
    "interactivity": (1, 1, "Speakers switched", " time{s} every 5 minutes", " switches every 5 minutes"),
}


def _num(x) -> float | None:
    if isinstance(x, bool) or not isinstance(x, int | float) or math.isnan(x):
        return None
    return float(x)


def _overall(review) -> float | None:
    scores = (review or {}).get("scores") or {}
    vals = [_num((scores.get(k) or {}).get("score")) for k in REVIEW_KEYS]
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), 2) if vals else None


def _values(sidecar: dict) -> dict[str, float | None]:
    owner = (sidecar.get("metrics") or {}).get("owner") or {}
    out = {m: _num(owner.get(m)) for m in T.TARGETS}
    out[OVERALL] = _overall(sidecar.get("review"))
    return out


def _distance(metric: str, value: float) -> float:
    """How far value sits outside the metric's band, as a fraction of the band's size (0 inside)."""
    t = T.TARGETS[metric]
    low, high = t["low"], t["high"]
    if low is not None and value < low:
        gap, edge = low - value, low
    elif high is not None and value > high:
        gap, edge = value - high, high
    else:
        return 0.0
    scale = (high - low) if low is not None and high is not None else abs(edge)
    return gap / scale if scale else gap


def _median(rows: list[dict], metric: str, start: float, end: float) -> float | None:
    vals = [r["values"][metric] for r in rows if start <= r["t"] < end and r["values"][metric] is not None]
    return statistics.median(vals) if vals else None


def _num_text(metric: str, value: float) -> str:
    mult, dec = _FOCUS[metric][:2]
    return f"{round(value * mult, dec):g}"


def _band_text(metric: str) -> str:
    t, unit = T.TARGETS[metric], _FOCUS[metric][4]
    low, high = t["low"], t["high"]
    if low is not None and high is not None:
        return f"{_num_text(metric, low)}–{_num_text(metric, high)}{unit}"
    if low is not None:
        return f"at least {_num_text(metric, low)}{unit}"
    return f"at most {_num_text(metric, high)}{unit}"


def _focus_text(metric: str, value: float) -> str:
    opening, unit = _FOCUS[metric][2:4]
    number = _num_text(metric, value)
    unit = unit.replace("{s}", "" if number == "1" else "s")
    return f"{opening} {number}{unit} over the last two weeks; aim for {_band_text(metric)}."


def _week_start(t: float) -> str:
    d = datetime.fromtimestamp(t, UTC).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def compute_progress(sidecars: list[tuple[str, float, dict]], now: float, weeks: int = 12) -> dict:
    """Aggregate sidecars into series, baseline, weekly medians and coaching pointers.

    Only sessions recorded in the last `weeks` weeks (up to `now`) count.
    - baseline: median of each metric over the last 30 days.
    - weekly: median per ISO week (Monday, UTC), oldest first, weeks with sessions only.
    - most_improved: among banded metrics, the one that moved furthest toward its band,
      comparing the median of the last 4 weeks with the 4 weeks before. Needs both medians.
      Distances are scaled by band size so metrics are comparable. None if nothing improved.
    - most_stuck: among banded metrics whose last-4-weeks median is outside its band and that
      did not move toward it (gain <= 0), the one that moved least (a move away counts as less
      than none). Without an earlier 4-week median the movement is taken as zero. Ties go to
      the one furthest outside. An improving metric is never "still stuck", so it is never the
      same metric as most_improved.
    - focus: the banded metric furthest outside its band over the last 2 weeks (scaled by band
      size), with a templated sentence.
    """
    start = now - weeks * 7 * DAY_S
    rows = [
        {"session_id": sid, "t": t, "values": _values(sc)}
        for sid, t, sc in sidecars
        if sc.get("status") == "done" and sc.get("mixed_up") is False and start <= t <= now
    ]
    rows.sort(key=lambda r: r["t"])
    metrics = [*T.TARGETS, OVERALL]
    end = now + 1  # make `now` itself inclusive in the half-open windows

    series = {
        m: [
            {"session_id": r["session_id"], "t": r["t"], "value": r["values"][m]}
            for r in rows
            if r["values"][m] is not None
        ]
        for m in metrics
    }
    baseline = {m: _median(rows, m, now - BASELINE_DAYS * DAY_S, end) for m in metrics}

    by_week: dict[str, list[dict]] = {}
    for r in rows:
        by_week.setdefault(_week_start(r["t"]), []).append(r)
    weekly = [
        {"week_start": w, **{m: _median(rs, m, float("-inf"), float("inf")) for m in metrics}}
        for w, rs in sorted(by_week.items())
    ]

    recent_from, prior_from = now - RECENT_DAYS * DAY_S, now - 2 * RECENT_DAYS * DAY_S
    improved, stuck, focus = None, None, None
    best_gain, worst = 0.0, None
    far = 0.0
    for m in T.TARGETS:
        last = _median(rows, m, recent_from, end)
        before = _median(rows, m, prior_from, recent_from)
        if last is not None and before is not None:
            gain = _distance(m, before) - _distance(m, last)
            if gain > best_gain:
                best_gain, improved = gain, {"metric": m, "from": before, "to": last}
        if last is not None and _distance(m, last) > 0:
            gain = 0.0 if before is None else _distance(m, before) - _distance(m, last)
            key = (gain, -_distance(m, last))
            if gain <= 0 and (worst is None or key < worst):
                worst = key
                t = T.TARGETS[m]
                stuck = {"metric": m, "value": last, "target": {"low": t["low"], "high": t["high"]}}
        recent2 = _median(rows, m, now - FOCUS_DAYS * DAY_S, end)
        if recent2 is not None and _distance(m, recent2) > far:
            far = _distance(m, recent2)
            focus = {"metric": m, "text": _focus_text(m, recent2)}

    return {
        "series": series,
        "baseline": baseline,
        "weekly": weekly,
        "most_improved": improved,
        "most_stuck": stuck,
        "focus": focus,
        "count": len(rows),
    }
