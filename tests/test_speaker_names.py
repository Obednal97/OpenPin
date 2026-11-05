"""Speaker names: the {label: name} layer on a transcript, its routes, summaries and Notion."""

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from app.coaching import runner, store
from app.utils import speaker_names as sn
from app.utils.config import update_config

SID = "1715000000"
SID2 = "1715000100"
SEGMENTS = [
    {"start": 0.0, "end": 20.0, "text": "Hello there.", "speaker": "Speaker 1"},
    {"start": 21.0, "end": 60.0, "text": "Hi, how are you?", "speaker": "Speaker 2"},
    {"start": 61.0, "end": 70.0, "text": "Fine.", "speaker": "Speaker 10"},
]


@pytest.fixture
def env(tmp_path, flask_client):
    trn = tmp_path / "transcripts"
    trn.mkdir()
    rec = tmp_path / "recordings"
    rec.mkdir()
    with ExitStack() as stack:
        for mod in (
            "app.routes.recordings",
            "app.routes.transcribe",
            "app.routes.notion",
            "app.coaching.runner",
            "app.state.speakers",
        ):
            stack.enter_context(patch(f"{mod}.TRANSCRIPTS_DIR", trn))
        stack.enter_context(patch("app.coaching.store.COACHING_DIR", tmp_path / "coaching"))
        stack.enter_context(patch("app.routes.recordings.get_all_recordings_dirs", return_value=[rec]))
        # No page linked and nothing queued unless a test says so.
        stack.enter_context(patch("app.routes.notion._get_notion_page_url", return_value=""))
        enqueue = stack.enter_context(patch("app.utils.notion_queue.enqueue"))
        yield {"client": flask_client, "trn": trn, "rec": rec, "enqueue": enqueue}


def _write(trn, sid=SID, segments=SEGMENTS, **extra):
    data = {"text": " ".join(s["text"] for s in segments), "segments": segments, **extra}
    (trn / f"{sid}.json").write_text(json.dumps(data))
    return data


def _read(trn, sid=SID):
    return json.loads((trn / f"{sid}.json").read_text())


def _user_owner(labels, segments=SEGMENTS, **extra):
    return {
        "owner": {
            "labels": labels,
            "source": "user",
            "confidence": 1.0,
            "labelling": runner.labelling_fingerprint(segments),
        },
        **extra,
    }


def _set_profile(name):
    update_config(lambda c: c.setdefault("profile", {}).__setitem__("name", name))


# ---------------------------------------------------------------- pure functions


def test_apply_names_whole_word():
    assert sn.apply_names("Speaker 1 and Speaker 10 met", {"Speaker 1": "Sarah"}) == "Sarah and Speaker 10 met"
    assert sn.apply_names("Speaker 12, Speaker 1.", {"Speaker 1": "Sarah"}) == "Speaker 12, Sarah."
    assert sn.apply_names("**Speaker 1**: hi", {"Speaker 1": "Sarah"}) == "**Sarah**: hi"
    # One pass: a name that looks like another label isn't renamed again.
    swapped = sn.apply_names("Speaker 1 to Speaker 2", {"Speaker 1": "Speaker 2", "Speaker 2": "Bob"})
    assert swapped == "Speaker 2 to Bob"
    assert sn.apply_names("Speaker 1", {}) == "Speaker 1"
    assert sn.apply_names("", {"Speaker 1": "Sarah"}) == ""


def test_names_to_labels_round_trip():
    names = {"Speaker 1": "Sarah", "Speaker 2": "Sarah Jones", "Speaker 10": "Tom"}
    text = "Speaker 1 asked Speaker 2 and Speaker 10. Speaker 12 left."
    shown = sn.apply_names(text, names)
    assert shown == "Sarah asked Sarah Jones and Tom. Speaker 12 left."
    assert sn.names_to_labels(shown, names) == text
    # Whole words only.
    assert sn.names_to_labels("Tommy and Tom", names) == "Tommy and Speaker 10"
    # A name two speakers share can't be turned back: it stays as typed.
    assert sn.names_to_labels("Ann spoke", {"Speaker 1": "Ann", "Speaker 2": "Ann"}) == "Ann spoke"


def test_validate_name_rules():
    for bad in ["", "   ", "a" * 81, "a:b", "a*b", "a\x07b", "a\nb", None, 5]:
        with pytest.raises(ValueError):
            sn.validate_name(bad)
    assert sn.validate_name("  Sarah ") == "Sarah"
    assert sn.validate_name("a" * 80) == "a" * 80


# ---------------------------------------------------------------- storage


def test_set_names_unknown_label_rejected(env):
    from app.state import speakers

    _write(env["trn"])
    with pytest.raises(ValueError):
        speakers.set_names(SID, {"Speaker 9": "Sarah"})
    with pytest.raises(ValueError):
        speakers.set_names(SID, {"Speaker 1": "a:b"})
    with pytest.raises(KeyError):
        speakers.set_names(SID2, {"Speaker 1": "Sarah"})
    assert "speaker_names" not in _read(env["trn"])


def test_set_names_null_clears(env):
    from app.state import speakers
    from app.utils import changes

    _write(env["trn"])
    before = changes.current()
    assert speakers.set_names(SID, {"Speaker 1": " Sarah ", "Speaker 2": "Bob"}) == {
        "Speaker 1": "Sarah",
        "Speaker 2": "Bob",
    }
    assert changes.current() > before
    assert speakers.set_names(SID, {"Speaker 2": None}) == {"Speaker 1": "Sarah"}
    saved = _read(env["trn"])
    assert saved["speaker_names"] == {"Speaker 1": "Sarah"}
    assert saved["speaker_names_labelling"] == runner.labelling_fingerprint(SEGMENTS)
    assert speakers.get_names(SID) == {"Speaker 1": "Sarah"}


def test_relabel_drops_names(env):
    from app.routes.transcribe import _merge_transcript

    path = env["trn"] / f"{SID}.json"
    _write(env["trn"], speaker_names={"Speaker 1": "Sarah"})
    from app.state import speakers

    speakers.set_names(SID, {"Speaker 2": "Bob"})

    # Same labelling: kept.
    _merge_transcript(SID, path, {"text": "new words", "segments": SEGMENTS})
    kept = _read(env["trn"])
    assert kept["speaker_names"] == {"Speaker 1": "Sarah", "Speaker 2": "Bob"}
    assert not kept.get("speaker_names_cleared")

    # Different labelling: dropped, and flagged once for the UI.
    relabelled = [
        {**s, "speaker": {"Speaker 1": "Speaker 2", "Speaker 2": "Speaker 1"}[s["speaker"]]} for s in SEGMENTS[:2]
    ]
    _merge_transcript(SID, path, {"text": "new words", "segments": relabelled})
    dropped = _read(env["trn"])
    assert dropped.get("speaker_names", {}) == {}
    assert dropped["speaker_names_cleared"] is True

    r = env["client"].post("/transcripts/speaker-names/ack-cleared", json={"session_id": SID})
    assert r.status_code == 200
    assert "speaker_names_cleared" not in _read(env["trn"])


def test_background_diarize_relabel_drops_names(env):
    """Speaker labels added later by local diarization count as relabelling too."""
    from app.routes import transcribe

    text = "Hello there. Hi, how are you? Fine."
    plain = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in SEGMENTS]
    _write(env["trn"], segments=plain, speaker_names={"Speaker 1": "Sarah"})

    class Result:
        status = "done"
        message = ""
        engine = "diarize"
        segments = []

    def fake_merge(t_obj, _segments):
        for seg, src in zip(t_obj.segments, SEGMENTS[::-1], strict=False):
            seg.speaker = src["speaker"]
        return t_obj

    with (
        patch("src.transcribe.run_diarization", return_value=Result()),
        patch("src.transcribe.merge_transcript_with_diarization", side_effect=fake_merge),
        patch.object(transcribe, "fire_webhooks"),
        patch.object(transcribe, "_after_transcript_final"),
    ):
        transcribe._background_diarize(
            audio_path="x.ogg",
            session_id=SID,
            transcript_path=env["trn"] / f"{SID}.json",
            segments=plain,
            text=text,
            language="en",
            duration=70,
            provider="whisper",
        )
    saved = _read(env["trn"])
    assert saved.get("speaker_names", {}) == {}
    assert saved["speaker_names_cleared"] is True


def test_owner_default_only_after_user_pick(env):
    from app.state import speakers

    _set_profile("Ollie")
    t = _write(env["trn"])

    voice = {"owner": {**_user_owner(["Speaker 1"])["owner"], "source": "voice"}}
    assert sn.effective_names(t, voice, "Ollie") == {}
    assert sn.effective_names(t, _user_owner(["Speaker 1"]), "Ollie") == {"Speaker 1": "Ollie"}
    assert sn.effective_names(t, _user_owner(["Speaker 1"], mixed_up=True), "Ollie") == {}
    assert sn.effective_names(t, _user_owner(["Speaker 1"], excluded=True), "Ollie") == {}
    assert sn.effective_names(t, _user_owner(["Speaker 1"]), "") == {}
    assert sn.effective_names(t, _user_owner(["Speaker 1"]), None) == {}
    # A pick made for another labelling doesn't count.
    stale = _user_owner(["Speaker 1"])
    stale["owner"]["labelling"] = "something else"
    assert sn.effective_names(t, stale, "Ollie") == {}
    # A typed name wins.
    typed = {**t, "speaker_names": {"Speaker 1": "Oliver"}}
    assert sn.effective_names(typed, _user_owner(["Speaker 1"]), "Ollie") == {"Speaker 1": "Oliver"}

    # Through the store: the sidecar and the profile from config.
    store.update(SID, lambda d: d.update(_user_owner(["Speaker 1"])))
    assert speakers.get_names(SID) == {"Speaker 1": "Ollie"}
    items = {t["session_id"]: t for t in env["client"].get("/transcripts").get_json()}
    assert items[SID]["speaker_names"] == {"Speaker 1": "Ollie"}
    assert items[SID]["stored_speaker_names"] == {}
    assert items[SID]["speaker_names_cleared"] is False


# ---------------------------------------------------------------- summaries and Notion


def test_summary_saved_with_names_is_stored_as_labels(env):
    from app.state import speakers

    _write(env["trn"], summary="old")
    speakers.set_names(SID, {"Speaker 1": "Sarah", "Speaker 10": "Tom"})
    r = env["client"].post(
        "/transcripts/update",
        json={"session_id": SID, "summary": "Sarah agreed with Tom; Speaker 2 and Sarahs did not."},
    )
    assert r.status_code == 200
    assert _read(env["trn"])["summary"] == "Speaker 1 agreed with Speaker 10; Speaker 2 and Sarahs did not."


def test_notion_content_uses_names(env):
    from app.routes import notion as rn
    from app.state import speakers

    t = _write(env["trn"], summary="Speaker 1 thanked Speaker 10.")
    assert "**Speaker 1**:" in rn._build_notion_content(t)
    speakers.set_names(SID, {"Speaker 1": "Sarah", "Speaker 10": "Tom"})
    t = _read(env["trn"])
    content = rn._build_notion_content(t, speakers.get_names(SID))
    assert "Sarah thanked Tom." in content
    assert "**Sarah**: Hello there." in content
    assert "**Speaker 2**: Hi" in content
    assert "**Tom**: Fine." in content
    section = rn._build_app_section(t, SID)
    assert "**Sarah**: Hello there." in section and "Sarah thanked Tom." in section


def test_set_names_enqueues_full_notion_update_when_linked(env):
    from app.state import speakers
    from app.utils import notion_queue

    _write(env["trn"])
    speakers.set_names(SID, {"Speaker 1": "Sarah"})
    env["enqueue"].assert_not_called()

    with patch("app.routes.notion._get_notion_page_url", return_value="https://www.notion.so/p-abc"):
        speakers.set_names(SID, {"Speaker 2": "Bob"})
        env["enqueue"].assert_called_once_with(SID, notion_queue.KIND_FULL)
        env["enqueue"].reset_mock()
        speakers.set_names(SID, {"Speaker 2": "Bob"})  # no change: nothing to send
        env["enqueue"].assert_not_called()


def test_user_owner_pick_enqueues_notion_update(env, monkeypatch):
    from app.utils import notion_queue

    _set_profile("Ollie")
    _write(env["trn"])
    monkeypatch.setattr(runner, "_learn_and_run", lambda sid, *a, **k: (None, store.load(sid)))
    with patch("app.routes.notion._get_notion_page_url", return_value="https://www.notion.so/p-abc"):
        runner.set_owner(SID, ["Speaker 1"], learn=False)
        env["enqueue"].assert_called_once_with(SID, notion_queue.KIND_FULL)

        # The same pick again changes nothing.
        env["enqueue"].reset_mock()
        runner.set_owner(SID, ["Speaker 1"], learn=False)
        env["enqueue"].assert_not_called()

        # Mixed up removes the default name: an update.
        r = env["client"].post(f"/coaching/{SID}/mixed-up", json={"mixed_up": True})
        assert r.status_code == 200
        env["enqueue"].assert_called_once_with(SID, notion_queue.KIND_FULL)

        # Excluding (after un-mixing) removes it too.
        env["client"].post(f"/coaching/{SID}/mixed-up", json={"mixed_up": False})
        env["enqueue"].reset_mock()
        runner.set_excluded(SID, True)
        env["enqueue"].assert_called_once_with(SID, notion_queue.KIND_FULL)


def test_owner_pick_without_profile_name_queues_nothing(env, monkeypatch):
    _write(env["trn"])
    monkeypatch.setattr(runner, "_learn_and_run", lambda sid, *a, **k: (None, store.load(sid)))
    with patch("app.routes.notion._get_notion_page_url", return_value="https://www.notion.so/p-abc"):
        runner.set_owner(SID, ["Speaker 1"], learn=False)
    env["enqueue"].assert_not_called()


# ---------------------------------------------------------------- people, routes, recordings


def test_list_people_counts(env):
    from app.state import speakers

    _set_profile("Ollie")
    _write(env["trn"])
    _write(env["trn"], SID2)
    speakers.set_names(SID, {"Speaker 1": "Sarah", "Speaker 2": "Bob"})
    speakers.set_names(SID2, {"Speaker 1": "Sarah", "Speaker 10": "Sarah"})
    store.update(SID2, lambda d: d.update(_user_owner(["Speaker 2"])))  # the profile default isn't a person
    assert speakers.list_people() == [{"name": "Sarah", "recordings": 2}, {"name": "Bob", "recordings": 1}]
    assert env["client"].get("/people").get_json() == speakers.list_people()


def test_routes_404_and_400(env):
    c = env["client"]
    _write(env["trn"])
    assert (
        c.post("/transcripts/speaker-names", json={"session_id": SID2, "names": {"Speaker 1": "A"}}).status_code == 404
    )
    for body in [
        {"session_id": SID, "names": {"Speaker 9": "A"}},
        {"session_id": SID, "names": {"Speaker 1": "a*b"}},
        {"session_id": SID, "names": {"Speaker 1": 5}},
        {"session_id": SID, "names": ["Speaker 1"]},
        {"session_id": SID},
        {"session_id": "../etc", "names": {}},
        {},
    ]:
        r = c.post("/transcripts/speaker-names", json=body)
        assert r.status_code == 400, body
        assert r.get_json()["error"]
    r = c.post("/transcripts/speaker-names", json={"session_id": SID, "names": {"Speaker 1": "Sarah"}})
    assert r.status_code == 200
    assert r.get_json() == {"speaker_names": {"Speaker 1": "Sarah"}}
    assert c.post("/transcripts/speaker-names/ack-cleared", json={"session_id": SID2}).status_code == 404
    assert c.post("/transcripts/speaker-names/ack-cleared", json={"session_id": "../x"}).status_code == 400


def test_recordings_list_participants(env):
    from app.state import speakers

    _set_profile("Ollie")
    (env["rec"] / f"{SID}.ogg").write_bytes(b"x" * 3000)
    (env["rec"] / f"{SID2}.ogg").write_bytes(b"x" * 3000)
    _write(env["trn"])
    speakers.set_names(SID, {"Speaker 2": "Sarah", "Speaker 10": "Sarah"})
    store.update(SID, lambda d: d.update(_user_owner(["Speaker 1"])))
    items = {r["session_id"]: r for r in env["client"].get("/recordings").get_json()}
    assert items[SID]["participants"] == ["Ollie", "Sarah"]
    assert items[SID2]["participants"] == []
    # A rename shows straight away.
    speakers.set_names(SID, {"Speaker 2": "Sam"})
    items = {r["session_id"]: r for r in env["client"].get("/recordings").get_json()}
    assert items[SID]["participants"] == ["Ollie", "Sam", "Sarah"]
