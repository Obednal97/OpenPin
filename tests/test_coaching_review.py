"""Tests for app.coaching.review (AI review and timestamp grounding)."""

import json
from unittest.mock import patch

from app.coaching import review as R

SEGS = [
    {"start": 0.0, "end": 5.0, "text": "Hello there.", "speaker": "Speaker 1"},
    {"start": 5.0, "end": 12.0, "text": "Hi, how are you?", "speaker": "Speaker 2"},
    {"start": 60.0, "end": 70.0, "text": "I wanted to talk about pricing.", "speaker": "Speaker 1"},
]
METRICS = {
    "owner": {"wpm": 187},
    "availability": {"wpm": "exact"},
    "moments": [
        {"t": 5.0, "kind": "interruption", "speaker": "Speaker 2", "quote": "Hi", "likely": True},
        {"t": 60.0, "kind": "interruption", "speaker": "Speaker 1", "quote": "I wanted", "likely": True},
    ],
}


def scores(**over):
    base = {
        k: {"score": 50, "reason": "ok", "evidence": [0.0]}
        for k in ("clarity", "warmth", "charisma", "listening", "reception")
    }
    base.update(over)
    return base


def review(**over):
    r = {
        "scores": scores(),
        "summary": "Fine.",
        "next_time": ["Pause more", "Ask more"],
        "reactions": [],
        "moments": [],
        "interruptions_confirmed": [],
    }
    r.update(over)
    return r


def test_ground_drops_invented_timestamps():
    r = review(
        reactions=[
            {"t": 60.5, "owner_point": "pricing", "response": "engaged", "evidence": "x"},
            {"t": 300.0, "owner_point": "invented", "response": "agreed", "evidence": "x"},
        ],
        moments=[
            {"t": 0.0, "kind": "opener", "note": "ok"},
            {"t": 33.0, "kind": "invented", "note": "no"},
        ],
    )
    out = R.ground(r, SEGS)
    assert [x["t"] for x in out["reactions"]] == [60.5]
    assert [x["t"] for x in out["moments"]] == [0.0]


def test_ground_clamps_scores():
    out = R.ground(
        review(
            scores=scores(
                clarity={"score": 140, "reason": "a", "evidence": [0.0]},
                warmth={"score": -3, "reason": "b", "evidence": [5.0]},
            )
        ),
        SEGS,
    )
    assert out["scores"]["clarity"]["score"] == 100
    assert out["scores"]["warmth"]["score"] == 0


def test_ground_limits_next_time_and_drops_empty():
    out = R.ground(review(next_time=["a", "", "  ", "b", "c", "d"]), SEGS)
    assert out["next_time"] == ["a", "b", "c"]


def test_ground_interruptions_only_confirm_candidates():
    r = review(interruptions_confirmed=[5.5, 60.0])
    out = R.ground(r, SEGS, interruption_candidates=[5.0])
    assert out["interruptions_confirmed"] == [5.5]
    assert R.ground(r, SEGS)["interruptions_confirmed"] == [5.5, 60.0]


def run(config, **kw):
    captured = {}

    def fake(system, user, schema, cfg, schema_name="result"):
        captured.update(system=system, user=user, schema=schema)
        return (
            review(
                reactions=[{"t": 99.0, "owner_point": "x", "response": "agreed", "evidence": "e"}],
                interruptions_confirmed=[5.0, 60.0, 0.0],
            ),
            "anthropic",
            [],
        )

    with patch.object(R, "call_llm_json", fake):
        res = R.review_conversation(SEGS, {"Speaker 1"}, METRICS, config, **kw)
    return res, captured


def test_prompt_marks_owner_and_includes_metrics():
    res, cap = run({"english_variant": "gb"})
    assert "Speaker 1 (you): Hello there." in cap["user"]
    assert "Speaker 2: Hi, how are you?" in cap["user"] and "Speaker 2 (you)" not in cap["user"]
    assert "187" in cap["user"]
    assert "likely" in cap["system"] and "British English" in cap["system"]
    assert res["provider"] == "anthropic" and res["errors"] == []
    assert res["review"]["reactions"] == []  # t=99 is invented
    # 60.0 is the owner's interruption; 5.0 is Speaker 2 interrupting the owner, 0.0 no candidate.
    assert res["review"]["interruptions_confirmed"] == [60.0]


def test_failed_llm_returns_none_with_errors():
    with patch.object(R, "call_llm_json", lambda *a, **k: (None, "none", ["boom"])):
        res = R.review_conversation(SEGS, {"Speaker 1"}, METRICS, {})
    assert res == {"review": None, "provider": "none", "errors": ["boom"]}


def test_owner_name_from_profile_used():
    _, cap = run({"profile": {"name": "Ollie"}})
    assert "Ollie" in cap["system"] + cap["user"]
    _, cap = run({"profile": {"name": "Ollie"}}, owner_name="Sam")
    assert "Sam" in cap["system"] + cap["user"] and "Ollie" not in cap["system"] + cap["user"]


def test_schema_is_strict_compatible():
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)

    check(R.REVIEW_SCHEMA)
    json.dumps(R.REVIEW_SCHEMA)


def test_ground_malformed_shapes_do_not_raise():
    bad = {
        "scores": {
            "clarity": "high",
            "warmth": {"score": float("inf"), "evidence": [0.0]},
            "ok": {"score": 70, "reason": "r", "evidence": [60.0]},
            "no_evidence": {"score": 70, "reason": "r"},
            "bad_evidence": {"score": 70, "reason": "r", "evidence": "5.0"},
        },
        "next_time": "not a list",
        "reactions": ["str", None, {"t": float("inf")}, {"t": float("nan")}, {"t": "5"}, {"t": 5.0}],
        "moments": {"t": 5.0},
        "interruptions_confirmed": [float("inf"), "x", 5.0],
    }
    out = R.ground(bad, SEGS)
    assert set(out["scores"]) == {"warmth", "ok"}
    assert out["scores"]["warmth"]["score"] == 0 and out["scores"]["ok"]["score"] == 70
    assert out["next_time"] == [] and out["moments"] == []
    assert [x["t"] for x in out["reactions"]] == [5.0]
    assert out["interruptions_confirmed"] == [5.0]
    assert R.ground({"scores": None}, SEGS)["scores"] == {}
    R.ground({}, SEGS)


def test_review_conversation_returns_errors_if_grounding_raises():
    with (
        patch.object(R, "call_llm_json", lambda *a, **k: ({}, "ollama", [])),
        patch.object(R, "ground", side_effect=ValueError("boom")),
    ):
        res = R.review_conversation(SEGS, {"Speaker 1"}, METRICS, {})
    assert res["review"] is None and res["provider"] == "ollama"
    assert any("invalid review shape" in e for e in res["errors"])


def test_long_transcript_truncated_to_400_turns_with_note():
    segs = []
    for i in range(500):
        segs.append({"start": i * 2.0, "end": i * 2.0 + 1, "text": f"line{i}.", "speaker": f"Speaker {1 + i % 2}"})
    _, cap = (None, {})
    with patch.object(
        R, "call_llm_json", lambda s, u, sc, c, schema_name="r": (cap.update(system=s, user=u) or review(), "x", [])
    ):
        R.review_conversation(segs, {"Speaker 1"}, {"moments": []}, {})
    lines = [ln for ln in cap["user"].splitlines() if ln.startswith("[")]
    assert len(lines) == 400
    assert "cut to the first 400 turns" in cap["system"]


def test_citation_within_tolerance_of_segment_start_grounds():
    segs = [{"start": 61.7, "end": 65.0, "text": "x", "speaker": "Speaker 1"}]
    out = R.ground(review(moments=[{"t": 61, "kind": "k", "note": "n"}]), segs)
    assert len(out["moments"]) == 1


def test_score_evidence_is_grounded_and_ungrounded_scores_dropped():
    r = review(
        scores=scores(
            clarity={"score": 80, "reason": "a", "evidence": [0.5, 300.0, float("nan"), "5", True]},
            warmth={"score": 70, "reason": "b", "evidence": [300.0]},
            charisma={"score": 70, "reason": "c", "evidence": []},
        )
    )
    out = R.ground(r, SEGS)
    assert out["scores"]["clarity"] == {"score": 80, "reason": "a", "evidence": [0.5]}
    assert "warmth" not in out["scores"] and "charisma" not in out["scores"]
    assert set(out["scores"]) == {"clarity", "listening", "reception"}


def test_schema_scores_carry_evidence_and_prompt_asks_for_it():
    score = R.REVIEW_SCHEMA["properties"]["scores"]["properties"]["clarity"]
    assert score["properties"]["evidence"] == {"type": "array", "items": {"type": "number"}}
    assert set(score["required"]) == {"score", "reason", "evidence"}
    _, cap = run({})
    assert "evidence" in cap["system"]


def test_only_the_owners_interruptions_are_candidates():
    metrics = {
        "moments": [
            {"t": 5.0, "kind": "interruption", "speaker": "Speaker 2", "likely": True},
            {"t": 60.0, "kind": "interruption", "speaker": "Speaker 1", "likely": True},
            {"t": 0.0, "kind": "monologue", "speaker": "Speaker 1", "likely": False},
        ]
    }
    confirmed = review(interruptions_confirmed=[0.0, 5.0, 60.0])
    with patch.object(R, "call_llm_json", lambda *a, **k: (confirmed, "x", [])):
        res = R.review_conversation(SEGS, {"Speaker 1"}, metrics, {})
    assert res["review"]["interruptions_confirmed"] == [60.0]
    with patch.object(R, "call_llm_json", lambda *a, **k: (confirmed, "x", [])):
        res = R.review_conversation(SEGS, {"Speaker 2"}, metrics, {})
    assert res["review"]["interruptions_confirmed"] == [5.0]


# ---------------------------------------------------------------- laughs (engagement)

LAUGHS = [
    {"t": 12.0, "line_start": 0.0, "line_text": "Hello there."},
    {"t": 72.0, "line_start": 60.0, "line_text": "I wanted to talk about pricing. " + "x" * 100},
]


def test_laughs_are_lines_in_the_prompt_and_explained_in_the_system_prompt():
    _res, cap = run({}, laughs=LAUGHS)
    assert '[00:12] laugh after your line: "Hello there."' in cap["user"]
    quoted = ("I wanted to talk about pricing. " + "x" * 100)[:80]
    assert f'[01:12] laugh after your line: "{quoted}"' in cap["user"]
    assert "x" * 60 not in cap["user"]  # first 80 characters only
    assert "single" in cap["system"] and "chuckles" in cap["system"] and "charisma" in cap["system"]


def test_no_laughs_no_laugh_lines():
    _res, cap = run({})
    assert "laugh after your line" not in cap["user"]
    _res, cap = run({}, laughs=[])
    assert "laugh after your line" not in cap["user"]


def test_ground_accepts_allowed_times_as_evidence_but_not_made_up_ones():
    """Review focus 4: a laugh time survives grounding; any other invented time is still dropped."""
    r = review(
        scores=scores(
            charisma={"score": 80, "reason": "laughs", "evidence": [33.0]},
            warmth={"score": 70, "reason": "made up", "evidence": [40.0]},
        ),
        moments=[{"t": 33.0, "kind": "landed", "note": "laugh"}, {"t": 40.0, "kind": "invented", "note": "no"}],
    )
    out = R.ground(r, SEGS, allowed_times=[33.0])
    assert out["scores"]["charisma"]["evidence"] == [33.0]
    assert "warmth" not in out["scores"]
    assert [m["t"] for m in out["moments"]] == [33.0]
    # Without the allowed time, 33.0 is invented too.
    assert "charisma" not in R.ground(r, SEGS)["scores"]


def test_review_conversation_grounds_laugh_times_only():
    cited = review(
        scores=scores(
            charisma={"score": 80, "reason": "laughs", "evidence": [12.0]},
            warmth={"score": 70, "reason": "made up", "evidence": [40.0]},
        )
    )
    with patch.object(R, "call_llm_json", lambda *a, **k: (cited, "x", [])):
        out = R.review_conversation(SEGS, {"Speaker 1"}, METRICS, {}, laughs=LAUGHS)["review"]
    assert out["scores"]["charisma"]["evidence"] == [12.0]
    assert "warmth" not in out["scores"]
    with patch.object(R, "call_llm_json", lambda *a, **k: (cited, "x", [])):
        out = R.review_conversation(SEGS, {"Speaker 1"}, METRICS, {})["review"]
    assert "charisma" not in out["scores"]


def test_laugh_times_never_confirm_interruptions():
    confirmed = review(interruptions_confirmed=[12.0])
    with patch.object(R, "call_llm_json", lambda *a, **k: (confirmed, "x", [])):
        out = R.review_conversation(SEGS, {"Speaker 1"}, METRICS, {}, laughs=LAUGHS)["review"]
    assert out["interruptions_confirmed"] == []
