import threading
from unittest.mock import patch

import pytest

from app.coaching import store
from tests.test_transcribe_providers import RESULT, _transcribe, route_env  # noqa: F401


@pytest.fixture(autouse=True)
def coaching_dir(tmp_path):
    with patch("app.coaching.store.COACHING_DIR", tmp_path / "coaching"):
        yield tmp_path / "coaching"


def test_load_missing_returns_empty():
    assert store.load("nope") == {}


def test_update_roundtrip_atomic(coaching_dir):
    out = store.update("s1", lambda d: d.update(status="done"))
    assert out["status"] == "done" and out["version"] == store.COACHING_SCHEMA_VERSION
    loaded = store.load("s1")
    assert loaded["status"] == "done" and loaded["version"] == 1
    assert [p.name for p in coaching_dir.iterdir()] == ["s1.json"]  # no temp file left


def test_mark_stale_only_when_report_exists():
    store.mark_stale("s1")
    assert not store.coaching_path("s1").exists()  # never creates a sidecar
    store.update("s1", lambda d: d.update(status="running"))
    store.mark_stale("s1")
    assert "report_stale" not in store.load("s1")
    store.update("s1", lambda d: d.update(status="done", review={"summary": "x"}))
    store.mark_stale("s1")
    assert store.load("s1")["report_stale"] is True


def test_corrupt_file_loads_empty(coaching_dir):
    coaching_dir.mkdir()
    store.coaching_path("bad").write_text("{not json")
    assert store.load("bad") == {}
    store.coaching_path("bad").write_text("[1, 2]")
    assert store.load("bad") == {}


def test_concurrent_updates_do_not_lose_writes():
    def bump(d):
        d["n"] = d.get("n", 0) + 1

    threads = [threading.Thread(target=store.update, args=("c", bump)) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert store.load("c")["n"] == 20


def test_merge_transcript_marks_coaching_stale(route_env):  # noqa: F811
    from app.routes.transcribe import _merge_transcript

    _, _, trn = route_env
    path = trn / "1715000000.json"
    done = []

    def run(text):
        _merge_transcript("1715000000", path, {"text": text}, txt_text=text)
        done.append(text)

    t = threading.Thread(target=run, args=("first",))
    t.start()
    t.join(timeout=5)
    assert done == ["first"], "merge deadlocked or failed"  # coaching lock != transcript lock

    store.update("1715000000", lambda d: d.update(status="done", review={"summary": "x"}))
    run("first")  # unchanged text: not stale
    assert "report_stale" not in store.load("1715000000")
    run("second")  # changed text: stale
    assert store.load("1715000000")["report_stale"] is True


def test_transcripts_update_marks_coaching_stale(route_env):  # noqa: F811
    import json

    client, _, trn = route_env
    sid = "1715000001"
    (trn / f"{sid}.json").write_text(json.dumps({"text": "a b", "segments": []}))
    store.update(sid, lambda d: d.update(status="done", review={"summary": "x"}))
    seg = [{"start": 0, "end": 1, "text": "a b"}]
    with patch("app.routes.recordings.TRANSCRIPTS_DIR", trn):
        client.post("/transcripts/update", json={"session_id": sid, "segments": seg})
        assert "report_stale" not in store.load(sid)  # same text
        seg[0]["text"] = "changed"
        r = client.post("/transcripts/update", json={"session_id": sid, "segments": seg})
    assert r.status_code == 200
    assert store.load(sid)["report_stale"] is True
