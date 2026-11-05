from datetime import UTC, datetime

import pytest

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
    assert out["focus"]["text"] == "Your share of the talking was 72% over the last two weeks; aim for 40–60%."


def test_focus_one_sided_band_and_units():
    out = compute_progress([("a", ago(1), sc(longest_monologue_s=300))], NOW)
    assert out["focus"]["text"] == (
        "Your longest monologue was 300 seconds over the last two weeks; aim for at most 150 seconds."
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


@pytest.mark.parametrize(
    ("metric", "value", "text"),
    [
        (
            "pace_wpm",
            190,
            "Your pace was 190 words per minute over the last two weeks; aim for 110–160 words per minute.",
        ),
        ("talk_share", 0.25, "Your share of the talking was 25% over the last two weeks; aim for 40–60%."),
        (
            "longest_monologue_s",
            240,
            "Your longest monologue was 240 seconds over the last two weeks; aim for at most 150 seconds.",
        ),
        (
            "interruption_rate",
            0.25,
            "You made about 2.5 likely interruptions in 10 turns over the last two weeks; aim for at most 1 in 10 turns.",
        ),
        (
            "patience_s",
            0.2,
            "Your usual pause before you spoke was 0.2 seconds over the last two weeks; aim for 0.5–1.5 seconds.",
        ),
        (
            "questions_per_hour",
            1,
            "You asked 1 question an hour over the last two weeks; aim for 10–18 questions an hour.",
        ),
        (
            "fillers_per_min",
            4.2,
            "You used 4.2 filler words a speaking minute over the last two weeks; aim for 0.6–3 a speaking minute.",
        ),
        (
            "interactivity",
            2,
            "Speakers switched 2 times every 5 minutes over the last two weeks; "
            "aim for at least 5 switches every 5 minutes.",
        ),
    ],
)
def test_focus_sentence_for_every_metric(metric, value, text):
    out = compute_progress([("a", ago(1), sc(**{metric: value}))], NOW)
    assert out["focus"] == {"metric": metric, "text": text}


def test_every_banded_metric_has_a_focus_sentence():
    from app.coaching import progress, targets

    assert set(progress._FOCUS) == set(targets.TARGETS)


def test_still_stuck_never_names_an_improving_metric():
    data = [
        ("p1", ago(40), sc(talk_share=0.95)),
        ("r1", ago(5), sc(talk_share=0.7)),  # still outside the band, but improving
    ]
    out = compute_progress(data, NOW)
    assert out["most_improved"]["metric"] == "talk_share"
    assert out["most_stuck"] is None


def test_still_stuck_takes_a_metric_that_did_not_move_or_got_worse():
    data = [
        ("p1", ago(40), sc(talk_share=0.95, pace_wpm=170, longest_monologue_s=200)),
        ("r1", ago(5), sc(talk_share=0.7, pace_wpm=200, longest_monologue_s=200)),
    ]
    out = compute_progress(data, NOW)
    assert out["most_improved"]["metric"] == "talk_share"
    assert out["most_stuck"]["metric"] == "pace_wpm"  # got worse; the monologue stayed put
    assert out["most_stuck"]["metric"] != out["most_improved"]["metric"]


def test_overall_score_averages_only_the_scores_present():
    review = {"scores": {"clarity": {"score": 60, "reason": "x", "evidence": [1.0]}, "warmth": {"score": 80}}}
    out = compute_progress([("a", ago(1), sc(review=review))], NOW)
    assert out["series"]["overall_score"][0]["value"] == 70
    out = compute_progress([("a", ago(1), sc(review={"scores": {}}))], NOW)
    assert out["series"]["overall_score"] == []


def test_excluded_recordings_are_ignored():
    excluded = {**sc(talk_share=0.9), "excluded": True}
    out = compute_progress([("a", ago(1), sc(talk_share=0.5)), ("b", ago(2), excluded)], NOW)
    assert out["count"] == 1
    assert [p["session_id"] for p in out["series"]["talk_share"]] == ["a"]


def test_reports_without_an_ai_review_still_count():
    # "Include an AI review" off: metrics only, so no overall score for those recordings
    reviewed = {"scores": {"clarity": {"score": 80}}}
    data = [
        ("a", ago(1), sc(talk_share=0.5, pace_wpm=130)),
        ("b", ago(2), sc(talk_share=0.7, pace_wpm=170, review=reviewed)),
        ("c", ago(3), sc(talk_share=0.6)),
    ]
    out = compute_progress(data, NOW)
    assert out["count"] == 3
    assert [p["session_id"] for p in out["series"]["talk_share"]] == ["c", "b", "a"]
    assert [p["session_id"] for p in out["series"]["overall_score"]] == ["b"]
    assert out["baseline"]["overall_score"] == 80
    assert out["baseline"]["talk_share"] == 0.6


# ---------------------------------------------------------------- engagement


def test_engagement_score_is_its_own_series_and_overall_stays_ai_only():
    review = {"scores": {"clarity": {"score": 80}, "warmth": {"score": 60}}}
    a = {**sc(review=review, laughs_per_hour=3.0, follow_up_share=0.3, built_on_share=0.5), "engagement": {"score": 40}}
    b = {**sc(laughs_per_hour=None, follow_up_share=0.2), "engagement": {"score": None}}
    c = sc(talk_share=0.5)  # older report, no engagement block
    out = compute_progress([("a", ago(1), a), ("b", ago(2), b), ("c", ago(3), c)], NOW)
    assert [p["value"] for p in out["series"]["engagement_score"]] == [40.0]
    assert [p["value"] for p in out["series"]["overall_score"]] == [70.0]
    assert out["baseline"]["engagement_score"] == 40.0
    assert out["weekly"][-1]["engagement_score"] == 40.0
    assert [p["value"] for p in out["series"]["laughs_per_hour"]] == [3.0]
    assert [p["value"] for p in out["series"]["follow_up_share"]] == [0.2, 0.3]
    assert [p["value"] for p in out["series"]["built_on_share"]] == [0.5]


def test_engagement_score_is_never_the_focus():
    out = compute_progress([("a", ago(1), {**sc(talk_share=0.5), "engagement": {"score": 0}})], NOW)
    assert out["focus"] is None


@pytest.mark.parametrize(
    ("metric", "value", "text"),
    [
        (
            "follow_up_share",
            0.1,
            "Follow-up questions came after 10% of your turns over the last two weeks; aim for at least 25%.",
        ),
        (
            "built_on_share",
            0.3,
            "Others built on 30% of your points over the last two weeks; aim for at least 50%.",
        ),
    ],
)
def test_focus_sentence_for_engagement_parts(metric, value, text):
    out = compute_progress([("a", ago(1), sc(**{metric: value}))], NOW)
    assert out["focus"] == {"metric": metric, "text": text}


def test_laughs_per_hour_is_a_series_but_never_the_focus_or_still_stuck():
    """The detector undercounts and isn't calibrated on real audio, so laughs aren't coached on."""
    data = [
        ("p1", ago(40), sc(laughs_per_hour=0.0)),
        ("r1", ago(3), sc(laughs_per_hour=0.0)),
        ("r2", ago(2), sc(laughs_per_hour=0.5)),
    ]
    out = compute_progress(data, NOW)
    assert [p["value"] for p in out["series"]["laughs_per_hour"]] == [0.0, 0.0, 0.5]
    assert out["focus"] is None
    assert out["most_stuck"] is None
    # Another measure outside its band still gets them.
    data.append(("r3", ago(1), sc(laughs_per_hour=0.0, talk_share=0.9)))
    out = compute_progress(data, NOW)
    assert out["focus"]["metric"] == "talk_share"
    assert out["most_stuck"]["metric"] == "talk_share"
