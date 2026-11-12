"""/mcp/v1 data endpoints (app/routes/mcp_api.py, app/mcp/library.py), through the real guard.

These are what the MCP connector (app/mcp/server.py) calls. Reads must show the
same data as the window; writes must go through the app's own functions.
"""

import json
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from app.coaching import runner
from app.utils import changes
from app.utils.config import load_config, save_config, update_config
from tests.conftest import TEST_API_TOKEN

MCP_HEADER = "X-Open-Pin-MCP-Key"
NOT_FOUND = "No recording with that id"

SID = "1715000000"  # 6 May 2024
SID2 = "1715100000"
SID3 = "1715200000"
SEGMENTS = [
    {"start": 0.0, "end": 20.0, "text": "Morning all, quick budget check.", "speaker": "Speaker 1"},
    {"start": 21.0, "end": 60.0, "text": "The forecast looks fine.", "speaker": "Speaker 2"},
    {"start": 61.0, "end": 70.0, "text": "Agreed.", "speaker": "Speaker 10"},
]


@pytest.fixture
def env(tmp_path, flask_client):
    trn = tmp_path / "transcripts"
    rec = tmp_path / "recordings"
    trn.mkdir()
    rec.mkdir()
    with ExitStack() as stack:
        for mod in (
            "app.routes.recordings",
            "app.routes.transcribe",
            "app.routes.notion",
            "app.coaching.runner",
            "app.state.speakers",
            "app.utils.tags",
        ):
            stack.enter_context(patch(f"{mod}.TRANSCRIPTS_DIR", trn))
        stack.enter_context(patch("app.coaching.store.COACHING_DIR", tmp_path / "coaching"))
        stack.enter_context(patch("app.routes.recordings.get_all_recordings_dirs", return_value=[rec]))
        stack.enter_context(patch("app.utils.constants.get_all_recordings_dirs", return_value=[rec]))
        stack.enter_context(patch("app.routes.notion._get_notion_page_url", return_value=""))
        stack.enter_context(patch("app.utils.notion_queue.enqueue"))
        from app.mcp import access

        access.enable(5123)
        client = flask_client
        client.environ_base.pop("HTTP_X_PLAUD_TOKEN", None)
        client.environ_base["HTTP_X_OPEN_PIN_MCP_KEY"] = access.current_key()
        yield type("Env", (), {"client": client, "trn": trn, "rec": rec, "tmp": tmp_path})
        access.disable()


def _audio(env, sid, size=4000):
    path = env.rec / f"{sid}.ogg"
    path.write_bytes(b"\0" * size)
    return path


def _transcript(env, sid, segments=SEGMENTS, **extra):
    data = {"text": " ".join(s["text"] for s in segments), "segments": segments, "duration": 70.0, **extra}
    (env.trn / f"{sid}.json").write_text(json.dumps(data))
    return data


def _recording(env, sid, transcript=True, **extra):
    _audio(env, sid)
    if transcript:
        _transcript(env, sid, **extra)


def _sidecar(env, sid, data):
    d = env.tmp / "coaching"
    d.mkdir(exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps(data))


def _user_owner(labels, segments=SEGMENTS):
    return {
        "labels": labels,
        "source": "user",
        "confidence": 1.0,
        "labelling": runner.labelling_fingerprint(segments),
    }


def _get(env, url, **params):
    r = env.client.get(url, query_string=params)
    return r.status_code, r.get_json()


def _post(env, url, body):
    r = env.client.post(url, json=body)
    return r.status_code, r.get_json()


LIST_KEYS = {
    "id",
    "title",
    "recorded_at",
    "duration_s",
    "tags",
    "participants",
    "has_transcript",
    "has_summary",
    "coaching_status",
    "notion_url",
}


# ---------------------------------------------------------------- the guard still applies


def test_requires_the_mcp_key(env):
    env.client.environ_base.pop("HTTP_X_OPEN_PIN_MCP_KEY")
    assert env.client.get("/mcp/v1/recordings").status_code == 401
    r = env.client.get("/mcp/v1/recordings", headers={"X-Plaud-Token": TEST_API_TOKEN})
    assert r.status_code == 401


def test_access_off_is_403(env):
    from app.mcp import access

    access.disable()
    status, body = _get(env, "/mcp/v1/tags")
    assert status == 403
    assert body == {"error": "AI tool access is turned off in Open Pin"}


# ---------------------------------------------------------------- list_recordings


def test_list_shape_and_order(env):
    _recording(env, SID, auto_title="Budget check", summary="Speaker 2 approved it.")
    _recording(env, SID2)
    _recording(env, SID3, transcript=False)
    save_config(
        {
            "recording_names": {SID2: "Named by me"},
            "recording_tags": {SID: ["Work"]},
            "notion_page_urls": {SID: "https://notion.so/page"},
        }
    )
    status, body = _get(env, "/mcp/v1/recordings")
    assert status == 200
    assert body["next_cursor"] is None
    items = body["items"]
    assert [i["id"] for i in items] == [SID3, SID2, SID]
    for item in items:
        assert set(item) == LIST_KEYS

    first = items[2]
    assert first["title"] == "Budget check"  # auto title when there's no custom name
    when = datetime.fromisoformat(first["recorded_at"])
    assert when.tzinfo is not None and when.timestamp() == int(SID)
    assert first["duration_s"] == 70.0
    assert first["tags"] == ["Work"]
    assert first["has_transcript"] is True and first["has_summary"] is True
    assert first["notion_url"] == "https://notion.so/page"
    assert first["coaching_status"] is None

    assert items[1]["title"] == "Named by me"
    assert items[1]["has_summary"] is False
    # No custom name, no auto title: the date.
    no_title = items[0]
    assert no_title["has_transcript"] is False
    expected = datetime.fromtimestamp(int(SID3))
    assert no_title["title"] == f"{expected.day} {expected:%B %Y} at {expected:%H:%M}"
    assert no_title["participants"] == []


def test_list_matches_the_window(env):
    """Every recording the window lists is listed for AI tools, and nothing else."""
    _recording(env, SID)
    _recording(env, SID2, transcript=False)
    (env.rec / f"{SID3}_uncropped.ogg").write_bytes(b"\0" * 100)  # a crop backup isn't a recording
    _transcript(env, "1715300000")  # a transcript without a recording isn't either
    env.client.environ_base["HTTP_X_PLAUD_TOKEN"] = TEST_API_TOKEN
    window = {r["session_id"] for r in env.client.get("/recordings").get_json()}
    status, body = _get(env, "/mcp/v1/recordings")
    assert {i["id"] for i in body["items"]} == window == {SID, SID2}


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("budget", {SID}),  # transcript text, case-insensitive
        ("FORECAST", {SID}),
        ("quarterly", {SID2}),  # title
        ("client-x", {SID2}),  # tag
        ("sarah", {SID}),  # speaker name
        ("decided to hire", {SID2}),  # summary
        ("nothing like this", set()),
    ],
)
def test_search_each_field(env, query, expected):
    _recording(env, SID, speaker_names={"Speaker 2": "Sarah Jones"})
    _recording(
        env,
        SID2,
        segments=[{"start": 0.0, "end": 5.0, "text": "Hello.", "speaker": "Speaker 1"}],
        summary="We decided to hire.",
    )
    save_config({"recording_names": {SID2: "Quarterly review"}, "recording_tags": {SID2: ["Client-X"]}})
    status, body = _get(env, "/mcp/v1/recordings", query=query)
    assert status == 200
    assert {i["id"] for i in body["items"]} == expected


def test_search_finds_your_name_once_confirmed(env):
    _recording(env, SID)
    update_config(lambda c: c.setdefault("profile", {}).__setitem__("name", "Ollie"))
    assert _get(env, "/mcp/v1/recordings", query="ollie")[1]["items"] == []
    _sidecar(env, SID, {"owner": _user_owner(["Speaker 1"])})
    items = _get(env, "/mcp/v1/recordings", query="ollie")[1]["items"]
    assert [i["id"] for i in items] == [SID]
    assert items[0]["participants"] == ["Ollie"]


def test_tag_filter_is_case_insensitive(env):
    _recording(env, SID)
    _recording(env, SID2)
    save_config({"recording_tags": {SID: ["Work"], SID2: ["Home"]}})
    assert [i["id"] for i in _get(env, "/mcp/v1/recordings", tag="work")[1]["items"]] == [SID]


def test_date_filters_are_inclusive(env):
    for sid in (SID, SID2, SID3):
        _recording(env, sid, transcript=False)
    day2 = datetime.fromtimestamp(int(SID2))

    def ids(**params):
        status, body = _get(env, "/mcp/v1/recordings", **params)
        assert status == 200, body
        return [i["id"] for i in body["items"]]

    # The whole day, both ends, as YYYY-MM-DD.
    assert ids(**{"from": day2.date().isoformat(), "to": day2.date().isoformat()}) == [SID2]
    # Exact date-times are inclusive too.
    exact = day2.astimezone().isoformat()
    assert ids(**{"from": exact, "to": exact}) == [SID2]
    assert ids(**{"from": exact}) == [SID3, SID2]
    assert ids(to=exact) == [SID2, SID]

    for bad in ({"from": "yesterday"}, {"to": "2024-13-45"}, {"from": "2024-05-09", "to": "2024-05-01"}):
        status, body = _get(env, "/mcp/v1/recordings", **bad)
        assert status == 400 and body["error"]


def test_paging(env):
    sids = [str(1715000000 + i * 1000) for i in range(5)]
    for sid in sids:
        _recording(env, sid, transcript=False)
    seen, cursor = [], None
    for _ in range(5):
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        status, body = _get(env, "/mcp/v1/recordings", **params)
        assert status == 200
        seen += [i["id"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(sids, reverse=True)

    # A full last page has no next cursor.
    assert _get(env, "/mcp/v1/recordings", limit=5)[1]["next_cursor"] is None
    # The default is 25.
    assert len(_get(env, "/mcp/v1/recordings")[1]["items"]) == 5

    for bad in ({"limit": 0}, {"limit": 101}, {"limit": "x"}, {"cursor": "not a cursor"}, {"cursor": "W10"}):
        status, body = _get(env, "/mcp/v1/recordings", **bad)
        assert status == 400 and body["error"], bad


# ---------------------------------------------------------------- get_recording


def test_get_shape(env):
    _recording(env, SID, speaker_names={"Speaker 2": "Sarah Jones"}, summary="Speaker 2 agreed with Speaker 10.")
    save_config({"recording_tags": {SID: ["Work"]}})
    status, body = _get(env, f"/mcp/v1/recordings/{SID}")
    assert status == 200
    assert set(body) > LIST_KEYS
    assert body["participants"] == ["Sarah Jones"]
    assert body["file"] == {
        "name": f"{SID}.ogg",
        "format": "ogg",
        "size_bytes": 4000,
        "folder": str(env.rec),  # not under the home folder in tests
        "location": "local",
        "audio_on_mac": True,
    }
    assert body["speakers"] == {"Speaker 2": "Sarah Jones"}
    assert body["you"] == []
    assert body["transcript"][1] == {
        "start_s": 21.0,
        "end_s": 60.0,
        "speaker_label": "Speaker 2",
        "speaker_name": "Sarah Jones",
        "text": "The forecast looks fine.",
    }
    assert body["transcript"][2]["speaker_name"] is None
    # Names applied whole-word: Speaker 10 isn't touched.
    assert body["summary"] == "Sarah Jones agreed with Speaker 10."
    assert body["coaching"] is None


def test_display_path_uses_tilde():
    from app.mcp.library import display_path

    home = Path.home()
    assert display_path(home / "Music" / "Plaud") == "~/Music/Plaud"
    assert display_path(home) == "~"
    assert display_path("/Volumes/Disk/rec") == "/Volumes/Disk/rec"


def test_get_include_subset(env):
    _recording(env, SID, summary="Text.")
    status, body = _get(env, f"/mcp/v1/recordings/{SID}", include="summary")
    assert status == 200
    assert body["summary"] == "Text."
    assert "transcript" not in body and "coaching" not in body
    status, body = _get(env, f"/mcp/v1/recordings/{SID}", include="transcript,coaching")
    assert "summary" not in body and "transcript" in body and "coaching" in body
    status, body = _get(env, f"/mcp/v1/recordings/{SID}", include="transcript,audio")
    assert status == 400 and "audio" in body["error"]


def test_get_without_transcript(env):
    _recording(env, SID, transcript=False)
    status, body = _get(env, f"/mcp/v1/recordings/{SID}")
    assert status == 200
    assert body["transcript"] is None and body["summary"] is None and body["coaching"] is None
    assert body["speakers"] == {} and body["you"] == []


def test_get_cloud_only(env):
    with patch(
        "app.state.gdrive.get_cloud_only_recordings",
        return_value=[{"session_id": SID, "drive_file_id": "d1", "synced_at": "", "file_size": 1234}],
    ):
        status, body = _get(env, f"/mcp/v1/recordings/{SID}")
    assert status == 200
    assert body["file"] == {
        "name": f"{SID}.ogg",
        "format": "ogg",
        "size_bytes": 1234,
        "folder": None,
        "location": "gdrive",
        "audio_on_mac": False,
    }


def test_you_and_your_name_only_after_a_user_pick(env):
    _recording(env, SID)
    update_config(lambda c: c.setdefault("profile", {}).__setitem__("name", "Ollie"))
    _sidecar(env, SID, {"owner": {**_user_owner(["Speaker 1"]), "source": "voice"}})
    body = _get(env, f"/mcp/v1/recordings/{SID}")[1]
    assert body["you"] == [] and body["speakers"] == {}
    _sidecar(env, SID, {"owner": _user_owner(["Speaker 1"])})
    body = _get(env, f"/mcp/v1/recordings/{SID}")[1]
    assert body["you"] == ["Speaker 1"]
    assert body["speakers"] == {"Speaker 1": "Ollie"}


def test_coaching_shown_with_names(env):
    _recording(env, SID, speaker_names={"Speaker 2": "Sarah"})
    _sidecar(
        env,
        SID,
        {
            "status": "done",
            "owner": _user_owner(["Speaker 1"]),
            "metrics": {"per_speaker": {"Speaker 1": {"talk_share": 0.4}}},
            "review": {"summary": "You let Speaker 2 finish.", "reactions": [{"text": "Speaker 2 nodded"}]},
            "words": [{"word": "um"}],
            "candidates": [],
        },
    )
    body = _get(env, f"/mcp/v1/recordings/{SID}")[1]
    assert body["coaching"] == {
        "status": "done",
        "metrics": {"per_speaker": {"Speaker 1": {"talk_share": 0.4}}},
        "review": {"summary": "You let Sarah finish.", "reactions": [{"text": "Sarah nodded"}]},
    }
    assert body["coaching_status"] == "done"


def test_coaching_includes_engagement_with_names(env):
    _recording(env, SID, speaker_names={"Speaker 2": "Sarah"})
    engagement = {
        "score": 64,
        "laughs": [{"t": 12.0, "line_start": 8.0, "line_text": "Tell Speaker 2 the joke"}],
        "laughs_count": 1,
        "laughs_per_hour": 51.4,
        "follow_ups": [{"t": 20.0, "text": "Speaker 2 asked why?"}],
        "follow_up_share": 0.5,
        "built_on": None,
        "built_on_share": None,
        "laughter_status": "done",
        "laughter_reason": "",
    }
    sidecar = {"status": "done", "owner": _user_owner(["Speaker 1"]), "metrics": {}, "review": None}
    _sidecar(env, SID, {**sidecar, "engagement": engagement})
    coaching = _get(env, f"/mcp/v1/recordings/{SID}")[1]["coaching"]
    assert coaching["engagement"] == {
        **engagement,
        "laughs": [{"t": 12.0, "line_start": 8.0, "line_text": "Tell Sarah the joke"}],
        "follow_ups": [{"t": 20.0, "text": "Sarah asked why?"}],
    }
    # A report from before engagement existed has no engagement key at all
    _sidecar(env, SID, sidecar)
    assert "engagement" not in _get(env, f"/mcp/v1/recordings/{SID}")[1]["coaching"]


def test_excluded_recordings_have_no_coaching(env):
    _recording(env, SID)
    _sidecar(env, SID, {"status": "excluded", "excluded": True, "metrics": {"x": 1}, "review": {"y": 2}})
    body = _get(env, f"/mcp/v1/recordings/{SID}")[1]
    assert body["coaching"] is None
    assert body["transcript"]  # the transcript is still there
    # Even a sidecar that somehow kept a report.
    _sidecar(env, SID, {"status": "done", "excluded": True, "metrics": {"x": 1}, "review": {"y": 2}})
    assert _get(env, f"/mcp/v1/recordings/{SID}")[1]["coaching"] is None


@pytest.mark.parametrize(
    "rid",
    ["1799999999", "..%2F..%2Fetc%2Fpasswd", "%2E%2E", "..", ".hidden", "a%00b", f"{SID}_uncropped"],
)
def test_unknown_or_bad_id_is_404(env, rid):
    _recording(env, SID)
    (env.rec / f"{SID}_uncropped.ogg").write_bytes(b"\0" * 10)
    for method, url, body in (
        ("get", f"/mcp/v1/recordings/{rid}", None),
        ("post", f"/mcp/v1/recordings/{rid}/speaker-names", {"names": {"Speaker 1": "A"}}),
        ("post", f"/mcp/v1/recordings/{rid}/tags", {"add": ["A"], "remove": []}),
    ):
        r = env.client.get(url) if method == "get" else env.client.post(url, json=body)
        assert r.status_code == 404, (url, r.get_json())
        assert r.get_json() == {"error": NOT_FOUND}
    assert load_config().get("recording_tags", {}) == {}


# ---------------------------------------------------------------- people and tags


def test_people_and_tags(env):
    _recording(env, SID, speaker_names={"Speaker 2": "Sarah Jones"})
    save_config({"recording_tags": {SID: ["Work"]}})
    status, body = _get(env, "/mcp/v1/people")
    assert status == 200 and body == {"items": [{"name": "Sarah Jones", "recordings": 1, "you": False}]}
    status, body = _get(env, "/mcp/v1/tags")
    assert status == 200
    assert {"tag": "Work", "recordings": 1} in body["items"]


# ---------------------------------------------------------------- writes


def test_set_speaker_names(env):
    _recording(env, SID)
    before = changes.current()
    status, body = _post(
        env, f"/mcp/v1/recordings/{SID}/speaker-names", {"names": {"Speaker 2": " Sarah Jones ", "Speaker 1": None}}
    )
    assert status == 200
    assert body == {"speakers": {"Speaker 2": "Sarah Jones"}}
    assert changes.current() > before
    stored = json.loads((env.trn / f"{SID}.json").read_text())
    assert stored["speaker_names"] == {"Speaker 2": "Sarah Jones"}

    status, body = _post(env, f"/mcp/v1/recordings/{SID}/speaker-names", {"names": {"Speaker 2": None}})
    assert status == 200 and body == {"speakers": {}}


def test_set_speaker_names_goes_through_the_app_function(env):
    _recording(env, SID)
    with patch("app.state.speakers.set_names", return_value={}) as set_names:
        _post(env, f"/mcp/v1/recordings/{SID}/speaker-names", {"names": {"Speaker 2": "Sarah"}})
    set_names.assert_called_once_with(SID, {"Speaker 2": "Sarah"}, from_ai=True)


def test_set_speaker_names_includes_your_confirmed_name(env):
    _recording(env, SID)
    update_config(lambda c: c.setdefault("profile", {}).__setitem__("name", "Ollie"))
    _sidecar(env, SID, {"owner": _user_owner(["Speaker 1"])})
    status, body = _post(env, f"/mcp/v1/recordings/{SID}/speaker-names", {"names": {"Speaker 2": "Sarah"}})
    assert body == {"speakers": {"Speaker 1": "Ollie", "Speaker 2": "Sarah"}}


@pytest.mark.parametrize(
    "body",
    [
        {"names": {"Speaker 9": "Bob"}},
        {"names": {"Speaker 2": "a:b"}},
        {"names": {"Speaker 2": "x" * 81}},
        {"names": {"Speaker 2": ""}},
        {"names": {}},
        {"names": ["Sarah"]},
        {},
    ],
)
def test_set_speaker_names_bad_input(env, body):
    _recording(env, SID)
    before = changes.current()
    status, reply = _post(env, f"/mcp/v1/recordings/{SID}/speaker-names", body)
    assert status == 400 and reply["error"]
    assert changes.current() == before


def test_set_speaker_names_without_transcript(env):
    _recording(env, SID, transcript=False)
    status, body = _post(env, f"/mcp/v1/recordings/{SID}/speaker-names", {"names": {"Speaker 1": "A"}})
    assert status == 400 and "transcript" in body["error"]


def test_tags_add_and_remove(env):
    _recording(env, SID)
    save_config({"recording_tags": {SID: ["Work"]}, "custom_tags": []})
    before = changes.current()
    status, body = _post(env, f"/mcp/v1/recordings/{SID}/tags", {"add": ["work", "Brand New"], "remove": []})
    assert status == 200
    assert body == {"tags": ["Work", "Brand New"]}
    assert changes.current() > before
    assert "Brand New" in load_config()["custom_tags"]
    assert json.loads((env.trn / f"{SID}.json").read_text())["tags"] == ["Work", "Brand New"]

    status, body = _post(env, f"/mcp/v1/recordings/{SID}/tags", {"add": [], "remove": ["WORK", "absent"]})
    assert status == 200 and body == {"tags": ["Brand New"]}


def test_tags_go_through_the_app_functions(env):
    _recording(env, SID)
    with (
        patch("app.utils.tags.add_tags", return_value=["A"]) as add,
        patch("app.utils.tags.remove_tags", return_value=[]) as remove,
    ):
        _post(env, f"/mcp/v1/recordings/{SID}/tags", {"add": ["A"], "remove": []})
        _post(env, f"/mcp/v1/recordings/{SID}/tags", {"add": [], "remove": ["A"]})
    add.assert_called_once_with(SID, ["A"])
    remove.assert_called_once_with(SID, ["A"])


@pytest.mark.parametrize(
    "body",
    [
        {"add": ["a" * 41]},
        {"add": ["ok", "bad\x07"]},
        {"add": [f"t{i}" for i in range(21)]},
        {"add": "Work"},
        {"add": [3]},
        {"add": [], "remove": []},
        {},
        {"remove": [""]},
    ],
)
def test_tags_bad_input_changes_nothing(env, body):
    _recording(env, SID)
    before = changes.current()
    status, reply = _post(env, f"/mcp/v1/recordings/{SID}/tags", body)
    assert status == 400 and reply["error"], body
    assert load_config().get("recording_tags", {}).get(SID, []) == []
    assert changes.current() == before


def test_tags_per_recording_limit(env):
    _recording(env, SID)
    save_config({"recording_tags": {SID: [f"t{i}" for i in range(20)]}})
    status, body = _post(env, f"/mcp/v1/recordings/{SID}/tags", {"add": ["one more"]})
    assert status == 400 and "20" in body["error"]


# ---------------------------------------------------------------- nothing else leaks


def test_no_secrets_in_any_response(env):
    secrets = {
        "openai_api_key": "sk-SECRET-OPENAI",
        "anthropic_api_key": "sk-ant-SECRET",
        "deepgram_api_key": "dg-SECRET",
        "notion_token": "ntn-SECRET",
        "gdrive_token": "gd-SECRET",
        "bind_token": "BIND-SECRET",
        "app_key": "APPKEY-SECRET",
    }
    save_config({**secrets, "recording_tags": {SID: ["Work"]}})
    _recording(env, SID, speaker_names={"Speaker 2": "Sarah"}, summary="Fine.", provider="deepgram")
    (env.tmp / "coaching").mkdir(exist_ok=True)
    (env.tmp / "coaching" / "voice_profile.json").write_text(json.dumps({"embedding": [0.123456789] * 4}))
    _sidecar(
        env,
        SID,
        {"status": "done", "owner": {**_user_owner(["Speaker 1"]), "voice_score": 0.987654}, "metrics": {}},
    )
    responses = [
        env.client.get("/mcp/v1/recordings"),
        env.client.get(f"/mcp/v1/recordings/{SID}"),
        env.client.get("/mcp/v1/people"),
        env.client.get("/mcp/v1/tags"),
        env.client.post(f"/mcp/v1/recordings/{SID}/speaker-names", json={"names": {"Speaker 1": "A"}}),
        env.client.post(f"/mcp/v1/recordings/{SID}/tags", json={"add": ["B"]}),
    ]
    assert [r.status_code for r in responses] == [200] * len(responses)
    blob = "\n".join(r.get_data(as_text=True) for r in responses)
    assert "Sarah" in blob and "Fine." in blob  # the scan saw real content
    for key, value in secrets.items():
        assert value not in blob
        assert key not in blob
    for word in ("api_key", "token", "voice_profile", "embedding", "0.123456789", "voice_score", "ogg_path"):
        assert word not in blob, word
    # No absolute paths beyond the display folder, and no audio bytes.
    assert str(env.trn) not in blob
    assert "\\u0000" not in blob


# ---------------------------------------------------------------- the app's tag routes and spoken tags


def test_app_tag_routes_404_for_unknown_recording(env):
    env.client.environ_base["HTTP_X_PLAUD_TOKEN"] = TEST_API_TOKEN
    _recording(env, SID)
    r = env.client.post("/recordings/tags/add", json={"session_ids": ["1799999999"], "tags": ["A"]})
    assert r.status_code == 404
    assert r.get_json()["error"] == NOT_FOUND
    r = env.client.post("/recordings/tags/add", json={"session_ids": [SID, "1799999999"], "tags": ["A"]})
    assert r.status_code == 200
    assert r.get_json()["tags"] == {SID: ["A"]}
    assert r.get_json()["errors"] == {"1799999999": NOT_FOUND}
    assert "1799999999" not in load_config().get("recording_tags", {})


def test_recording_exists(env):
    from app.routes.recordings import recording_exists

    _recording(env, SID, transcript=False)
    (env.rec / f"{SID2}.ogg.partial").write_bytes(b"\0")
    (env.rec / f"{SID}_uncropped.ogg").write_bytes(b"\0")
    _transcript(env, SID3)
    assert recording_exists(SID)
    assert recording_exists(SID2)  # an interrupted download is listed too
    assert not recording_exists(SID3)  # a transcript alone isn't a recording
    assert not recording_exists(f"{SID}_uncropped")
    for bad in ("../x", "..", "", None, ".x", "a/b"):
        assert not recording_exists(bad)
    with patch("app.state.gdrive.get_cloud_only_recordings", return_value=[{"session_id": "cloud1"}]):
        assert recording_exists("cloud1")


def test_spoken_tags_follow_the_tag_rules(env):
    from app.routes.transcribe import _apply_spoken_tags

    save_config({"recording_tags": {SID: [f"t{i}" for i in range(18)]}, "custom_tags": ["Standup"]})
    spoken = ["standup", "A" * 45, "Bad\x07Tag", "Planning", "Overflow"]
    with patch("app.routes.transcribe._extract_spoken_tags", return_value=spoken):
        applied = _apply_spoken_tags(SID, "Tags: ...")
    # The existing spelling wins; a too-long or control-character tag is skipped; the
    # 21st tag is skipped; nothing raises.
    assert applied == ["Standup", "Planning"]
    assert load_config()["recording_tags"][SID][-2:] == ["Standup", "Planning"]
    assert len(load_config()["recording_tags"][SID]) == 20
    assert "A" * 45 not in load_config()["custom_tags"]


# ---------------------------------------------------------------- end to end with the connector


def test_connector_end_to_end(env):
    """The real connector (app/mcp/server.py) against these routes on a real socket."""
    import os
    import subprocess
    import sys
    import threading

    from werkzeug.serving import make_server

    from app.api_server import app
    from app.mcp import access
    from app.utils import constants

    _recording(env, SID, summary="Speaker 2 agreed.")
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        access.enable(server.server_port)
        calls = [
            ("list_recordings", {"query": "forecast"}),
            ("set_speaker_names", {"id": SID, "names": {"Speaker 2": "Sarah Jones"}}),
            ("add_tags", {"id": SID, "tags": ["Budget"]}),
            ("remove_tags", {"id": SID, "tags": ["budget"]}),
            ("get_recording", {"id": SID, "include": ["summary"]}),
            ("list_people", {}),
            ("get_recording", {"id": "1799999999"}),
            ("add_tags", {"id": "..", "tags": ["x"]}),
        ]
        lines = [json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})]
        lines += [
            json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": a}})
            for i, (n, a) in enumerate(calls, start=1)
        ]
        child_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        child_env["PLAUD_DATA_DIR"] = str(constants.DATA_DIR)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "app" / "mcp" / "server.py")],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            env=child_env,
            timeout=60,
        )
    finally:
        server.shutdown()
        thread.join(5)
    replies = {r["id"]: r["result"] for r in map(json.loads, proc.stdout.splitlines())}

    def payload(i):
        result = replies[i]
        assert result["isError"] is False, result
        return json.loads(result["content"][0]["text"])

    assert [i["id"] for i in payload(1)["items"]] == [SID]
    assert payload(2) == {"speakers": {"Speaker 2": "Sarah Jones"}}
    assert payload(3) == {"tags": ["Budget"]}
    assert payload(4) == {"tags": []}
    got = payload(5)
    assert got["summary"] == "Sarah Jones agreed." and "transcript" not in got
    assert payload(6) == {"items": [{"name": "Sarah Jones", "recordings": 1, "you": False}]}
    for i in (7, 8):
        assert replies[i] == {"content": [{"type": "text", "text": NOT_FOUND}], "isError": True}


# ---------------------------------------------------------------- bad files and bad bounds


@pytest.mark.parametrize(
    "bad",
    [
        {"text": None, "segments": SEGMENTS},
        {"text": "hi", "segments": None},
        {"text": "hi", "segments": ["x"]},
        {"text": "hi", "segments": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": ["Speaker 1"]}]},
        {"text": "hi", "segments": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker": 5}]},
        {"text": "hi", "segments": [{"start": 0.0, "end": 1.0, "text": None, "speaker": "Speaker 1"}]},
        {"text": "hi", "segments": SEGMENTS, "summary": ["not text"], "auto_title": 7},
        ["not", "an", "object"],
        "not json at all {",
    ],
)
def test_malformed_transcript_is_skipped_not_a_500(env, bad):
    """One bad transcript file must not break listing or search (the window skips it too)."""
    _recording(env, SID)
    _audio(env, SID2)
    path = env.trn / f"{SID2}.json"
    path.write_text(bad if isinstance(bad, str) else json.dumps(bad))
    save_config({"recording_tags": {SID2: ["Work"]}})

    status, body = _get(env, "/mcp/v1/recordings")
    assert status == 200, body
    items = {i["id"]: i for i in body["items"]}
    assert set(items) == {SID, SID2}
    assert items[SID]["has_transcript"] is True
    broken = items[SID2]
    assert broken["participants"] == []
    assert broken["tags"] == ["Work"]
    assert isinstance(broken["title"], str) and broken["title"]

    status, body = _get(env, "/mcp/v1/recordings", query="forecast")
    assert status == 200 and [i["id"] for i in body["items"]] == [SID]
    assert _get(env, "/mcp/v1/recordings", query="work")[0] == 200

    status, body = _get(env, f"/mcp/v1/recordings/{SID2}")
    assert status == 200, body
    assert body["id"] == SID2
    assert body["file"]["audio_on_mac"] is True
    if body["has_transcript"] is False:
        assert body["transcript"] is None and body["speakers"] == {}
    json.dumps(body)


def test_malformed_transcript_names_no_participants(env):
    """A transcript the read model can't use gives no participants, even if a name is stored."""
    _audio(env, SID)
    (env.trn / f"{SID}.json").write_text(
        json.dumps({"text": None, "segments": SEGMENTS, "speaker_names": {"Speaker 2": "Sarah"}})
    )
    item = _get(env, "/mcp/v1/recordings")[1]["items"][0]
    assert item["has_transcript"] is False
    assert item["participants"] == []
    assert _get(env, "/mcp/v1/recordings", query="sarah")[1]["items"] == []
    body = _get(env, f"/mcp/v1/recordings/{SID}")[1]
    assert body["transcript"] is None and body["summary"] is None and body["speakers"] == {}


@pytest.mark.parametrize(
    "params",
    [
        {"to": "9999-12-31"},
        {"from": "9999-12-31"},
        {"from": "0001-01-01"},
        {"from": "0001-01-01T00:00:00"},
        {"to": "0001-01-01T00:00:00"},
        {"to": "9999-12-31T23:59:59.999999"},
        {"from": "9999-12-31T23:59:59-23:59"},
    ],
)
def test_extreme_dates_are_400_not_500(env, params):
    _recording(env, SID, transcript=False)
    status, body = _get(env, "/mcp/v1/recordings", **params)
    assert status in (200, 400), body
    if status == 400:
        assert body["error"]


def test_extreme_dates_that_cannot_be_used_are_400(env):
    _recording(env, SID, transcript=False)
    for params in ({"to": "9999-12-31"}, {"from": "0001-01-01T00:00:00"}):
        status, body = _get(env, "/mcp/v1/recordings", **params)
        assert status == 400, (params, body)
        assert "from" in body["error"] or "to" in body["error"]


def test_out_of_range_cursor_is_400(env):
    import base64

    _recording(env, SID, transcript=False)
    for value in ('[1e400, "x"]', '["NaN", "x"]', '[NaN, "x"]', '[Infinity, "x"]', f'[{10**400}, "x"]'):
        cursor = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
        status, body = _get(env, "/mcp/v1/recordings", cursor=cursor)
        assert status == 400, (value, body)
        assert "cursor" in body["error"]


# ---------------------------------------------------------------- people filter


def test_person_filter_is_exact_and_people_marks_you(env):
    _recording(env, SID, speaker_names={"Speaker 2": "Sam Carter"})
    _recording(env, SID2, speaker_names={"Speaker 2": "Sam"})
    save_config({"recording_tags": {SID2: ["Sam Carter"]}, "profile": {"name": "Alex"}})
    _sidecar(env, SID2, {"owner": _user_owner(["Speaker 1"])})
    ids = lambda **q: [i["id"] for i in _get(env, "/mcp/v1/recordings", **q)[1]["items"]]  # noqa: E731
    # query matches the tag too; person only where Sam Carter is a speaker
    assert set(ids(query="sam carter")) == {SID, SID2}
    assert ids(person="  sam   CARTER ") == [SID]
    assert ids(person="Alex") == [SID2]
    assert ids(person="Nobody") == []
    assert _get(env, "/mcp/v1/people")[1] == {
        "items": [
            {"name": "Alex", "recordings": 1, "you": True},
            {"name": "Sam", "recordings": 1, "you": False},
            {"name": "Sam Carter", "recordings": 1, "you": False},
        ]
    }
