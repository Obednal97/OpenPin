"""Coaching runner: owner resolution, skip rules, staleness and the transcription hook.

Voice matching, the AI review and threads are always stubbed: no network, no embeddings.
"""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from app.coaching import runner, store
from tests.test_speaker_id import _SyncThread
from tests.test_transcribe_providers import RESULT, _transcribe, route_env  # noqa: F401 — fixture reuse

SID = "1715000000"

SEGMENTS = [
    {"start": 0.0, "end": 20.0, "text": "Hello, thanks for making time today.", "speaker": "Speaker 1"},
    {"start": 21.0, "end": 60.0, "text": "Of course. What did you want to cover?", "speaker": "Speaker 2"},
    {"start": 61.0, "end": 120.0, "text": "The launch plan and who owns what.", "speaker": "Speaker 1"},
    {"start": 121.0, "end": 180.0, "text": "Sounds good. I can take the website.", "speaker": "Speaker 2"},
]

REVIEW = {
    "scores": {"clarity": {"score": 80, "reason": "clear"}},
    "summary": "Clear and direct.",
    "next_time": ["Ask more questions."],
    "reactions": [],
    "moments": [],
    "interruptions_confirmed": [],
}


@pytest.fixture(autouse=True)
def clean_runner_state():
    """Runs in progress, queued reruns and run numbers are process-wide: start each test clean."""
    for state in (runner._running, runner._pending, runner._generation):
        state.clear()
    yield
    for state in (runner._running, runner._pending, runner._generation):
        state.clear()


@pytest.fixture
def env(tmp_path):
    """Isolated transcripts and coaching dirs, stubbed voice, review, audio lookup and threads."""
    trn = tmp_path / "transcripts"
    trn.mkdir()
    audio = tmp_path / f"{SID}.ogg"
    audio.write_bytes(b"OggS")
    config = {"coaching_auto": True, "summarization_provider": "anthropic"}
    stubs = {
        "config": config,
        "trn": trn,
        "audio": str(audio),
        "model_available": MagicMock(return_value=True),
        "match_owner": MagicMock(return_value={"labels": ["Speaker 1"], "confidence": 0.8, "source": "voice"}),
        "loudest_label": MagicMock(return_value="Speaker 2"),
        "add_exemplars": MagicMock(return_value=1),
        "review": MagicMock(return_value={"review": REVIEW, "provider": "anthropic", "errors": []}),
        "find_audio": MagicMock(return_value=str(audio)),
    }
    with (
        patch("app.coaching.store.COACHING_DIR", tmp_path / "coaching"),
        patch("app.coaching.runner.TRANSCRIPTS_DIR", trn),
        patch("app.coaching.runner.load_config", lambda: dict(config)),
        patch("app.coaching.runner.voice.model_available", stubs["model_available"]),
        patch("app.coaching.runner.voice.match_owner", stubs["match_owner"]),
        patch("app.coaching.runner.voice.loudest_label", stubs["loudest_label"]),
        patch("app.coaching.runner.voice.add_exemplars", stubs["add_exemplars"]),
        patch("app.coaching.runner.review_conversation", stubs["review"]),
        patch("app.coaching.runner._find_audio", stubs["find_audio"]),
        patch("app.coaching.runner.Thread", _SyncThread),
    ):
        yield stubs


def _write_transcript(trn, segments=SEGMENTS, duration=180.0, **extra):
    data = {
        "text": " ".join(s["text"] for s in segments),
        "duration": duration,
        "segments": segments,
        **extra,
    }
    (trn / f"{SID}.json").write_text(json.dumps(data))
    return data


def test_skips_without_speaker_labels(env):
    _write_transcript(env["trn"], [{**s, "speaker": None} for s in SEGMENTS])
    out = runner.run_coaching(SID, env["audio"], force=True)
    assert out["status"] == "skipped"
    assert out["status_reason"] == "no speaker labels"
    env["review"].assert_not_called()


def test_skips_too_short(env):
    _write_transcript(env["trn"], duration=90.0)
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "skipped"
    assert out["status_reason"] == "too short"
    env["review"].assert_not_called()


def test_force_bypasses_too_short(env):
    _write_transcript(env["trn"], duration=90.0)
    out = runner.run_coaching(SID, env["audio"], force=True)
    assert out["status"] == "done"


def test_only_owner_spoke_is_skipped(env):
    _write_transcript(env["trn"])
    env["match_owner"].return_value = {"labels": ["Speaker 1", "Speaker 2"], "confidence": 0.9, "source": "voice"}
    out = runner.run_coaching(SID, env["audio"], force=True)
    assert out["status"] == "skipped"
    assert out["status_reason"] == "only you spoke"
    env["review"].assert_not_called()


def test_one_speaker_is_skipped_without_voice_matching(env):
    _write_transcript(env["trn"], [{**s, "speaker": "Speaker 1"} for s in SEGMENTS])
    out = runner.run_coaching(SID, env["audio"], force=True)
    assert out["status"] == "skipped"
    assert out["status_reason"] == "only you spoke"
    env["match_owner"].assert_not_called()


def test_needs_owner_lists_candidates_with_first_lines(env):
    _write_transcript(env["trn"])
    env["match_owner"].return_value = {"labels": [], "confidence": 0.0, "source": "none", "reason": "no voice profile"}
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "needs_owner"
    assert out["status_reason"] == "no voice profile"
    assert out["candidates"] == [
        {"label": "Speaker 1", "first_line": "Hello, thanks for making time today.", "loud": False},
        {"label": "Speaker 2", "first_line": "Of course. What did you want to cover?", "loud": True},
    ]
    assert "metrics" not in out and "review" not in out
    env["review"].assert_not_called()


def test_needs_owner_without_model_never_checks_loudness(env):
    _write_transcript(env["trn"])
    env["model_available"].return_value = False
    env["match_owner"].return_value = {"labels": [], "source": "none", "reason": "voice model not installed"}
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "needs_owner"
    assert all(c["loud"] is False for c in out["candidates"])
    env["loudest_label"].assert_not_called()


def test_no_audio_skips_voice_matching(env):
    _write_transcript(env["trn"])
    env["find_audio"].return_value = None
    out = runner.run_coaching(SID, None)
    assert out["status"] == "needs_owner"
    assert out["status_reason"] == "no audio file"
    env["match_owner"].assert_not_called()
    env["loudest_label"].assert_not_called()


def test_done_writes_metrics_and_review(env):
    data = _write_transcript(env["trn"])
    store.update(SID, lambda d: d.update(words=[], report_stale=True))
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "done"
    assert out["review"] == REVIEW
    assert out["owner"] == {
        "labels": ["Speaker 1"],
        "source": "voice",
        "confidence": 0.8,
        "labelling": runner.labelling_fingerprint(SEGMENTS),
    }
    assert out["metrics"]["owner"]["talk_share"] is not None
    assert out["mixed_up"] is False
    assert "report_stale" not in out
    assert out["transcript_text_hash"] == hashlib.sha1(data["text"].encode()).hexdigest()
    assert store.load(SID)["status"] == "done"
    # Empty words (provider without word timing) are passed as None.
    segments, labels, metrics, _config = env["review"].call_args.args
    assert labels == ["Speaker 1"]
    assert segments == SEGMENTS


def test_existing_mixed_up_is_preserved(env):
    _write_transcript(env["trn"])
    store.update(SID, lambda d: d.update(mixed_up=True))
    assert runner.run_coaching(SID, env["audio"])["mixed_up"] is True


def test_words_used_only_when_non_empty(env):
    _write_transcript(env["trn"])
    words = [{"word": "hello", "start": 0.0, "end": 0.5, "speaker": "Speaker 1"}]
    store.update(SID, lambda d: d.update(words=words))
    with patch("app.coaching.runner.compute_metrics", wraps=runner.compute_metrics) as cm:
        runner.run_coaching(SID, env["audio"])
    assert cm.call_args.args[1] == words
    store.update(SID, lambda d: d.update(words=[]))
    with patch("app.coaching.runner.compute_metrics", wraps=runner.compute_metrics) as cm:
        runner.run_coaching(SID, env["audio"])
    assert cm.call_args.args[1] is None


def test_review_failure_keeps_metrics_status_failed(env):
    _write_transcript(env["trn"])
    env["review"].return_value = {"review": None, "provider": "none", "errors": ["Anthropic: 401", "Ollama: down"]}
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "failed"
    assert out["metrics"]["owner"]
    assert out["review"] is None
    assert "Anthropic: 401" in out["status_reason"] and "Ollama: down" in out["status_reason"]


def test_review_exception_is_a_failure(env):
    _write_transcript(env["trn"])
    env["review"].side_effect = RuntimeError("boom")
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "failed"
    assert "boom" in out["status_reason"]
    assert out["metrics"]["owner"]


def test_local_only_without_ollama_is_done_with_metrics_note(env):
    _write_transcript(env["trn"])
    env["config"]["summarization_provider"] = "local"
    env["review"].return_value = {"review": None, "provider": "none", "errors": ["Ollama: down"]}
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "done"
    assert out["review"] is None
    assert out["status_reason"] == "AI feedback needs Ollama; showing measured metrics only"
    assert out["metrics"]["owner"]


def test_result_dropped_if_transcript_changed_meanwhile(env):
    _write_transcript(env["trn"])
    store.update(SID, lambda d: d.update(status="needs_owner", status_reason="no voice profile"))

    def edit_then_review(*args, **kwargs):
        path = env["trn"] / f"{SID}.json"
        data = json.loads(path.read_text())
        data["text"] = "Edited while coaching ran."
        path.write_text(json.dumps(data))
        return {"review": REVIEW, "provider": "anthropic", "errors": []}

    env["review"].side_effect = edit_then_review
    out = runner.run_coaching(SID, env["audio"])
    assert "review" not in out and "metrics" not in out
    assert out["status"] == "needs_owner"  # the state before the run, not "running" forever
    assert store.load(SID) == out


def test_result_dropped_if_superseded_by_a_newer_run(env):
    _write_transcript(env["trn"])
    inner = {}

    def newer_run_meanwhile(*args, **kwargs):
        env["review"].side_effect = None
        env["match_owner"].return_value = {"labels": ["Speaker 2"], "confidence": 0.9, "source": "voice"}
        inner["out"] = runner.run_coaching(SID, env["audio"])
        return {"review": {"summary": "old"}, "provider": "anthropic", "errors": []}

    env["review"].side_effect = newer_run_meanwhile
    runner.run_coaching(SID, env["audio"])
    saved = store.load(SID)
    assert saved["owner"]["labels"] == ["Speaker 2"]
    assert saved["review"] == REVIEW


def test_user_owner_choice_persists_and_reruns(env):
    _write_transcript(env["trn"])
    env["match_owner"].return_value = {"labels": [], "source": "none", "reason": "no confident match"}
    assert runner.run_coaching(SID, env["audio"])["status"] == "needs_owner"

    out = runner.set_owner(SID, ["Speaker 2"])
    assert out["status"] == "done"
    assert out["owner"] == {
        "labels": ["Speaker 2"],
        "source": "user",
        "confidence": 1.0,
        "labelling": runner.labelling_fingerprint(SEGMENTS),
    }
    assert "candidates" not in out
    env["add_exemplars"].assert_called_once_with(env["audio"], SEGMENTS, ["Speaker 2"])
    assert env["review"].call_args.args[1] == ["Speaker 2"]

    # A later automatic run keeps the user's choice and doesn't ask the voice model.
    env["match_owner"].reset_mock()
    again = runner.run_coaching(SID, env["audio"])
    assert again["owner"]["source"] == "user"
    assert again["owner"]["labels"] == ["Speaker 2"]
    env["match_owner"].assert_not_called()


def test_set_owner_without_learning_or_model_adds_no_exemplars(env):
    _write_transcript(env["trn"])
    runner.set_owner(SID, ["Speaker 1"], learn=False)
    env["model_available"].return_value = False
    runner.set_owner(SID, ["Speaker 1"], learn=True)
    env["add_exemplars"].assert_not_called()


def test_set_owner_rejects_unknown_labels(env):
    _write_transcript(env["trn"])
    with pytest.raises(ValueError):
        runner.set_owner(SID, ["Speaker 9"])
    with pytest.raises(ValueError):
        runner.set_owner(SID, [])
    assert "owner" not in store.load(SID)


def test_set_owner_cannot_force_past_only_you_spoke(env):
    _write_transcript(env["trn"])
    out = runner.set_owner(SID, ["Speaker 1", "Speaker 2"])
    assert out["status"] == "skipped"
    assert out["status_reason"] == "only you spoke"


def test_auto_off_does_nothing(env):
    _write_transcript(env["trn"])
    env["config"]["coaching_auto"] = False
    runner.start_coaching_async(SID, env["audio"])
    assert store.load(SID) == {}
    env["review"].assert_not_called()
    # A forced run (the "Coach this recording" action) still works.
    runner.start_coaching_async(SID, env["audio"], force=True)
    assert store.load(SID)["status"] == "done"


def test_async_trigger_while_running_is_queued_not_concurrent(env):
    _write_transcript(env["trn"])
    started = []
    with patch("app.coaching.runner.Thread", lambda target, kwargs, daemon: MagicMock(start=lambda: started.append(1))):
        runner.start_coaching_async(SID, env["audio"])
        runner.start_coaching_async(SID, None)
        runner.start_coaching_async(SID, None, force=True)
    assert started == [1]  # one thread; the other triggers wait as a single rerun
    assert runner.is_running(SID)
    assert runner._pending[SID] == {"audio_path": None, "force": True}
    runner._running.clear()
    runner._pending.clear()
    runner.start_coaching_async(SID, env["audio"])  # _SyncThread: runs now
    assert store.load(SID)["status"] == "done"
    assert not runner.is_running(SID)


def test_status_is_running_during_a_run(env):
    _write_transcript(env["trn"])
    seen = []

    def check(*args, **kwargs):
        seen.append((store.load(SID)["status"], runner.is_running(SID)))
        return {"review": REVIEW, "provider": "anthropic", "errors": []}

    env["review"].side_effect = check
    runner.start_coaching_async(SID, env["audio"])
    runner.run_coaching(SID, env["audio"])
    assert seen == [("running", True), ("running", True)]
    assert not runner.is_running(SID)


def test_trigger_during_a_run_reruns_on_the_new_transcript(env):
    """Re-transcribed mid-run: the first result is dropped as stale and the queued rerun coaches the new text."""
    _write_transcript(env["trn"])
    new_text = "Re-transcribed while coaching ran."

    def retranscribe_then_review(*args, **kwargs):
        if env["review"].call_count == 1:
            path = env["trn"] / f"{SID}.json"
            data = json.loads(path.read_text())
            data["text"] = new_text
            path.write_text(json.dumps(data))
            runner.start_coaching_async(SID, env["audio"])  # the hook after re-transcription
        return {"review": REVIEW, "provider": "anthropic", "errors": []}

    env["review"].side_effect = retranscribe_then_review
    runner.start_coaching_async(SID, env["audio"])
    assert env["review"].call_count == 2
    saved = store.load(SID)
    assert saved["status"] == "done"
    assert saved["transcript_text_hash"] == store.text_hash(new_text)
    assert not runner.is_running(SID) and SID not in runner._pending


def test_trigger_during_set_owner_waits_then_reruns(env):
    _write_transcript(env["trn"])

    seen = []

    def hook_fires_meanwhile(*args, **kwargs):
        runner.start_coaching_async(SID, env["audio"])
        seen.append((env["review"].call_count, SID in runner._pending))
        return 1

    env["add_exemplars"].side_effect = hook_fires_meanwhile
    out = runner.set_owner(SID, ["Speaker 2"])
    assert seen == [(0, True)]  # queued, no concurrent duplicate run
    assert out["status"] == "done"
    assert env["review"].call_count == 2  # set_owner's run, then the queued one
    assert store.load(SID)["owner"]["source"] == "user"
    assert not runner.is_running(SID)


def test_current_marks_an_orphaned_running_status_failed(env):
    store.update(SID, lambda d: d.update(status="running", status_reason=""))
    out = runner.current(SID)
    assert out["status"] == "failed"
    assert out["status_reason"] == "Coaching was interrupted. Try again."
    assert store.load(SID)["status"] == "failed"


def test_current_leaves_a_live_run_alone(env):
    store.update(SID, lambda d: d.update(status="running"))
    runner._enter(SID)
    assert runner.current(SID)["status"] == "running"
    assert runner.current("1715000001") == {}
    store.update(SID, lambda d: d.update(status="done"))
    runner._running.clear()
    assert runner.current(SID)["status"] == "done"


def test_relabelled_segments_ignore_the_old_user_pick(env):
    _write_transcript(env["trn"])
    assert runner.set_owner(SID, ["Speaker 2"])["status"] == "done"

    # Re-diarized: same text, different labelling ("Speaker 2" is now someone else).
    swap = {"Speaker 1": "Speaker 2", "Speaker 2": "Speaker 1"}
    _write_transcript(env["trn"], [{**s, "speaker": swap[s["speaker"]]} for s in SEGMENTS])
    env["match_owner"].return_value = {"labels": [], "source": "none", "reason": "no confident match"}
    out = runner.run_coaching(SID, env["audio"])
    env["match_owner"].assert_called_once()
    assert out["status"] == "needs_owner"
    assert "owner" not in out


def test_voice_owner_reused_until_relabelled(env):
    _write_transcript(env["trn"])
    runner.run_coaching(SID, env["audio"])
    runner.run_coaching(SID, env["audio"])
    assert env["match_owner"].call_count == 1
    _write_transcript(env["trn"], [{**s, "start": s["start"] + 0.5} for s in SEGMENTS])
    runner.run_coaching(SID, env["audio"])
    assert env["match_owner"].call_count == 2


def test_missing_transcript_writes_nothing(env):
    out = runner.run_coaching(SID, env["audio"], force=True)
    assert out == {}
    assert not store.coaching_path(SID).exists()


def test_mark_stale_still_sees_a_metrics_only_report(env):
    _write_transcript(env["trn"])
    env["config"]["summarization_provider"] = "local"
    env["review"].return_value = {"review": None, "provider": "none", "errors": ["Ollama: down"]}
    runner.run_coaching(SID, env["audio"])
    store.mark_stale(SID)
    assert store.load(SID)["report_stale"] is True


# ---------------------------------------------------------------- the hook


def _hook_setup(rec, provider, diarize):
    from app.utils.config import save_config

    (rec / f"{SID}.ogg").write_bytes(b"OggS" + bytes(100))
    save_config({f"{provider}_api_key": "k", "transcription_provider": provider, "diarize": diarize})


def test_hook_called_after_background_diarize_and_both_no_bg_branches(route_env):  # noqa: F811
    import src.transcribe as T

    client, rec, _ = route_env
    audio = str(rec / f"{SID}.ogg")

    # 1. Cloud, no background job: called right after the webhook.
    _hook_setup(rec, "groq", diarize=False)
    with (
        patch("app.routes.transcribe.transcribe_groq", return_value=json.loads(json.dumps(RESULT))),
        patch("app.coaching.runner.start_coaching_async") as start,
    ):
        assert _transcribe(client, rec / f"{SID}.ogg").status_code == 200
    start.assert_called_once()
    assert start.call_args.args[0] == SID
    assert str(start.call_args.args[1]).endswith(f"{SID}.ogg")

    # 2. Cloud + background speaker labels: called once, when the job finishes.
    _hook_setup(rec, "groq", diarize=True)
    done = T.DiarizationResult(
        "done",
        segments=[{"start": 0, "end": 1, "speaker": "Speaker 1"}],
        engine="diarize",
    )
    with (
        patch("app.routes.transcribe.transcribe_groq", return_value=json.loads(json.dumps(RESULT))),
        patch("app.routes.transcribe.threading.Thread", _SyncThread),
        patch("src.transcribe.known_unavailable_reason", return_value=""),
        patch("src.transcribe.run_diarization", return_value=done),
        patch("app.coaching.runner.start_coaching_async") as start,
    ):
        body = _transcribe(client, rec / f"{SID}.ogg").get_json()
    assert body["diarizing"] is True
    start.assert_called_once_with(SID, audio)

    # 3. Local Whisper, no background job.
    from app.utils.config import save_config

    save_config({"transcription_provider": "local", "diarize": False})
    t = T.Transcript(text="hello there", language="en", duration=1.0)
    t.segments.append(T.TranscriptSegment(start=0, end=1, text="hello there"))
    with (
        patch("src.transcribe.transcribe", return_value=t),
        patch("app.coaching.runner.start_coaching_async") as start,
    ):
        assert _transcribe(client, rec / f"{SID}.ogg").status_code == 200
    start.assert_called_once_with(SID, audio)


def test_hook_never_raises(route_env):  # noqa: F811
    client, rec, trn = route_env
    _hook_setup(rec, "groq", diarize=False)
    with (
        patch("app.routes.transcribe.transcribe_groq", return_value=json.loads(json.dumps(RESULT))),
        patch("app.coaching.runner.start_coaching_async", side_effect=RuntimeError("boom")),
    ):
        r = _transcribe(client, rec / f"{SID}.ogg")
    assert r.status_code == 200
    assert (trn / f"{SID}.json").exists()


def test_find_audio_uses_the_recordings_lookup(tmp_path):
    rec = tmp_path / "recordings"
    rec.mkdir()
    (rec / f"{SID}.ogg").write_bytes(b"OggS")
    with patch("app.routes.recordings.get_all_recordings_dirs", return_value=[rec]):
        assert runner._find_audio(SID) == str(rec / f"{SID}.ogg")
        assert runner._find_audio("1715000001") is None
