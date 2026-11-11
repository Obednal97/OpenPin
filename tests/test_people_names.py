"""Effective speaker names from voice matches: order, validity and the transcript API."""

import json
from unittest.mock import patch

import pytest

from app.coaching import runner
from app.people import store
from app.state import speakers
from app.utils import speaker_names as sn

SID = "1715000000"
SEGMENTS = [
    {"start": 0.0, "end": 20.0, "text": "Hello there.", "speaker": "Speaker 1"},
    {"start": 21.0, "end": 60.0, "text": "Hi, how are you?", "speaker": "Speaker 2"},
]
PEOPLE = {"me": None, "p1": "Sam"}


def _t(matches=None, labelling=None, **extra):
    block = {
        "labelling": labelling or runner.labelling_fingerprint(SEGMENTS),
        "matches": matches if matches is not None else {"Speaker 1": {"person": "p1", "score": 0.82}},
        "rejected": {},
    }
    return {"segments": SEGMENTS, "speaker_voice_matches": block, **extra}


def _owner(labels, **extra):
    return {
        "owner": {
            "labels": labels,
            "source": "user",
            "confidence": 1.0,
            "labelling": runner.labelling_fingerprint(SEGMENTS),
        },
        **extra,
    }


def test_voice_match_names_a_label():
    assert sn.effective_names(_t(), {}, None, PEOPLE) == {"Speaker 1": "Sam"}
    assert sn.voice_matched(_t(), {}, None, PEOPLE) == {"Speaker 1": 0.82}


def test_typed_name_beats_voice_match():
    t = _t(speaker_names={"Speaker 1": "Alex"})
    assert sn.effective_names(t, {}, None, PEOPLE) == {"Speaker 1": "Alex"}
    assert sn.voice_matched(t, {}, None, PEOPLE) == {}


def test_coaching_pick_beats_voice_match():
    t = _t(matches={"Speaker 1": {"person": "p1", "score": 0.8}})
    assert sn.effective_names(t, _owner(["Speaker 1"]), "Ollie", PEOPLE) == {"Speaker 1": "Ollie"}
    assert sn.voice_matched(t, _owner(["Speaker 1"]), "Ollie", PEOPLE) == {}


def test_voice_match_names_owner_without_coaching_pick():
    t = _t(matches={"Speaker 2": {"person": "me", "score": 0.9}})
    assert sn.effective_names(t, {}, "Ollie", PEOPLE) == {"Speaker 2": "Ollie"}
    assert sn.voice_matched(t, {}, "Ollie", PEOPLE) == {"Speaker 2": 0.9}


def test_me_match_without_profile_name_shows_label():
    t = _t(matches={"Speaker 2": {"person": "me", "score": 0.9}})
    for profile in (None, "", "  "):
        assert sn.effective_names(t, {}, profile, {"me": profile}) == {}
    assert sn.voice_matched(t, {}, None, PEOPLE) == {}


def test_stale_labelling_match_ignored():
    assert sn.effective_names(_t(labelling="old"), {}, None, PEOPLE) == {}


def test_mixed_up_recording_ignores_voice_matches():
    assert sn.effective_names(_t(), {"mixed_up": True}, None, PEOPLE) == {}


def test_excluded_recording_still_gets_voice_names():
    assert sn.effective_names(_t(), {"excluded": True}, None, PEOPLE) == {"Speaker 1": "Sam"}


def test_deleted_or_unnamed_person_match_ignored():
    assert sn.effective_names(_t(), {}, None, {"me": None}) == {}
    assert sn.effective_names(_t(), {}, None, {"p1": None}) == {}
    assert sn.effective_names(_t(), {}, None, None) == {}


def test_frozen_list_and_odd_shapes_do_not_break():
    t = _t(speaker_names_frozen=["Speaker 1"])
    assert sn.effective_names(t, {}, None, PEOPLE) == {"Speaker 1": "Sam"}
    t["speaker_voice_matches"]["matches"] = {"Speaker 1": "junk", "Speaker 2": {"person": 3}}
    assert sn.effective_names(t, {}, None, PEOPLE) == {}
    t["speaker_voice_matches"] = "junk"
    assert sn.effective_names(t, {}, None, PEOPLE) == {}


@pytest.fixture
def voices(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "VOICES_PATH", tmp_path / "people" / "voices.json")
    monkeypatch.setattr("app.coaching.store.COACHING_DIR", tmp_path / "coaching")
    return store.VOICES_PATH


def test_people_names_never_takes_the_store_lock(voices):
    voices.parent.mkdir(parents=True)
    voices.write_text(
        json.dumps(
            {"version": 2, "people": {"me": {"name": None, "samples": []}, "p1": {"name": "Sam", "samples": []}}}
        )
    )
    with store._lock:  # another thread mid-mutation
        names = speakers.people_names("Ollie")
    assert names == {"me": "Ollie", "p1": "Sam"}
    assert speakers.people_names(None)["me"] is None


def test_people_names_without_a_store_file(voices):
    assert speakers.people_names("Ollie") == {"me": "Ollie"}


def test_names_for_and_api_use_the_store(voices, tmp_path):
    voices.parent.mkdir(parents=True)
    voices.write_text(json.dumps({"version": 2, "people": {"p1": {"name": "Sam", "samples": [{"vec": [1.0]}]}}}))
    t = _t()
    assert speakers.names_for(SID, t, profile=None) == {"Speaker 1": "Sam"}
    assert speakers.name_sources(SID, t, profile=None) == {"Speaker 1": {"source": "voice", "score": 0.82}}


def test_api_reports_voice_sources(voices, tmp_path, flask_client):
    voices.parent.mkdir(parents=True)
    voices.write_text(json.dumps({"version": 2, "people": {"p1": {"name": "Sam", "samples": [{"vec": [0.5]}]}}}))
    trn = tmp_path / "transcripts"
    trn.mkdir()
    data = {"text": "x", **_t(speaker_names={"Speaker 2": "Alex"})}
    (trn / f"{SID}.json").write_text(json.dumps(data))
    with (
        patch("app.routes.recordings.TRANSCRIPTS_DIR", trn),
        patch("app.state.speakers.TRANSCRIPTS_DIR", trn),
    ):
        body = flask_client.get("/transcripts").get_json()
    item = next(i for i in body if i["session_id"] == SID)
    assert item["speaker_names"] == {"Speaker 1": "Sam", "Speaker 2": "Alex"}
    assert item["speaker_name_sources"] == {"Speaker 1": {"source": "voice", "score": 0.82}}
    assert "vec" not in json.dumps(item)
