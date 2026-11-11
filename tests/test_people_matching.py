"""Matching and calibration: lean towards leaving a speaker unnamed."""

import math

from app.people import matching


def _vec(cos_a, cos_b=0.0):
    """3D unit vector with the given cosines to axes 0 and 1."""
    rest = max(0.0, 1.0 - cos_a**2 - cos_b**2)
    return [cos_a, cos_b, math.sqrt(rest)]


A = [1.0, 0.0, 0.0]
B = [0.0, 1.0, 0.0]


def _person(vec, sessions=("s1",), name="P"):
    return {
        "name": name,
        "is_me": False,
        "samples": [{"vec": vec, "session_id": s, "label": "Speaker 1"} for s in sessions],
    }


def _people(**kw):
    return {"a": _person(A, name="A", **kw), "b": _person(B, name="B", **kw)}


def test_match_names_confident_speaker():
    out = matching.match({"S1": _vec(0.9)}, _people(), 0.7)
    assert out["S1"]["person"] == "a"
    assert abs(out["S1"]["score"] - 0.9) < 1e-9


def test_match_below_threshold_unnamed():
    assert matching.match({"S1": _vec(0.6)}, _people(), 0.7) == {}


def test_match_ambiguous_between_two_people_unnamed():
    people = {
        "a": _person([0.80, 0.6, 0.0]),
        "b": _person([0.77, 0.0, math.sqrt(1 - 0.77**2)]),
    }
    assert matching.match({"S1": [1.0, 0.0, 0.0]}, people, 0.7) == {}


def test_two_labels_can_match_same_person():
    out = matching.match({"S1": _vec(0.9), "S2": _vec(0.85)}, _people(), 0.7)
    assert out["S1"]["person"] == out["S2"]["person"] == "a"


def test_rejected_person_not_applied_and_no_fallthrough():
    people = {"a": _person(A), "b": _person(B)}
    # best is a (0.9), b is far behind; rejecting a must not fall through to b
    out = matching.match({"S1": _vec(0.9, 0.3)}, people, 0.2, rejected={"S1": ["a"]})
    assert out == {}


def test_exclude_session_skips_samples_and_empty_people():
    people = {"a": _person(A, ("s1",)), "b": _person(B, ("s2",))}
    out = matching.match({"S1": _vec(0.9)}, people, 0.7, exclude_session="s1")
    assert out == {}


def _instances_for(people_sessions, genuine, impostor_like_b=0, stranger=None):
    inst = []
    for i in range(genuine):
        inst.append((f"a{i}", "S1", _vec(0.75), "a"))
    for i in range(impostor_like_b):
        inst.append((f"a{i}", "S2", _vec(0.0, 0.6), "a"))
    return inst


def _library(n=12):
    return {
        "a": _person(A, tuple(f"a{i}" for i in range(n))),
        "b": _person(B, tuple(f"b{i}" for i in range(n))),
    }


def test_calibrate_few_instances_uses_default():
    out = matching.calibrate([("a0", "S1", _vec(0.9), "a")], _library())
    assert out == {"threshold": matching.DEFAULT_THRESHOLD, "accuracy": None, "instances": 1}


def test_calibrate_picks_lowest_safe_threshold():
    inst = _instances_for(None, 10, 10)
    out = matching.calibrate(inst, _library())
    assert out["threshold"] == 0.625
    assert out["instances"] == 20
    assert out["accuracy"] == 1.0


def test_calibrate_falls_back_when_nothing_safe():
    inst = [(f"a{i}", "S1", _vec(0.0, 0.95), "a") for i in range(20)]
    out = matching.calibrate(inst, _library())
    assert out["threshold"] == 0.80


def test_calibrate_single_recording_person_acts_as_stranger():
    people = _library()
    people["c"] = _person([0.0, 0.0, 1.0], ("c0",), name="C")
    inst = [(f"a{i}", "S1", _vec(0.9), "a") for i in range(20)]
    # C's only recording: excluded, so the voice looks like A at 0.7 and is named wrongly
    inst += [("c0", "S1", _vec(0.7), "c")] * 5
    out = matching.calibrate(inst, people)
    assert out["threshold"] > 0.7


def test_current_threshold_reads_config(monkeypatch):
    monkeypatch.setattr(matching, "load_config", lambda: {"people_match_threshold": 0.65})
    assert matching.current_threshold() == 0.65
    monkeypatch.setattr(matching, "load_config", lambda: {})
    assert matching.current_threshold() is None
