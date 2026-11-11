"""People directory routes: list, rename, merge, delete, "Not them", and the name suggestions."""

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from app.coaching import runner, voice
from app.people import apply, backlog, store, worker
from app.state import speakers
from app.utils import changes
from app.utils.config import update_config

SEGMENTS = [
    {"start": 0.0, "end": 20.0, "text": "Hello there.", "speaker": "Speaker 1"},
    {"start": 21.0, "end": 60.0, "text": "Hi, how are you?", "speaker": "Speaker 2"},
]
LABELLING = runner.labelling_fingerprint(SEGMENTS)
PROFILE = "Ollie Bennett"
S1, S2, S3, S4 = "1715000000", "1715100000", "1715200000", "1715300000"


def _vec(axis: int, wobble: float = 0.0) -> list[float]:
    v = [0.0, 0.0, 0.0, 0.0]
    v[axis] = 1.0
    v[3] = wobble
    return v


@pytest.fixture
def env(tmp_path, flask_client):
    worker.wait_idle(10)
    trn = tmp_path / "transcripts"
    trn.mkdir()
    cache: dict = {}
    with ExitStack() as stack:
        for mod in ("app.routes.recordings", "app.state.speakers", "app.coaching.runner"):
            stack.enter_context(patch(f"{mod}.TRANSCRIPTS_DIR", trn))
        stack.enter_context(patch("app.coaching.store.COACHING_DIR", tmp_path / "coaching"))
        stack.enter_context(patch.object(store, "VOICES_PATH", tmp_path / "people" / "voices.json"))
        stack.enter_context(patch.object(backlog, "STATE_PATH", tmp_path / "people" / "backlog.json"))
        stack.enter_context(patch.object(voice, "PROFILE_PATH", tmp_path / "coaching" / "voice_profile.json"))
        stack.enter_context(patch.object(voice, "model_available", return_value=True))
        stack.enter_context(patch("app.people.embeddings.cached", side_effect=lambda sid, seg: cache.get(sid)))
        stack.enter_context(patch("app.people.embeddings.get", side_effect=lambda sid, seg, audio=None: cache.get(sid)))
        stack.enter_context(patch("app.routes.notion._get_notion_page_url", return_value="https://www.notion.so/p"))
        stack.enter_context(patch("app.state.notion.is_connected", return_value=True))
        notion = stack.enter_context(patch("app.utils.notion_queue.enqueue"))
        update_config(lambda c: c.update(profile={"name": PROFILE}))
        yield type("Env", (), {"client": flask_client, "trn": trn, "notion": notion, "cache": cache})
        backlog.cancel()
        assert worker.wait_idle(10)


def _person(name: str, n: int, axis: int, session_prefix: str = "old") -> str:
    """A person with ``n`` samples from recordings that no longer exist."""
    out = {}

    def fn(d):
        pid = store.create_person(d, name)
        for i in range(n):
            d["people"][pid]["samples"].append(
                {
                    "vec": _vec(axis, i / 10),
                    "session_id": f"{session_prefix}{i}",
                    "label": "Speaker 1",
                    "seconds": 20,
                    "added_at": f"2026-11-01T00:00:{i:02d}+00:00",
                }
            )
        out["pid"] = pid

    store.mutate(fn)
    return out["pid"]


def _me(n: int) -> None:
    def fn(d):
        d["people"][store.ME]["samples"] = [
            {"vec": _vec(2, i / 10), "session_id": f"me{i}", "label": "Speaker 1", "seconds": 20, "added_at": None}
            for i in range(n)
        ]

    store.mutate(fn)


def _write(env, sid, names=None, matches=None, rejected=None, **extra):
    t = {"text": "x", "segments": SEGMENTS, **extra}
    if names:
        t["speaker_names"] = names
    if matches is not None or rejected is not None:
        t["speaker_voice_matches"] = {
            "labelling": LABELLING,
            "matches": {label: {"person": pid, "score": 0.9} for label, pid in (matches or {}).items()},
            "rejected": rejected or {},
        }
    (env.trn / f"{sid}.json").write_text(json.dumps(t))


def _read(env, sid) -> dict:
    return json.loads((env.trn / f"{sid}.json").read_text())


def _notion_sids(env) -> set[str]:
    return {c.args[0] for c in env.notion.call_args_list}


# ---------------------------------------------------------------- the directory


def test_directory_lists_me_and_people_without_vectors(env):
    _me(3)
    harry = _person("Harry Fisher", 4, 0)
    _person("Sam", 2, 1)
    update_config(lambda c: c.update(people_calibration={"threshold": 0.7, "accuracy": 0.98, "instances": 30}))
    _write(env, S1, names={"Speaker 2": "harry fisher"})  # typed, any case
    _write(env, S2, matches={"Speaker 1": harry, "Speaker 2": store.ME})  # matched
    _write(env, S3, names={"Speaker 1": "Someone Else"})

    r = env.client.get("/people/directory")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert "vec" not in text and "0.1" not in text
    body = r.get_json()
    assert body["me"]["id"] == "me" and body["me"]["name"] == PROFILE and body["me"]["samples"] == 3
    assert [p["name"] for p in body["people"]] == ["Harry Fisher", "Sam"]
    harry_row = body["people"][0]
    assert harry_row["id"] == harry and harry_row["samples"] == 4 and harry_row["recordings"] == 2
    assert body["people"][1]["recordings"] == 0
    assert body["total_bytes"] == body["me"]["bytes"] + sum(p["bytes"] for p in body["people"]) > 0
    assert body["model"] is True
    assert body["backlog"]["phase"] == "idle"
    assert body["calibration"] == {"threshold": 0.7, "accuracy": 0.98, "instances": 30}


def test_directory_without_a_profile_name_or_calibration(env):
    update_config(lambda c: c.update(profile={}))
    body = env.client.get("/people/directory").get_json()
    assert body["me"]["name"] is None and body["people"] == [] and body["calibration"] is None


# ---------------------------------------------------------------- rename


def test_rename_rewrites_typed_names_and_queues_notion(env):
    harry = _person("Harry", 3, 0)
    _write(env, S1, names={"Speaker 1": "harry", "Speaker 2": "Sam"})  # typed, other case
    _write(env, S2, matches={"Speaker 2": harry})  # matched: its shown name changes too
    _write(env, S3, names={"Speaker 1": "Harriet"})  # untouched
    with patch.object(worker, "enqueue") as enqueue:
        r = env.client.post(f"/people/{harry}/rename", json={"name": "  Harry Fisher "})
    assert r.status_code == 200, r.get_json()
    assert not enqueue.called  # no learning, unlearning or rematch
    assert _read(env, S1)["speaker_names"] == {"Speaker 1": "Harry Fisher", "Speaker 2": "Sam"}
    assert _read(env, S3)["speaker_names"] == {"Speaker 1": "Harriet"}
    assert store.load()["people"][harry]["name"] == "Harry Fisher"
    assert len(store.load()["people"][harry]["samples"]) == 3
    assert _notion_sids(env) == {S1, S2}
    assert [p["name"] for p in r.get_json()["people"]] == ["Harry Fisher"]


def test_rename_to_existing_name_409(env):
    _person("Harry", 1, 0)
    sam = _person("Sam", 1, 1)
    r = env.client.post(f"/people/{sam}/rename", json={"name": "HARRY"})
    assert r.status_code == 409 and "Harry" in r.get_json()["error"]
    r = env.client.post(f"/people/{sam}/rename", json={"name": "ollie bennett"})
    assert r.status_code == 409 and "your name" in r.get_json()["error"]
    assert env.client.post(f"/people/{sam}/rename", json={"name": "a:b"}).status_code == 400
    assert env.client.post("/people/me/rename", json={"name": "Me"}).status_code == 400
    assert env.client.post("/people/p_00000000/rename", json={"name": "X"}).status_code == 404
    assert env.client.post("/people/..%2Fx/rename", json={"name": "X"}).status_code in (400, 404)
    assert store.load()["people"][sam]["name"] == "Sam"
    # Changing only the letter case of a person's own name is fine.
    assert env.client.post(f"/people/{sam}/rename", json={"name": "SAM"}).status_code == 200
    assert store.load()["people"][sam]["name"] == "SAM"


# ---------------------------------------------------------------- merge


def test_merge_combines_and_trims_to_20(env):
    harry = _person("Harry Fisher", 12, 0, "h")
    dup = _person("Harry F", 12, 0, "d")
    sam = _person("Sam", 1, 1)
    _write(env, S1, names={"Speaker 1": "harry f"})
    _write(env, S2, matches={"Speaker 1": dup, "Speaker 2": sam})
    _write(env, S3, matches={"Speaker 2": sam}, rejected={"Speaker 1": [dup, sam]})
    _write(env, S4, names={"Speaker 1": "Harry Fisher"})
    with patch.object(worker, "enqueue") as enqueue:
        r = env.client.post(f"/people/{dup}/merge", json={"into": harry})
    assert r.status_code == 200, r.get_json()
    enqueue.assert_called_once_with("rematch")
    people = store.load()["people"]
    assert dup not in people
    assert len(people[harry]["samples"]) == 20
    assert {s["session_id"][0] for s in people[harry]["samples"]} == {"h", "d"}
    assert _read(env, S1)["speaker_names"] == {"Speaker 1": "Harry Fisher"}
    block = _read(env, S2)["speaker_voice_matches"]
    assert block["matches"]["Speaker 1"]["person"] == harry and block["matches"]["Speaker 2"]["person"] == sam
    assert _read(env, S3)["speaker_voice_matches"]["rejected"] == {"Speaker 1": [harry, sam]}
    assert _notion_sids(env) == {S1, S2}
    assert [p["name"] for p in r.get_json()["people"]] == ["Harry Fisher", "Sam"]


def test_merge_refuses_me_itself_and_unknown(env):
    harry = _person("Harry", 1, 0)
    assert env.client.post("/people/me/merge", json={"into": harry}).status_code == 400
    assert env.client.post(f"/people/{harry}/merge", json={"into": harry}).status_code == 400
    assert env.client.post(f"/people/{harry}/merge", json={"into": "nope"}).status_code == 400
    assert env.client.post(f"/people/{harry}/merge", json={"into": "p_00000000"}).status_code == 404
    assert harry in store.load()["people"]


# ---------------------------------------------------------------- delete


def test_delete_person_freezes_names_without_notion_update(env):
    harry = _person("Harry", 3, 0)
    sam = _person("Sam", 1, 1)
    _write(env, S1, matches={"Speaker 1": harry, "Speaker 2": sam})  # shown by voice
    _write(env, S2, names={"Speaker 1": "Harry Senior"}, matches={"Speaker 1": harry})  # typed wins
    _write(env, S3, rejected={"Speaker 2": [harry]})
    before = changes.current()
    with patch.object(worker, "enqueue") as enqueue:
        r = env.client.delete(f"/people/{harry}")
    assert r.status_code == 200, r.get_json()
    assert not enqueue.called
    t1 = _read(env, S1)
    assert t1["speaker_names"] == {"Speaker 1": "Harry"}
    assert t1["speaker_names_frozen"] == ["Speaker 1"]
    assert t1["speaker_names_labelling"] == LABELLING
    assert t1["speaker_voice_matches"]["matches"] == {"Speaker 2": {"person": sam, "score": 0.9}}
    t2 = _read(env, S2)
    assert t2["speaker_names"] == {"Speaker 1": "Harry Senior"} and "speaker_names_frozen" not in t2
    assert t2["speaker_voice_matches"]["matches"] == {}
    assert _read(env, S3)["speaker_voice_matches"]["rejected"] == {}
    assert harry not in store.load()["people"]
    assert env.notion.call_count == 0
    assert changes.current() > before
    # The names shown are what they were.
    names = {t["session_id"]: t["speaker_names"] for t in env.client.get("/transcripts").get_json()}
    assert names[S1] == {"Speaker 1": "Harry", "Speaker 2": "Sam"}


def test_delete_me_refused(env):
    _me(2)
    assert env.client.delete("/people/me").status_code == 400
    assert env.client.delete("/people/p_zz").status_code == 400
    assert env.client.delete("/people/p_00000000").status_code == 404
    assert len(store.load()["people"][store.ME]["samples"]) == 2


# ---------------------------------------------------------------- "Not them"


def test_reject_unnames_and_survives_rematch(env):
    harry = _person("Harry", 3, 0)
    sam = _person("Sam", 3, 1)
    update_config(lambda c: c.update(people_match_threshold=0.7))
    _write(env, S1, matches={"Speaker 1": harry, "Speaker 2": sam})
    env.cache[S1] = {"Speaker 1": _vec(0), "Speaker 2": _vec(1)}

    r = env.client.post("/transcripts/speaker-names/reject", json={"session_id": S1, "label": "Speaker 1"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["speaker_names"] == {"Speaker 2": "Sam"}
    assert set(body["speaker_name_sources"]) == {"Speaker 2"}
    assert _notion_sids(env) == {S1}
    block = _read(env, S1)["speaker_voice_matches"]
    assert block["rejected"] == {"Speaker 1": [harry]} and "Speaker 1" not in block["matches"]

    apply.match_recording(S1, cached_only=True)
    block = _read(env, S1)["speaker_voice_matches"]
    assert "Speaker 1" not in block["matches"] and block["matches"]["Speaker 2"]["person"] == sam

    bad = env.client.post("/transcripts/speaker-names/reject", json={"session_id": S1, "label": "Speaker 1"})
    assert bad.status_code == 400
    missing = env.client.post("/transcripts/speaker-names/reject", json={"session_id": S4, "label": "Speaker 1"})
    assert missing.status_code == 404
    assert (
        env.client.post("/transcripts/speaker-names/reject", json={"session_id": "../x", "label": "x"}).status_code
        == 400
    )


# ---------------------------------------------------------------- backlog


def test_backlog_start_and_cancel(env):
    with patch.object(backlog, "start", return_value=True) as start:
        r = env.client.post("/people/backlog")
    assert r.status_code == 200 and start.called and "phase" in r.get_json()["backlog"]
    with patch.object(backlog, "start", return_value=False):
        assert env.client.post("/people/backlog").status_code == 409
    with patch.object(backlog, "cancel") as cancel:
        r = env.client.delete("/people/backlog")
    assert r.status_code == 200 and cancel.called


# ---------------------------------------------------------------- suggestions and MCP


def test_suggestions_list_people_with_a_voice_first(env):
    harry = _person("Harry", 1, 0)
    _person("Nobody Typed", 1, 1)
    _write(env, S1, names={"Speaker 1": "Bob", "Speaker 2": "harry"})
    _write(env, S2, names={"Speaker 1": "Bob"})
    items = env.client.get("/people").get_json()
    assert items == [
        {"name": "harry", "recordings": 1, "person_id": harry, "has_voice": True},
        {"name": "Nobody Typed", "recordings": 0, "person_id": items[1]["person_id"], "has_voice": True},
        {"name": "Bob", "recordings": 2, "person_id": None, "has_voice": False},
    ]


def test_mcp_people_has_no_vectors(env):
    from app.mcp import access

    _me(2)
    _person("Harry", 3, 0)
    _write(env, S1, names={"Speaker 1": "Harry"})
    access.enable(5123)
    try:
        env.client.environ_base.pop("HTTP_X_PLAUD_TOKEN", None)
        env.client.environ_base["HTTP_X_OPEN_PIN_MCP_KEY"] = access.current_key()
        r = env.client.get("/mcp/v1/people")
    finally:
        access.disable()
    assert r.status_code == 200
    assert r.get_json() == {"items": [{"name": "Harry", "recordings": 1}]}
    assert "vec" not in r.get_data(as_text=True)


def test_set_speaker_names_tool_says_names_teach_voices():
    from app.mcp.server import TOOLS

    tool = next(t for t in TOOLS if t["name"] == "set_speaker_names")
    assert (
        "Names you set teach Open Pin that person's voice, so later recordings are named automatically."
        in tool["description"]
    )


def test_merge_when_both_match_labels_on_one_recording(env):
    harry = _person("Harry Fisher", 2, 0, "h")
    dup = _person("Harry F", 2, 0, "d")
    _write(env, S1, matches={"Speaker 1": dup, "Speaker 2": harry})
    before = changes.current()
    with patch.object(worker, "enqueue"):
        assert env.client.post(f"/people/{dup}/merge", json={"into": harry}).status_code == 200
    matches = _read(env, S1)["speaker_voice_matches"]["matches"]
    assert {label: m["person"] for label, m in matches.items()} == {"Speaker 1": harry, "Speaker 2": harry}
    names = {t["session_id"]: t["speaker_names"] for t in env.client.get("/transcripts").get_json()}
    assert names[S1] == {"Speaker 1": "Harry Fisher", "Speaker 2": "Harry Fisher"}
    assert _notion_sids(env) == {S1}
    assert changes.current() > before


def test_rename_bumps_changes_even_when_no_transcript_shows_them(env):
    sam = _person("Sam", 1, 1)
    before = changes.current()
    assert env.client.post(f"/people/{sam}/rename", json={"name": "Sam Jones"}).status_code == 200
    assert changes.current() > before


# ---------------------------------------------------------------- a quit midway, then a retry


def _fail_store_write_once():
    real = store.mutate
    calls = {"n": 0}

    def flaky(fn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("quit")
        return real(fn)

    return patch.object(store, "mutate", side_effect=flaky)


def test_rename_stopped_after_the_transcripts_completes_on_retry(env):
    from app.people import directory

    harry = _person("Harry", 2, 0)
    _write(env, S1, names={"Speaker 1": "Harry"})
    _write(env, S2, matches={"Speaker 1": harry})
    with _fail_store_write_once(), pytest.raises(RuntimeError):
        directory.rename(harry, "Harry Fisher")
    assert store.load()["people"][harry]["name"] == "Harry"  # the store is written last
    assert _read(env, S1)["speaker_names"] == {"Speaker 1": "Harry Fisher"}  # after the transcripts
    env.notion.reset_mock()
    r = env.client.post(f"/people/{harry}/rename", json={"name": "Harry Fisher"})
    assert r.status_code == 200, r.get_json()
    people = store.load()["people"]
    assert people[harry]["name"] == "Harry Fisher" and len(people) == 2  # me + Harry
    assert _read(env, S1)["speaker_names"] == {"Speaker 1": "Harry Fisher"}
    assert S2 in _notion_sids(env)  # the voice-matched name changes only now


def test_merge_stopped_after_the_transcripts_completes_on_retry(env):
    from app.people import directory

    harry = _person("Harry Fisher", 2, 0, "h")
    dup = _person("Harry F", 2, 0, "d")
    _write(env, S1, names={"Speaker 1": "Harry F"})
    _write(env, S2, matches={"Speaker 1": dup})
    with patch.object(worker, "enqueue"):
        with _fail_store_write_once(), pytest.raises(RuntimeError):
            directory.merge(dup, harry)
        assert dup in store.load()["people"]  # the store is written last
        assert _read(env, S1)["speaker_names"] == {"Speaker 1": "Harry Fisher"}  # after the transcripts
        r = env.client.post(f"/people/{dup}/merge", json={"into": harry})
    assert r.status_code == 200, r.get_json()
    people = store.load()["people"]
    assert dup not in people and len(people[harry]["samples"]) == 4
    assert _read(env, S1)["speaker_names"] == {"Speaker 1": "Harry Fisher"}
    assert _read(env, S2)["speaker_voice_matches"]["matches"]["Speaker 1"]["person"] == harry


# ---------------------------------------------------------------- directory writes wait for voice jobs


def test_rename_waits_for_a_learn_job_in_hand(env):
    import threading

    from app.people import directory

    harry = _person("Harry", 2, 0)
    _write(env, S1, names={"Speaker 1": "Harry"})
    env.cache[S1] = {"Speaker 1": _vec(0, 0.5), "Speaker 2": _vec(1)}
    embedding, release = threading.Event(), threading.Event()

    def slow_get(sid, segments, audio=None):
        embedding.set()
        assert release.wait(10)
        return env.cache.get(sid)

    with patch("app.people.embeddings.get", side_effect=slow_get):
        worker.enqueue("learn", session_id=S1, label="Speaker 1", name="Harry")
        assert embedding.wait(10)
        renaming = threading.Thread(target=directory.rename, args=(harry, "Harry Fisher"))
        renaming.start()
        renaming.join(0.3)
        assert renaming.is_alive()  # waits for the learn job
        release.set()
        renaming.join(10)
        assert worker.wait_idle(10)
    people = store.load()["people"]
    assert [p["name"] for pid, p in people.items() if pid != store.ME] == ["Harry Fisher"]
    assert any(s["session_id"] == S1 for s in people[harry]["samples"])
    assert _read(env, S1)["speaker_names"] == {"Speaker 1": "Harry Fisher"}


def test_a_waiting_directory_write_makes_the_backlog_yield(env):
    import threading

    assert worker._others_waiting() is False
    with worker.exclusive():
        entered = threading.Event()

        def wait_turn():
            with worker.exclusive():
                entered.set()

        waiting = threading.Thread(target=wait_turn)
        waiting.start()
        for _ in range(100):
            if worker._others_waiting():
                break
            waiting.join(0.01)
        assert worker._others_waiting() is True
        assert not entered.is_set()
    waiting.join(5)
    assert entered.is_set() and worker._others_waiting() is False


# ---------------------------------------------------------------- final review fixes


def test_editing_segments_keeps_voice_names_for_labels_still_there(env):
    harry, sam = _person("Harry Fisher", 3, 0), _person("Sam", 2, 1)
    _write(env, S1, matches={"Speaker 1": harry, "Speaker 2": sam}, rejected={"Speaker 2": [harry]})
    assert speakers.get_names(S1) == {"Speaker 1": "Harry Fisher", "Speaker 2": "Sam"}
    segs = [{**SEGMENTS[0], "start": 1.0}]  # Speaker 2's only segment deleted, Speaker 1's moved
    assert env.client.post("/transcripts/update", json={"session_id": S1, "segments": segs}).status_code == 200
    assert speakers.get_names(S1) == {"Speaker 1": "Harry Fisher"}
    block = _read(env, S1)["speaker_voice_matches"]
    assert block["labelling"] == runner.labelling_fingerprint(segs)
    assert list(block["matches"]) == ["Speaker 1"] and block["rejected"] == {}
    assert S1 in _notion_sids(env)  # Sam's name went


def test_editing_segments_keeps_a_stale_match_block_stale(env):
    harry = _person("Harry Fisher", 3, 0)
    _write(env, S1)
    t = _read(env, S1)
    t["speaker_voice_matches"] = {"labelling": "old", "matches": {"Speaker 1": {"person": harry}}, "rejected": {}}
    (env.trn / f"{S1}.json").write_text(json.dumps(t))
    segs = [{**SEGMENTS[0], "start": 1.0}, SEGMENTS[1]]
    env.client.post("/transcripts/update", json={"session_id": S1, "segments": segs})
    assert speakers.get_names(S1) == {}
    assert _read(env, S1)["speaker_voice_matches"]["labelling"] == "old"
    assert not env.notion.called


def test_a_cleared_name_never_comes_back_by_voice(env):
    update_config(lambda c: c.update(people_match_threshold=0.7))
    harry = _person("Harry Fisher", 3, 0)
    env.cache[S1] = {"Speaker 1": _vec(0), "Speaker 2": _vec(2)}
    _me(3)
    _write(env, S1)
    speakers.set_names(S1, {"Speaker 1": "harry fisher", "Speaker 2": PROFILE})
    assert worker.wait_idle(10)
    speakers.set_names(S1, {"Speaker 1": None, "Speaker 2": None})
    assert worker.wait_idle(10)
    assert speakers.get_names(S1) == {}
    assert _read(env, S1)["speaker_voice_matches"]["rejected"] == {"Speaker 1": [harry], "Speaker 2": [store.ME]}
    speakers.set_names(S1, {"Speaker 1": "Harry Fisher"})  # typing them again lifts it
    assert _read(env, S1)["speaker_voice_matches"]["rejected"] == {"Speaker 2": [store.ME]}


def test_a_deleted_person_is_not_learnt_again_from_their_typed_names(env):
    env.cache[S1] = {"Speaker 1": _vec(0), "Speaker 2": _vec(1)}
    env.cache[S2] = {"Speaker 1": _vec(0, 0.1), "Speaker 2": _vec(1)}
    _write(env, S1, names={"Speaker 1": "Harry"})
    _write(env, S2, names={"Speaker 1": "harry", "Speaker 2": "Sam"})
    backlog.start()
    assert worker.wait_idle(10)
    pid = store.find_person(store.load(), "Harry")
    assert env.client.delete(f"/people/{pid}").status_code == 200
    assert _read(env, S1)["speaker_names_frozen"] == ["Speaker 1"]
    t2 = _read(env, S2)
    assert t2["speaker_names_frozen"] == ["Speaker 1"]
    assert t2["speaker_names"] == {"Speaker 1": "harry", "Speaker 2": "Sam"}
    backlog.start()
    assert worker.wait_idle(10)
    assert store.find_person(store.load(), "Harry") is None
    assert store.find_person(store.load(), "Sam") is not None


def test_merge_into_you(env):
    _me(2)
    ollie = _person("Ollie", 3, 2, "o")
    _write(env, S1, names={"Speaker 2": "ollie"})
    _write(env, S2, matches={"Speaker 1": ollie})
    _write(env, S3, rejected={"Speaker 2": [ollie]})
    with patch.object(worker, "enqueue") as enqueue:
        r = env.client.post(f"/people/{ollie}/merge", json={"into": "me"})
    assert r.status_code == 200, r.get_json()
    enqueue.assert_called_once_with("rematch")
    people = store.load()["people"]
    assert ollie not in people and len(people[store.ME]["samples"]) == 5
    assert _read(env, S1)["speaker_names"] == {"Speaker 2": PROFILE}
    assert _read(env, S2)["speaker_voice_matches"]["matches"]["Speaker 1"]["person"] == store.ME
    assert _read(env, S3)["speaker_voice_matches"]["rejected"] == {"Speaker 2": [store.ME]}
    assert speakers.get_names(S2) == {"Speaker 1": PROFILE}


def test_merge_into_you_needs_your_name(env):
    update_config(lambda c: c.update(profile={}))
    ollie = _person("Ollie", 1, 2)
    r = env.client.post(f"/people/{ollie}/merge", json={"into": "me"})
    assert r.status_code == 409 and "name" in r.get_json()["error"]
    assert ollie in store.load()["people"]


def test_saving_your_name_folds_that_person_into_you(env):
    update_config(lambda c: c.update(profile={}))
    _me(2)
    ollie = _person("Ollie", 3, 2, "o")
    _person("Sam", 1, 1)
    _write(env, S1, names={"Speaker 2": "ollie"})
    _write(env, S2, matches={"Speaker 1": ollie}, rejected={"Speaker 2": [ollie]})
    r = env.client.post("/settings", json={"profile": {"name": "Ollie"}})
    assert r.status_code == 200
    assert worker.wait_idle(10)
    people = store.load()["people"]
    assert ollie not in people and len(people[store.ME]["samples"]) == 5
    assert [p["name"] for pid, p in people.items() if pid != store.ME] == ["Sam"]
    assert _read(env, S1)["speaker_names"] == {"Speaker 2": "ollie"}  # typed names stay as typed
    block = _read(env, S2)["speaker_voice_matches"]
    assert block["matches"]["Speaker 1"]["person"] == store.ME and block["rejected"] == {"Speaker 2": [store.ME]}
    # A name nobody in People has changes nothing.
    env.client.post("/settings", json={"profile": {"name": "Someone New"}})
    assert len(store.load()["people"]) == 2


def test_marking_mixed_up_forgets_what_the_recording_taught(env):
    update_config(lambda c: c.update(people_match_threshold=0.7))
    _me(2)
    harry = _person("Harry", 2, 0, "h")

    def taught(d):
        d["people"][harry]["samples"] = [{"vec": _vec(0), "session_id": S1, "label": "Speaker 1", "added_at": None}]
        d["people"][store.ME]["samples"].append({"vec": _vec(2), "session_id": S1, "label": "Speaker 2"})

    store.mutate(taught)
    _write(env, S1, names={"Speaker 1": "Harry", "Speaker 2": PROFILE})
    _write(env, S2, matches={"Speaker 1": harry})
    env.cache[S2] = {"Speaker 1": _vec(0), "Speaker 2": _vec(1)}
    assert speakers.get_names(S2) == {"Speaker 1": "Harry"}
    r = env.client.post(f"/coaching/{S1}/mixed-up", json={"mixed_up": True})
    assert r.status_code == 200
    assert worker.wait_idle(10)
    people = store.load()["people"]
    assert people[harry]["samples"] == []
    assert [s["session_id"] for s in people[store.ME]["samples"]] == ["me0", "me1"]
    assert speakers.get_names(S2) == {}  # re-matched without them


def test_ai_tools_sending_a_voice_match_back_teach_nothing(env):
    from unittest.mock import call

    from app.mcp import access

    harry, sam = _person("Harry Fisher", 3, 0), _person("Sam", 1, 1)
    _write(env, S1, matches={"Speaker 1": harry, "Speaker 2": sam})
    access.enable(5123)
    try:
        env.client.environ_base.pop("HTTP_X_PLAUD_TOKEN", None)
        env.client.environ_base["HTTP_X_OPEN_PIN_MCP_KEY"] = access.current_key()
        names = {"Speaker 1": "harry fisher", "Speaker 2": "Samuel"}
        with (
            patch.object(worker, "enqueue") as enqueue,
            patch("app.routes.mcp_api.recording_exists", return_value=True),
        ):
            r = env.client.post(f"/mcp/v1/recordings/{S1}/speaker-names", json={"names": names})
    finally:
        access.disable()
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["speakers"] == {"Speaker 1": "Harry Fisher", "Speaker 2": "Samuel"}
    assert _read(env, S1)["speaker_names"] == {"Speaker 2": "Samuel"}  # the match stays a match
    assert enqueue.call_args_list == [call("learn", session_id=S1, label="Speaker 2", name="Samuel")]
    # Save in the app still accepts a match by storing it as typed.
    speakers.set_names(S1, {"Speaker 1": "Harry Fisher"})
    assert _read(env, S1)["speaker_names"]["Speaker 1"] == "Harry Fisher"
