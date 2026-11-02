"""Crash-safety and locking tests for app.utils.config."""

import json
import os
import stat
import threading
import time


def test_live_missing_falls_back_to_backup(tmp_config):
    """A crash that leaves only the .bak must not reset the config to {}."""
    from app.utils.config import load_config

    backup = tmp_config.with_suffix(".json.bak")
    backup.write_text(json.dumps({"deleted_ids": ["a"], "deepgram_api_key": "k"}))
    assert not tmp_config.exists()

    cfg = load_config()
    assert cfg["deleted_ids"] == ["a"]
    # Live file is restored from the backup
    assert json.loads(tmp_config.read_text())["deleted_ids"] == ["a"]


def test_live_corrupt_falls_back_to_backup(tmp_config):
    from app.utils.config import load_config

    tmp_config.write_text("{not json")
    tmp_config.with_suffix(".json.bak").write_text(json.dumps({"x": 1}))
    assert load_config() == {"x": 1}


def test_save_keeps_previous_as_backup_and_live_always_exists(tmp_config):
    from app.utils.config import load_config, save_config

    save_config({"v": 1})
    save_config({"v": 2})
    assert json.loads(tmp_config.read_text()) == {"v": 2}
    assert json.loads(tmp_config.with_suffix(".json.bak").read_text()) == {"v": 1}
    assert load_config() == {"v": 2}


def test_crash_before_replace_leaves_live_intact(tmp_config, monkeypatch):
    """If os.replace never happens, the old live file is still there."""
    import app.utils.config as config_mod

    config_mod.save_config({"v": 1})

    def boom(*a, **k):
        raise OSError("simulated crash")

    monkeypatch.setattr(config_mod.os, "replace", boom)
    try:
        config_mod.save_config({"v": 2})
    except OSError:
        pass
    monkeypatch.undo()
    assert json.loads(tmp_config.read_text()) == {"v": 1}
    # No temp files left behind
    assert not list(tmp_config.parent.glob(".config_*.tmp"))


def test_corrupt_live_does_not_overwrite_good_backup(tmp_config):
    from app.utils.config import save_config

    tmp_config.with_suffix(".json.bak").write_text(json.dumps({"good": True}))
    tmp_config.write_text("{corrupt")
    save_config({"new": True})
    assert json.loads(tmp_config.with_suffix(".json.bak").read_text()) == {"good": True}


def test_config_files_are_0600(tmp_config):
    from app.utils.config import save_config

    save_config({"v": 1})
    save_config({"v": 2})
    for p in (tmp_config, tmp_config.with_suffix(".json.bak")):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_update_config_mutates_in_place_and_ignores_return_value(tmp_config):
    from app.utils.config import load_config, update_config

    update_config(lambda c: c.setdefault("tags", {}).update({"s1": ["A"]}))
    assert load_config()["tags"] == {"s1": ["A"]}
    # A callback that happens to return something (here: the popped value or a
    # sub-dict) must never replace the whole config.
    update_config(lambda c: c.setdefault("tags", {}))
    update_config(lambda c: c.pop("missing", {"oops": 1}))
    out = update_config(lambda c: {"replaced": True})
    assert out["tags"] == {"s1": ["A"]}
    assert load_config() == {"tags": {"s1": ["A"]}}


def test_update_config_does_not_lose_concurrent_writes(tmp_config):
    """Many threads each adding one id — none may be lost."""
    from app.utils.config import load_config, update_config

    def add(i):
        update_config(lambda c: c.setdefault("deleted_ids", []).append(f"id{i}"))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(load_config()["deleted_ids"]) == sorted(f"id{i}" for i in range(25))


def test_update_config_exception_saves_nothing(tmp_config):
    from app.utils.config import load_config, save_config, update_config

    save_config({"v": 1})

    def bad(c):
        c["v"] = 2
        raise RuntimeError("nope")

    try:
        update_config(bad)
    except RuntimeError:
        pass
    assert load_config() == {"v": 1}


def test_update_config_blocks_other_writers_while_running(tmp_config):
    """A plain save_config from another thread waits for the update to finish."""
    from app.utils.config import load_config, save_config, update_config

    save_config({"n": 0})
    started = threading.Event()

    def slow(c):
        started.set()
        time.sleep(0.1)
        c["n"] = 1

    t = threading.Thread(target=update_config, args=(slow,))
    t.start()
    started.wait()
    update_config(lambda c: c.__setitem__("other", True))
    t.join()
    cfg = load_config()
    assert cfg == {"n": 1, "other": True}
