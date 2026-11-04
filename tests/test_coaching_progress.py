from datetime import UTC, datetime

from app.coaching.progress import compute_progress

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC).timestamp()  # a Wednesday
DAY = 86400.0


def sc(status="done", mixed_up=False, review=None, **owner):
    return {"status": status, "mixed_up": mixed_up, "metrics": {"owner": owner}, "review": review}


def ago(days):
    return NOW - days * DAY


def test_excludes_mixed_up_and_not_done():
    data = [
        ("a", ago(1), sc(talk_share=0.5)),
        ("b", ago(2), sc(talk_share=0.9, mixed_up=True)),
        ("c", ago(3), sc(talk_share=0.9, status="failed")),
    ]
    out = compute_progress(data, NOW)
    assert out["count"] == 1
    assert [p["session_id"] for p in out["series"]["talk_share"]] == ["a"]


def test_baseline_is_30_day_median():
    data = [
        ("a", ago(1), sc(pace_wpm=100)),
        ("b", ago(10), sc(pace_wpm=120)),
        ("c", ago(20), sc(pace_wpm=200)),
        ("old", ago(40), sc(pace_wpm=1000)),
        ("none", ago(2), sc(pace_wpm=None)),
    ]
    out = compute_progress(data, NOW)
    assert out["baseline"]["pace_wpm"] == 120
    assert out["baseline"]["talk_share"] is None
    assert len(out["series"]["pace_wpm"]) == 5 - 1


def test_most_improved_moves_toward_band():
    data = [
        ("p1", ago(40), sc(talk_share=0.9, pace_wpm=130)),
        ("p2", ago(35), sc(talk_share=0.9, pace_wpm=135)),
        ("r1", ago(5), sc(talk_share=0.65, pace_wpm=180)),  # talk share better, pace worse
        ("r2", ago(3), sc(talk_share=0.65, pace_wpm=180)),
    ]
    out = compute_progress(data, NOW)
    assert out["most_improved"] == {"metric": "talk_share", "from": 0.9, "to": 0.65}
    assert out["most_stuck"]["metric"] == "pace_wpm"
    assert out["most_stuck"]["target"] == {"low": 110.0, "high": 160.0}


def test_focus_is_furthest_outside_band():
    data = [
        ("a", ago(1), sc(talk_share=0.72, pace_wpm=170, patience_s=1.0)),
        ("b", ago(2), sc(talk_share=0.72, pace_wpm=170, patience_s=1.0)),
    ]
    out = compute_progress(data, NOW)
    assert out["focus"]["metric"] == "talk_share"
    assert out["focus"]["text"] == "Talk share was 72% over the last two weeks; aim for 40–60%."


def test_focus_one_sided_band_and_units():
    out = compute_progress([("a", ago(1), sc(longest_monologue_s=300))], NOW)
    assert out["focus"]["text"] == (
        "Longest monologue was 300 seconds over the last two weeks; aim for at most 150 seconds."
    )


def test_weekly_uses_monday_utc_weeks():
    sunday = datetime(2026, 11, 1, 23, 0, tzinfo=UTC).timestamp()
    monday = datetime(2026, 11, 2, 0, 30, tzinfo=UTC).timestamp()
    out = compute_progress([("s", sunday, sc(talk_share=0.4)), ("m", monday, sc(talk_share=0.6))], NOW)
    assert [w["week_start"] for w in out["weekly"]] == ["2026-10-26", "2026-11-02"]
    assert [w["talk_share"] for w in out["weekly"]] == [0.4, 0.6]


def test_overall_score_is_mean_of_review_and_excluded_from_pointers():
    review = {
        "scores": {
            k: {"score": s, "reason": "x"}
            for k, s in zip(["clarity", "warmth", "charisma", "listening", "reception"], [2, 4, 6, 8, 10], strict=True)
        }
    }
    out = compute_progress([("a", ago(1), sc(review=review))], NOW)
    assert out["series"]["overall_score"][0]["value"] == 6
    assert out["focus"] is None and out["most_stuck"] is None and out["most_improved"] is None


def test_empty_is_safe():
    out = compute_progress([], NOW)
    assert out["count"] == 0
    assert out["weekly"] == []
    assert out["most_improved"] is None and out["most_stuck"] is None and out["focus"] is None
    assert all(v == [] for v in out["series"].values())
    assert all(v is None for v in out["baseline"].values())
