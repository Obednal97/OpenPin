"""scripts/voice_eval.py prints the threshold table for a library of cached embeddings."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from app.people import store

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "voice_eval.py"
SEGMENTS = [{"start": 0.0, "end": 30.0, "text": "Hello.", "speaker": "Speaker 1"}]


def test_voice_eval_prints_table(tmp_path, capsys):
    trn = tmp_path / "transcripts"
    trn.mkdir()
    patch.object(store, "VOICES_PATH", tmp_path / "people" / "voices.json").start()
    cache = {}
    for i in range(24):
        sid = str(1715000000 + i * 1000)
        name, axis = ("Harry", 0) if i % 2 else ("Sam", 1)
        (trn / f"{sid}.json").write_text(json.dumps({"segments": SEGMENTS, "speaker_names": {"Speaker 1": name}}))
        vec = [0.0, 0.0, 0.0]
        vec[axis] = 1.0
        vec[2] = i / 100
        cache[sid] = {"Speaker 1": vec}
        data = store.load()
        pid = store.find_person(data, name) or store.create_person(data, name)
        store.mutate(
            lambda d, pid=pid, sid=sid, vec=vec: (
                d["people"]
                .setdefault(pid, {})
                .setdefault("samples", [])
                .append({"session_id": sid, "label": "Speaker 1", "vec": vec})
            )
        )
    spec = importlib.util.spec_from_file_location("voice_eval", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with (
        patch("app.state.speakers.TRANSCRIPTS_DIR", trn),
        patch("app.coaching.store.COACHING_DIR", tmp_path / "coaching"),
        patch("app.people.embeddings.cached", side_effect=lambda sid, seg: cache.get(sid)),
    ):
        assert mod.main([]) == 0
    patch.stopall()
    out = capsys.readouterr().out
    assert "threshold" in out and "wrong %" in out
    assert "0.550" in out and "0.700" in out
    assert "calibrate would pick" in out
