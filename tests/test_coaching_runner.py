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
        "detect": MagicMock(return_value={"status": "unavailable", "events": [], "reason": "stubbed"}),
        # Your voice at -20 dBFS; a laugh window measured after 100 s is loud (you), before it quiet.
        "measure_loudness": MagicMock(
            side_effect=lambda audio, segs, labels, windows: {
                "owner_db": -20.0,
                "windows": [-18.0 if a > 100 else -40.0 for a, _b in windows],
            }
        ),
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
        patch("app.coaching.runner.laughter.detect", stubs["detect"]),
        patch("app.coaching.runner.engagement.measure_loudness", stubs["measure_loudness"]),
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


OLLAMA_DOWN = "Ollama (local): not running on http://localhost:11434"
OLLAMA_NO_MODEL = "Ollama (local): no chat model pulled (try: ollama pull qwen3.5:9b)"


@pytest.mark.parametrize("preference", ["local", "ollama"])
@pytest.mark.parametrize("errors", [[OLLAMA_DOWN], [OLLAMA_NO_MODEL]])
def test_local_only_without_ollama_is_done_with_metrics_note(env, preference, errors):
    _write_transcript(env["trn"])
    env["config"]["summarization_provider"] = preference
    env["review"].return_value = {"review": None, "provider": "none", "errors": errors}
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "done"
    assert out["review"] is None
    assert out["status_reason"] == "AI feedback needs Ollama; showing measured metrics only"
    assert out["metrics"]["owner"]


@pytest.mark.parametrize(
    "errors",
    [
        ["Ollama (local): response was not a valid JSON object"],
        ["Ollama (local): Timed out — the service took too long to respond"],
        [],
    ],
)
def test_local_only_with_a_real_ollama_error_is_failed(env, errors):
    _write_transcript(env["trn"])
    env["config"]["summarization_provider"] = "local"
    env["review"].return_value = {"review": None, "provider": "none", "errors": errors}
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "failed"
    assert out["status_reason"].startswith("AI feedback failed: ")
    for e in errors:
        assert e in out["status_reason"]
    assert out["metrics"]["owner"]


def test_local_only_invalid_review_shape_is_failed(env):
    _write_transcript(env["trn"])
    env["config"]["summarization_provider"] = "local"
    env["review"].return_value = {"review": None, "provider": "ollama", "errors": ["invalid review shape: boom"]}
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "failed" and "invalid review shape" in out["status_reason"]


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


def test_set_owner_saves_owner_and_running_before_learning(env):
    _write_transcript(env["trn"])
    store.update(SID, lambda d: d.update(status="needs_owner", status_reason="no voice profile"))
    seen = []

    def learn(*args, **kwargs):
        side = store.load(SID)
        seen.append((side["status"], side["owner"]["labels"]))
        return 1

    env["add_exemplars"].side_effect = learn
    assert runner.set_owner(SID, ["Speaker 2"])["status"] == "done"
    assert seen == [("running", ["Speaker 2"])]


@pytest.mark.parametrize("learn", [True, False])
def test_current_says_running_between_the_owner_write_and_the_run(env, learn):
    """The session is registered before the owner + running write, so current() never calls it interrupted."""
    _write_transcript(env["trn"])
    store.update(SID, lambda d: d.update(status="needs_owner", status_reason="no voice profile"))
    seen = []
    real_save, real_run = runner._save_pick, runner._run_once

    def save_pick(session_id, owner):
        seen.append(("before write", runner.is_running(session_id)))
        return real_save(session_id, owner)

    def run_once(session_id, audio_path, force):
        seen.append(("before run", runner.current(session_id)["status"], store.load(session_id)["status"]))
        return real_run(session_id, audio_path, force)

    with patch.object(runner, "_save_pick", save_pick), patch.object(runner, "_run_once", run_once):
        out = runner.set_owner(SID, ["Speaker 2"], learn=learn)
    assert seen == [("before write", True), ("before run", "running", "running")]
    assert out["status"] == "done"
    assert runner.current(SID)["status"] == "done" and not runner.is_running(SID)


def test_start_set_owner_registers_before_writing_running(env):
    _write_transcript(env["trn"])
    seen = []
    real_save = runner._save_pick

    def save_pick(session_id, owner):
        seen.append(runner.is_running(session_id))
        return real_save(session_id, owner)

    with (
        patch.object(runner, "_save_pick", save_pick),
        patch("app.coaching.runner.Thread", lambda target, kwargs, daemon: MagicMock(start=lambda: None)),
    ):
        runner.start_set_owner(SID, ["Speaker 2"])
    assert seen == [True]
    assert runner.current(SID)["status"] == "running"


def test_start_set_owner_returns_running_then_coaches_in_background(env):
    _write_transcript(env["trn"])
    store.update(SID, lambda d: d.update(status="needs_owner", status_reason="no voice profile"))
    started = []
    with patch(
        "app.coaching.runner.Thread", lambda target, kwargs, daemon: MagicMock(start=lambda: started.append(kwargs))
    ):
        out = runner.start_set_owner(SID, ["Speaker 2"], learn=False)
    assert out["status"] == "running" and out["status_reason"] == ""
    assert out["owner"]["labels"] == ["Speaker 2"] and out["owner"]["source"] == "user"
    assert runner.is_running(SID)  # so current() doesn't call it interrupted
    assert runner.current(SID)["status"] == "running"
    runner._set_owner_in_background(**started[0])
    assert store.load(SID)["status"] == "done"
    assert not runner.is_running(SID)


def test_start_set_owner_rejects_bad_picks_without_writing(env):
    _write_transcript(env["trn"])
    with pytest.raises(ValueError):
        runner.start_set_owner(SID, ["Speaker 9"])
    assert store.load(SID) == {}
    assert not runner.is_running(SID)


def test_words_file_feeds_the_metrics(env):
    _write_transcript(env["trn"])
    words = [{"word": "hello", "start": 0.0, "end": 0.5, "speaker": "Speaker 1"}]
    store.save_words(SID, words, "h")
    with patch("app.coaching.runner.compute_metrics", wraps=runner.compute_metrics) as cm:
        out = runner.run_coaching(SID, env["audio"])
    assert cm.call_args.args[1] == words
    assert "words" not in out and "words" not in store.load(SID)


def test_transcript_deleted_mid_run_writes_nothing(env):
    _write_transcript(env["trn"])

    def delete_then_review(*args, **kwargs):
        (env["trn"] / f"{SID}.json").unlink()
        for path in store.session_files(SID):
            path.unlink(missing_ok=True)
        return {"review": REVIEW, "provider": "anthropic", "errors": []}

    env["review"].side_effect = delete_then_review
    runner.run_coaching(SID, env["audio"])
    assert not store.coaching_path(SID).exists()


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
    env["review"].return_value = {"review": None, "provider": "none", "errors": [OLLAMA_DOWN]}
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


# ---------------------------------------------------------------- AI review off


@pytest.mark.parametrize("provider", ["anthropic", "local"])
def test_ai_review_off_never_asks_a_provider_and_ends_done(env, provider):
    _write_transcript(env["trn"])
    env["config"].update(coaching_ai_review=False, summarization_provider=provider)
    out = runner.run_coaching(SID, env["audio"])
    env["review"].assert_not_called()
    assert out["status"] == "done"
    assert out["status_reason"] == runner.REVIEW_OFF
    assert out["review"] is None
    assert out["metrics"]["owner"]["talk_share"] is not None


def test_ai_review_off_applies_to_manual_runs_and_owner_picks(env):
    _write_transcript(env["trn"])
    env["config"].update(coaching_ai_review=False, coaching_auto=False)
    assert runner.run_coaching(SID, env["audio"], force=True)["status"] == "done"
    out = runner.set_owner(SID, ["Speaker 2"], learn=False)
    assert out["status"] == "done" and out["review"] is None
    env["review"].assert_not_called()


def test_ai_review_back_on_reviews_again(env):
    _write_transcript(env["trn"])
    env["config"]["coaching_ai_review"] = False
    runner.run_coaching(SID, env["audio"])
    env["config"]["coaching_ai_review"] = True
    out = runner.run_coaching(SID, env["audio"])
    assert out["review"] == REVIEW and out["status_reason"] == ""


# ---------------------------------------------------------------- "Don't coach this recording"


WORDS = [{"word": "um", "start": 0.0, "end": 0.3, "speaker": "Speaker 1"}]


def _done_and_words(env):
    _write_transcript(env["trn"])
    runner.run_coaching(SID, env["audio"])
    store.save_words(SID, WORDS, "h")
    store.update(SID, lambda d: d.update(mixed_up=True, candidates=[{"label": "Speaker 1"}]))


def test_exclude_deletes_the_report_and_keeps_the_words(env):
    _done_and_words(env)
    out = runner.set_excluded(SID, True)
    assert out["excluded"] is True and out["status"] == "excluded"
    for key in ("metrics", "review", "owner", "candidates", "report_stale", "mixed_up", "created_at"):
        assert key not in out, key
    assert store.load(SID) == out
    assert store.load_words(SID) == WORDS


def test_excluded_recording_is_never_coached(env):
    _done_and_words(env)
    runner.set_excluded(SID, True)
    env["review"].reset_mock()
    env["match_owner"].reset_mock()
    runner.start_coaching_async(SID, env["audio"])
    runner.start_coaching_async(SID, env["audio"], force=True)
    out = runner.run_coaching(SID, env["audio"], force=True)
    assert out["status"] == "excluded" and "metrics" not in out
    env["review"].assert_not_called()
    env["match_owner"].assert_not_called()
    assert store.load(SID)["status"] == "excluded"


def test_retranscribing_keeps_the_exclusion(env):
    _done_and_words(env)
    runner.set_excluded(SID, True)
    env["review"].reset_mock()
    # What a re-transcribe does: new words, a stale mark, then the coaching hook
    _write_transcript(env["trn"], [{**s, "text": s["text"] + " Again."} for s in SEGMENTS])
    store.save_words(SID, [{**WORDS[0], "word": "uh"}], "h2")
    store.mark_stale(SID)
    runner.start_coaching_async(SID, env["audio"])
    saved = store.load(SID)
    assert saved["excluded"] is True and saved["status"] == "excluded"
    assert "report_stale" not in saved
    env["review"].assert_not_called()


def test_excluding_mid_run_drops_its_result(env):
    _write_transcript(env["trn"])

    def exclude_meanwhile(*args, **kwargs):
        runner.set_excluded(SID, True)
        return {"review": REVIEW, "provider": "anthropic", "errors": []}

    env["review"].side_effect = exclude_meanwhile
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "excluded"
    assert "review" not in out and "metrics" not in out and "owner" not in out


def test_an_exclusion_saved_mid_run_wins_even_without_a_newer_run_number(env):
    # The exclusion's run number can land before this run took its own; the saved flag still wins.
    _write_transcript(env["trn"])

    def flag_meanwhile(*args, **kwargs):
        store.update(SID, lambda d: d.update(excluded=True, status="excluded", status_reason=""))
        return {"review": REVIEW, "provider": "anthropic", "errors": []}

    env["review"].side_effect = flag_meanwhile
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "excluded" and "review" not in out


def test_excluding_drops_a_queued_rerun(env):
    _write_transcript(env["trn"])
    runner._running[SID] = 1
    runner.start_coaching_async(SID, env["audio"], force=True)
    assert SID in runner._pending
    runner.set_excluded(SID, True)
    assert SID not in runner._pending


def test_excluded_recording_never_teaches_the_voice(env):
    _write_transcript(env["trn"])
    runner.set_excluded(SID, True)
    with pytest.raises(ValueError):
        runner.set_owner(SID, ["Speaker 1"])
    with pytest.raises(ValueError):
        runner.start_set_owner(SID, ["Speaker 1"])
    env["add_exemplars"].assert_not_called()
    assert "owner" not in store.load(SID)
    assert not runner.is_running(SID)


def test_excluded_after_the_pick_never_teaches_the_voice(env):
    _write_transcript(env["trn"])
    resolve = runner._resolve_audio

    def exclude_then_resolve(sid, path):
        runner.set_excluded(SID, True)
        return resolve(sid, path)

    with patch("app.coaching.runner._resolve_audio", exclude_then_resolve):
        out = runner.set_owner(SID, ["Speaker 1"])
    env["add_exemplars"].assert_not_called()
    assert out["status"] == "excluded"


def test_undoing_the_exclusion_lets_a_forced_run_coach_again(env):
    _done_and_words(env)
    runner.set_excluded(SID, True)
    cleared = runner.set_excluded(SID, False)
    assert "excluded" not in cleared and "status" not in cleared
    out = runner.run_coaching(SID, env["audio"], force=True)
    assert out["status"] == "done" and out["review"] == REVIEW


def test_undoing_without_an_exclusion_writes_nothing(env):
    assert runner.set_excluded(SID, False) == {}
    assert not store.coaching_path(SID).exists()


# ---------------------------------------------------------------- engagement

# Speaker 1 (you) ends turns at 20.0 and 120.0.
LAUGH_EVENTS = [
    {"start": 20.5, "end": 22.0, "score": 0.6, "loudness_db": -40.0},  # someone else laughing
    {"start": 120.5, "end": 122.0, "score": 0.6, "loudness_db": -18.0},  # you, loudest on your own mic
]


def _laughs(env, events=LAUGH_EVENTS):
    env["detect"].return_value = {"status": "done", "events": events, "reason": ""}


def test_engagement_block_is_stored_and_its_parts_copied_into_the_metrics(env):
    _write_transcript(env["trn"])
    _laughs(env)
    out = runner.run_coaching(SID, env["audio"])
    eng = out["engagement"]
    env["detect"].assert_called_once_with(env["audio"])
    assert eng["laughter_status"] == "done"
    assert eng["laughs"] == [{"t": 20.5, "line_start": 0.0, "line_text": SEGMENTS[0]["text"]}]
    assert eng["laughs_count"] == 1
    assert eng["laughs_per_hour"] == 20.0
    assert eng["score"] is not None
    owner = out["metrics"]["owner"]
    assert owner["laughs_per_hour"] == 20.0
    assert owner["follow_up_share"] == eng["follow_up_share"]
    assert owner["built_on_share"] is None  # REVIEW has no reactions
    # One measurement: your lines plus each candidate's window, starting where your line ends.
    env["measure_loudness"].assert_called_once()
    assert env["measure_loudness"].call_args.args[3] == [(20.0, 22.5), (120.0, 122.5)]
    # The counted laugh went to the review prompt, before the review ran.
    assert env["review"].call_args.kwargs["laughs"] == eng["laughs"]


def test_built_on_uses_the_review(env):
    _write_transcript(env["trn"])
    reacted = {
        **REVIEW,
        "reactions": [
            {"t": 0.0, "owner_point": "p", "response": r, "evidence": "e"} for r in ("engaged", "short", "follow_up")
        ],
    }
    env["review"].return_value = {"review": reacted, "provider": "anthropic", "errors": []}
    out = runner.run_coaching(SID, env["audio"])
    assert out["engagement"]["built_on"] == {"count": 2, "of": 3}
    assert out["metrics"]["owner"]["built_on_share"] == round(2 / 3, 4)


def test_ai_review_off_still_measures_engagement(env):
    _write_transcript(env["trn"])
    _laughs(env)
    env["config"]["coaching_ai_review"] = False
    out = runner.run_coaching(SID, env["audio"])
    env["review"].assert_not_called()
    assert out["engagement"]["laughs_count"] == 1
    assert out["engagement"]["built_on"] is None
    assert out["engagement"]["score"] is not None


def test_review_failure_keeps_the_engagement_block(env):
    _write_transcript(env["trn"])
    _laughs(env)
    env["review"].return_value = {"review": None, "provider": "none", "errors": ["Anthropic: 401"]}
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "failed"
    assert out["engagement"]["laughs_count"] == 1


def test_laughter_unavailable_keeps_its_reason_and_coaching_carries_on(env):
    _write_transcript(env["trn"])
    env["detect"].return_value = {"status": "unavailable", "events": [], "reason": "no model"}
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "done"
    assert out["engagement"]["laughter_status"] == "unavailable"
    assert out["engagement"]["laughter_reason"] == "no model"
    assert out["metrics"]["owner"]["laughs_per_hour"] is None
    env["measure_loudness"].assert_not_called()


def test_laughter_raising_never_breaks_coaching(env):
    _write_transcript(env["trn"])
    env["detect"].side_effect = RuntimeError("onnx exploded")
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "done"
    assert out["review"] == REVIEW
    assert out["engagement"]["laughter_status"] == "unavailable"
    assert out["engagement"]["laughter_reason"]


def test_malformed_detect_result_is_unavailable(env):
    _write_transcript(env["trn"])
    env["detect"].return_value = "nonsense"
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "done"
    assert out["engagement"]["laughter_status"] == "unavailable"


def test_no_laughs_found_needs_no_voice_level(env):
    _write_transcript(env["trn"])
    _laughs(env, [])
    out = runner.run_coaching(SID, env["audio"])
    assert out["engagement"]["laughs_count"] == 0
    env["measure_loudness"].assert_not_called()


def test_over_three_hours_skips_laughter_without_converting(env):
    long = [*SEGMENTS, {"start": 10800.0, "end": 10802.0, "text": "Bye then.", "speaker": "Speaker 2"}]
    _write_transcript(env["trn"], long, duration=10801.0)
    out = runner.run_coaching(SID, env["audio"])
    env["detect"].assert_not_called()
    assert out["engagement"]["laughter_status"] == "unavailable"
    assert out["engagement"]["laughter_reason"] == "Too long to check for laughter"


def test_no_audio_on_this_mac_skips_laughter(env):
    _write_transcript(env["trn"])
    env["find_audio"].return_value = None
    out = runner.set_owner(SID, ["Speaker 1"], learn=False)
    assert out["status"] == "done"
    env["detect"].assert_not_called()
    assert out["engagement"]["laughter_status"] == "unavailable"
    assert out["engagement"]["laughter_reason"]


def test_excluding_drops_the_engagement_block(env):
    _write_transcript(env["trn"])
    assert "engagement" in runner.run_coaching(SID, env["audio"])
    assert "engagement" not in runner.set_excluded(SID, True)


def test_needs_owner_drops_an_old_engagement_block(env):
    _write_transcript(env["trn"])
    assert "engagement" in runner.run_coaching(SID, env["audio"])
    env["match_owner"].return_value = {"labels": [], "confidence": 0.2, "reason": "no confident match"}
    relabelled = [{**s, "start": s["start"] + 0.5} for s in SEGMENTS]
    _write_transcript(env["trn"], relabelled)
    out = runner.run_coaching(SID, env["audio"])
    assert out["status"] == "needs_owner"
    assert "engagement" not in out


def test_laughs_far_from_your_lines_need_no_measuring(env):
    _write_transcript(env["trn"])
    _laughs(env, [{"start": 40.0, "end": 41.0, "score": 0.6, "loudness_db": -40.0}])
    out = runner.run_coaching(SID, env["audio"])
    assert out["engagement"]["laughter_status"] == "done" and out["engagement"]["laughs_count"] == 0
    env["measure_loudness"].assert_not_called()
