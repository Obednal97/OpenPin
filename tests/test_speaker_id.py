"""Local speaker identification: tier gating, Hugging Face token handling, and the
background job's status (no model downloads: the diarize/pyannote tiers and pip are mocked)."""

import json
import stat
import sys
import types
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest
import requests

import src.transcribe as T
from tests.test_transcribe_providers import RESULT, _transcribe, route_env  # noqa: F401 — fixture reuse

RAW = [
    {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_03"},
    {"start": 5.0, "end": 9.0, "speaker": "SPEAKER_00"},
    {"start": 9.0, "end": 12.0, "speaker": "SPEAKER_03"},
]


class _FakeResult:
    def __init__(self, segments):
        self._segments = segments

    def to_list(self):
        return [dict(s) for s in self._segments]


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    """Fresh install-failure memo and a temp .hf_token for every test; never shell out to ffmpeg."""
    with (
        patch.object(T, "HF_TOKEN_PATH", tmp_path / ".hf_token"),
        patch.object(T, "_diarize_install_failed_at", None),
        patch.object(T, "_to_wav16k", return_value=None),
    ):
        yield tmp_path / ".hf_token"


def _fake_diarize_lib(segments=RAW, error=None):
    calls = []

    def fn(path):
        calls.append(path)
        if error:
            raise error
        return _FakeResult(segments)

    fn.calls = calls
    return fn


# ---------------------------------------------------------------------------
# Tier gating
# ---------------------------------------------------------------------------


def test_diarize_tier_runs_without_a_token():
    fn = _fake_diarize_lib()
    with patch.object(T, "_load_diarize_lib", return_value=fn), patch.object(T, "_diarize_audio_pyannote") as pya:
        segments, engine = T.diarize_audio("/x/a.ogg", None)
    assert engine == "diarize"
    assert fn.calls == ["/x/a.ogg"]
    assert [s["speaker"] for s in segments] == ["Speaker 1", "Speaker 2", "Speaker 1"]
    pya.assert_not_called()


def test_run_diarization_reads_no_token_and_reports_done():
    with patch.object(T, "_load_diarize_lib", return_value=_fake_diarize_lib()):
        result = T.run_diarization("/x/a.ogg")
    assert result.status == "done"
    assert result.engine == "diarize"
    assert result.message == ""
    assert len(result.segments) == 3


def test_missing_package_installs_pinned_extra(monkeypatch):
    """`from diarize import diarize` fails -> ensure_installed(..., extra="diarize"), then import again."""
    fake_mod = types.ModuleType("diarize")
    fake_mod.diarize = _fake_diarize_lib()
    monkeypatch.setitem(sys.modules, "diarize", None)  # ImportError on import

    def fake_install(package, pip_name=None, required=False, *, extra=None):
        assert (package, extra) == ("diarize", "diarize")
        sys.modules["diarize"] = fake_mod
        return True

    with (
        patch("app.utils.system.ensure_installed", side_effect=fake_install) as inst,
        patch.object(T, "_install_soundfile_audio_loader") as loader,
    ):
        assert T._load_diarize_lib() is fake_mod.diarize
    inst.assert_called_once()
    loader.assert_called_once()


def test_failed_install_is_reported_and_not_retried_immediately(monkeypatch):
    monkeypatch.setitem(sys.modules, "diarize", None)
    with patch("app.utils.system.ensure_installed", return_value=False) as inst:
        first = T.run_diarization("/x/a.ogg")
        second = T.run_diarization("/x/b.ogg")
    assert first.status == second.status == "failed"
    assert "couldn't be installed" in first.message
    assert first.message.startswith("Speaker labels skipped:")
    assert inst.call_count == 1  # memoised: a batch doesn't run pip per file
    with patch("importlib.util.find_spec", return_value=None):
        assert "couldn't be installed" in T.known_unavailable_reason()


def test_known_unavailable_reason_none_before_any_failure():
    assert T.known_unavailable_reason() is None


def test_failed_install_retried_after_the_backoff(monkeypatch):
    monkeypatch.setitem(sys.modules, "diarize", None)
    with patch("app.utils.system.ensure_installed", return_value=False) as inst:
        T.run_diarization("/x/a.ogg")
        T._diarize_install_failed_at -= T._DIARIZE_INSTALL_RETRY_SECONDS + 1
        T.run_diarization("/x/a.ogg")
    assert inst.call_count == 2


def test_pyannote_fallback_needs_token_and_package(_isolated):
    broken = _fake_diarize_lib(error=RuntimeError("onnx exploded"))
    with (
        patch.object(T, "_load_diarize_lib", return_value=broken),
        patch.object(T, "_pyannote_installed", return_value=True),
        patch.object(T, "_diarize_audio_pyannote", return_value=[dict(RAW[0])]) as pya,
    ):
        # No token: pyannote isn't tried.
        result = T.run_diarization("/x/a.ogg")
        assert result.status == "failed"
        assert "onnx exploded" in result.message
        pya.assert_not_called()

        T.save_hf_token("hf_abc123")
        result = T.run_diarization("/x/a.ogg")
    assert result.status == "done"
    assert result.engine == "pyannote"
    pya.assert_called_once_with("/x/a.ogg", "hf_abc123")


def test_pyannote_not_installed_is_skipped(_isolated):
    T.save_hf_token("hf_abc123")
    with (
        patch.object(T, "_load_diarize_lib", return_value=None),
        patch.object(T, "_pyannote_installed", return_value=False),
        patch.object(T, "_diarize_audio_pyannote") as pya,
    ):
        result = T.run_diarization("/x/a.ogg")
    assert result.status == "failed"
    pya.assert_not_called()


def test_pyannote_gated_error_names_the_models(_isolated):
    T.save_hf_token("hf_abc123")
    with (
        patch.object(T, "_load_diarize_lib", return_value=None),
        patch.object(T, "_pyannote_installed", return_value=True),
        patch.object(T, "_diarize_audio_pyannote", side_effect=RuntimeError("403 Client Error: gated repo")),
    ):
        result = T.run_diarization("/x/a.ogg")
    assert result.status == "failed"
    assert "pyannote/speaker-diarization-3.1" in result.message
    assert "pyannote/segmentation-3.0" in result.message


def test_no_speech_reports_none():
    with patch.object(T, "_load_diarize_lib", return_value=_fake_diarize_lib(segments=[])):
        result = T.run_diarization("/x/a.ogg")
    assert result.status == "none"
    assert result.segments == []


def test_soundfile_loader_patch(monkeypatch):
    """torchaudio.load gets a soundfile path (torchcodec isn't in the pinned extra)."""

    def original_load(uri, *a, **k):
        raise RuntimeError("torchaudio requires torchcodec")

    fake_ta = types.ModuleType("torchaudio")
    fake_ta.load = original_load
    data = MagicMock()
    data.T.copy.return_value = "CHANNELS_FIRST"
    fake_sf = types.ModuleType("soundfile")
    fake_sf.read = MagicMock(return_value=(data, 16000))
    fake_torch = types.ModuleType("torch")
    fake_torch.from_numpy = lambda x: ("tensor", x)
    monkeypatch.setitem(sys.modules, "torchaudio", fake_ta)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    T._install_soundfile_audio_loader()
    T._install_soundfile_audio_loader()  # idempotent
    assert fake_ta.load("/x/a.wav") == (("tensor", "CHANNELS_FIRST"), 16000)
    fake_sf.read.assert_called_once_with("/x/a.wav", start=0, frames=-1, dtype="float32", always_2d=True)

    fake_sf.read.side_effect = RuntimeError("unsupported format")
    with pytest.raises(RuntimeError, match="torchcodec"):
        fake_ta.load("/x/a.m4a")  # falls back to the original loader


def _wave_backed_soundfile():
    """A stand-in for soundfile.read (not installed in the test venv), with its
    start/frames/dtype/always_2d semantics, so the loader runs end to end."""
    import wave

    import numpy as np

    def read(file, start=0, frames=-1, dtype="float64", always_2d=False):
        with wave.open(str(file), "rb") as w:
            channels, rate = w.getnchannels(), w.getframerate()
            w.setpos(start)
            raw = w.readframes(w.getnframes() - start if frames < 0 else frames)
        data = np.frombuffer(raw, dtype="<i2").reshape(-1, channels).astype(dtype) / 32768.0
        return data, rate

    mod = types.ModuleType("soundfile")
    mod.read = read
    return mod


def test_soundfile_loader_honours_offset_frames_and_layout(monkeypatch, tmp_path):
    """frame_offset/num_frames select the same frames torchaudio.load would."""
    import wave

    import numpy as np

    torch = pytest.importorskip("torch")
    try:
        import soundfile  # noqa: F401 — the real reader when installed
    except ImportError:
        monkeypatch.setitem(sys.modules, "soundfile", _wave_backed_soundfile())

    # 16 kHz stereo, 10 frames: left = 0..9, right = -(0..9), in int16 steps.
    left = np.arange(10, dtype="<i2") * 1000
    pcm = np.stack([left, -left], axis=1)
    wav = tmp_path / "tone.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    expected = pcm.astype("float32") / 32768.0  # (frames, channels)

    def original_load(uri, *a, **k):
        raise AssertionError("original loader must not be used")

    fake_ta = types.ModuleType("torchaudio")
    fake_ta.load = original_load
    monkeypatch.setitem(sys.modules, "torchaudio", fake_ta)
    T._install_soundfile_audio_loader()

    full, sr = fake_ta.load(wav)
    assert sr == 16000
    assert full.dtype == torch.float32
    assert tuple(full.shape) == (2, 10)  # channels first by default
    np.testing.assert_allclose(full.numpy(), expected.T, atol=1e-6)

    part, _ = fake_ta.load(str(wav), frame_offset=3, num_frames=4)
    assert tuple(part.shape) == (2, 4)
    np.testing.assert_allclose(part.numpy(), expected[3:7].T, atol=1e-6)

    # Positional form, num_frames <= 0 means "to the end", channels last.
    tail, _ = fake_ta.load(str(wav), 6, -1, True, False)
    assert tuple(tail.shape) == (4, 2)
    assert tail.dtype == torch.float32
    np.testing.assert_allclose(tail.numpy(), expected[6:], atol=1e-6)

    zero, _ = fake_ta.load(str(wav), num_frames=0)
    assert tuple(zero.shape) == (2, 10)


def test_transcribe_merges_speakers_or_says_why():
    whisper_t = T.Transcript(
        text="Hello. Hi there.",
        segments=[T.TranscriptSegment(0, 5, "Hello."), T.TranscriptSegment(5, 9, "Hi there.")],
    )
    with (
        patch.object(T, "transcribe_with_whisper", return_value=whisper_t),
        patch.object(T, "_load_diarize_lib", return_value=_fake_diarize_lib()),
    ):
        t = T.transcribe("/x/a.ogg", diarize=True)
    assert t.diarization_status == "done"
    assert [s.speaker for s in t.segments] == ["Speaker 1", "Speaker 2"]

    whisper_t = T.Transcript(text="Hello.", segments=[T.TranscriptSegment(0, 5, "Hello.")])
    with (
        patch.object(T, "transcribe_with_whisper", return_value=whisper_t),
        patch.object(T, "_load_diarize_lib", return_value=None),
    ):
        t = T.transcribe("/x/a.ogg", diarize=True)
    assert t.diarization_status == "failed"
    assert t.diarization_message
    assert t.segments[0].speaker == ""


# ---------------------------------------------------------------------------
# Hugging Face token file
# ---------------------------------------------------------------------------


def test_token_file_is_0600_and_round_trips(_isolated):
    assert T.get_hf_token() is None
    T.save_hf_token("  hf_abc123\n")
    assert T.get_hf_token() == "hf_abc123"
    assert stat.S_IMODE(_isolated.stat().st_mode) == 0o600
    _isolated.chmod(0o644)
    T.save_hf_token("hf_new")
    assert stat.S_IMODE(_isolated.stat().st_mode) == 0o600
    T.delete_hf_token()
    T.delete_hf_token()  # already gone: no error
    assert T.get_hf_token() is None


def _whoami(status):
    r = MagicMock()
    r.status_code = status
    return r


def test_settings_save_valid_token(flask_client, _isolated):
    token = "hf_ValidToken0123456789"
    with patch("app.routes.settings.http_requests.get", return_value=_whoami(200)) as get:
        r = flask_client.post("/settings", json={"hf_token": f" {token} "})
    assert r.status_code == 200
    assert token not in r.get_data(as_text=True)
    url = get.call_args.args[0]
    assert url == "https://huggingface.co/api/whoami-v2"
    assert get.call_args.kwargs["headers"] == {"Authorization": f"Bearer {token}"}
    assert _isolated.read_text() == token
    assert stat.S_IMODE(_isolated.stat().st_mode) == 0o600

    data = flask_client.get("/settings").get_json()
    assert data["hf_token_set"] is True
    assert token not in json.dumps(data)
    # Never copied into the config file either.
    from app.utils.config import load_config

    assert token not in json.dumps(load_config())


def test_settings_rejected_token_not_saved(flask_client, _isolated):
    with patch("app.routes.settings.http_requests.get", return_value=_whoami(401)):
        r = flask_client.post("/settings", json={"hf_token": "hf_bad"})
    assert r.status_code == 400
    assert "rejected" in r.get_json()["error"]
    assert not _isolated.exists()
    assert flask_client.get("/settings").get_json()["hf_token_set"] is False


@pytest.mark.parametrize("bad", ["", "not-a-token", "hf_has space", "hf_" + "x" * 300])
def test_settings_malformed_token_no_network(flask_client, _isolated, bad):
    with patch("app.routes.settings.http_requests.get") as get:
        r = flask_client.post("/settings", json={"hf_token": bad})
    assert r.status_code == 400
    get.assert_not_called()
    assert not _isolated.exists()


def test_settings_token_offline(flask_client, _isolated):
    with patch("app.routes.settings.http_requests.get", side_effect=requests.ConnectionError("down")):
        r = flask_client.post("/settings", json={"hf_token": "hf_abc"})
    assert r.status_code == 400
    assert "internet" in r.get_json()["error"]
    assert not _isolated.exists()


def test_settings_remove_token(flask_client, _isolated):
    T.save_hf_token("hf_abc")
    r = flask_client.post("/settings", json={"remove_hf_token": True})
    assert r.status_code == 200
    assert not _isolated.exists()
    assert flask_client.get("/settings").get_json()["hf_token_set"] is False


def test_diarization_provider_is_retired(flask_client):
    from app.utils.config import load_config, save_config

    save_config({"diarization_provider": "local", "diarize": True})
    flask_client.post("/settings", json={"language": "fr", "diarization_provider": "pyannote"})
    config = load_config()
    assert "diarization_provider" not in config
    assert config["diarize"] is True
    assert "diarization_provider" not in flask_client.get("/settings").get_json()


# ---------------------------------------------------------------------------
# /transcribe: cloud transcript + background local diarization
# ---------------------------------------------------------------------------


class _SyncThread:
    """Runs the target when started, so the background job finishes inside the request."""

    def __init__(self, target, kwargs=None, daemon=None):
        self._target, self._kwargs = target, kwargs or {}

    def start(self):
        self._target(**self._kwargs)


GROQ_RESULT = {
    "text": "Hello. Hi there.",
    "segments": [{"start": 0, "end": 5, "text": "Hello."}, {"start": 5, "end": 9, "text": "Hi there."}],
    "duration": 9,
}


def _groq_setup(rec):
    from app.utils.config import save_config

    (rec / "1715000000.ogg").write_bytes(b"OggS" + bytes(100))
    save_config({"groq_api_key": "k", "transcription_provider": "groq", "diarize": True})


def test_cloud_transcript_gets_local_speakers(route_env):  # noqa: F811
    client, rec, trn = route_env
    _groq_setup(rec)
    done = T.DiarizationResult("done", segments=[dict(s) for s in RAW[:2]], engine="diarize")
    with (
        patch("app.routes.transcribe.transcribe_groq", return_value=json.loads(json.dumps(GROQ_RESULT))),
        patch("app.routes.transcribe.threading.Thread", _SyncThread),
        patch("src.transcribe.run_diarization", return_value=done),
    ):
        r = _transcribe(client, rec / "1715000000.ogg")
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["diarizing"] is True
    saved = json.loads((trn / "1715000000.json").read_text())
    assert saved["diarization_status"] == "done"
    assert saved["provider"] == "groq+diarize"
    assert saved["speakers"] == ["Speaker 1", "Speaker 2"]
    assert [s["speaker"] for s in saved["segments"]] == ["Speaker 1", "Speaker 2"]

    with patch("app.routes.recordings.TRANSCRIPTS_DIR", trn):
        listed = client.get("/transcripts").get_json()[0]
    assert listed["diarization_status"] == "done"
    assert sorted(listed["speakers"]) == ["Speaker 1", "Speaker 2"]


def test_cloud_transcript_background_failure_sets_status(route_env):  # noqa: F811
    client, rec, trn = route_env
    _groq_setup(rec)
    failed = T.DiarizationResult("failed", message="Speaker labels skipped: speaker detection failed (boom)")
    with (
        patch("app.routes.transcribe.transcribe_groq", return_value=json.loads(json.dumps(GROQ_RESULT))),
        patch("app.routes.transcribe.threading.Thread", _SyncThread),
        patch("src.transcribe.run_diarization", return_value=failed),
        patch("app.routes.transcribe.fire_webhooks") as hooks,
    ):
        _transcribe(client, rec / "1715000000.ogg")
    saved = json.loads((trn / "1715000000.json").read_text())
    assert saved["diarization_status"] == "failed"
    assert "boom" in saved["diarization_message"]
    assert saved["provider"] == "groq"
    assert all(not s.get("speaker") for s in saved["segments"])
    with patch("app.routes.recordings.TRANSCRIPTS_DIR", trn):
        listed = client.get("/transcripts").get_json()[0]
    assert listed["diarization_status"] == "failed"
    assert "boom" in listed["diarization_message"]
    assert hooks.call_count == 1  # webhook still fires once


def test_cloud_transcript_known_unavailable_skips_background(route_env):  # noqa: F811
    client, rec, trn = route_env
    _groq_setup(rec)
    thread = MagicMock()
    with (
        patch("app.routes.transcribe.transcribe_groq", return_value=json.loads(json.dumps(GROQ_RESULT))),
        patch("app.routes.transcribe.threading.Thread", thread),
        patch("src.transcribe.known_unavailable_reason", return_value="Speaker labels skipped: offline"),
    ):
        r = _transcribe(client, rec / "1715000000.ogg")
    body = r.get_json()
    assert body["diarizing"] is False
    assert body["diarization_message"] == "Speaker labels skipped: offline"
    thread.assert_not_called()
    saved = json.loads((trn / "1715000000.json").read_text())
    assert saved["diarization_status"] == "failed"


def test_background_result_dropped_if_retranscribed(route_env):  # noqa: F811
    client, rec, trn = route_env
    _groq_setup(rec)
    started = []
    with (
        patch("app.routes.transcribe.transcribe_groq", return_value=json.loads(json.dumps(GROQ_RESULT))),
        patch(
            "app.routes.transcribe.threading.Thread",
            lambda target, kwargs, daemon: MagicMock(start=lambda: started.append(kwargs)),
        ),
    ):
        _transcribe(client, rec / "1715000000.ogg")
    # The transcript changes before the job finishes.
    path = trn / "1715000000.json"
    data = json.loads(path.read_text())
    data["text"] = "Something else entirely."
    path.write_text(json.dumps(data))

    from app.routes.transcribe import _background_diarize

    done = T.DiarizationResult("done", segments=[dict(s) for s in RAW[:2]], engine="diarize")
    with patch("src.transcribe.run_diarization", return_value=done):
        _background_diarize(**started[0])
    saved = json.loads(path.read_text())
    assert saved["text"] == "Something else entirely."
    assert saved["provider"] == "groq"
    assert "speakers" not in saved


def test_deepgram_speakers_need_no_local_job(route_env):  # noqa: F811
    from app.utils.config import save_config

    client, rec, trn = route_env
    (rec / "1715000000.ogg").write_bytes(b"OggS" + bytes(100))
    save_config({"deepgram_api_key": "k", "transcription_provider": "deepgram", "diarize": True})
    dg = {
        "text": "Hello. Hi there.",
        "segments": [{"start": 0, "end": 5, "text": "Hello.", "speaker": "Speaker 1"}],
        "duration": 5,
        "speakers": ["Speaker 1"],
    }
    thread = MagicMock()
    with (
        patch("app.routes.transcribe.transcribe_deepgram", return_value=dg) as fn,
        patch("app.routes.transcribe.threading.Thread", thread),
    ):
        r = _transcribe(client, rec / "1715000000.ogg")
    assert fn.call_args.kwargs["diarize"] is True
    assert r.get_json()["diarizing"] is False
    thread.assert_not_called()
    assert json.loads((trn / "1715000000.json").read_text())["diarization_status"] == "done"


def _local_setup(rec):
    from app.utils.config import save_config

    (rec / "1715000000.ogg").write_bytes(b"OggS" + bytes(100))
    save_config({"transcription_provider": "local", "diarize": True})


def _whisper_only():
    return T.Transcript(
        text="Hello. Hi there.",
        language="en",
        duration=9,
        segments=[T.TranscriptSegment(0, 5, "Hello."), T.TranscriptSegment(5, 9, "Hi there.")],
    )


def test_local_whisper_path_reports_why(route_env):  # noqa: F811
    client, rec, trn = route_env
    _local_setup(rec)
    thread = MagicMock()
    with (
        patch("src.transcribe.transcribe", return_value=_whisper_only()) as fn,
        patch("src.transcribe.known_unavailable_reason", return_value="Speaker labels skipped: offline"),
        patch("app.routes.transcribe.threading.Thread", thread),
    ):
        r = _transcribe(client, rec / "1715000000.ogg")
    assert fn.call_args.kwargs["diarize"] is False  # never inline
    body = r.get_json()
    assert body["diarizing"] is False
    assert body["diarization_message"] == "Speaker labels skipped: offline"
    thread.assert_not_called()
    saved = json.loads((trn / "1715000000.json").read_text())
    assert saved["diarization_status"] == "failed"
    assert saved["provider"] == "local"


def test_local_transcribe_saves_text_before_slow_diarization(route_env):  # noqa: F811
    """The response (and the saved text) don't wait for speaker detection."""
    import threading

    client, rec, trn = route_env
    _local_setup(rec)
    release, entered = threading.Event(), threading.Event()
    done = T.DiarizationResult("done", segments=[dict(s) for s in RAW[:2]], engine="diarize")

    def slow_diarize(path):
        entered.set()
        assert release.wait(10), "test never released the diarization"
        return done

    started = []
    real_thread = threading.Thread

    def record_thread(*a, **k):
        th = real_thread(*a, **k)
        started.append(th)
        return th

    path = trn / "1715000000.json"
    with (
        patch("src.transcribe.transcribe", return_value=_whisper_only()) as fn,
        patch("src.transcribe.known_unavailable_reason", return_value=None),
        patch("src.transcribe.run_diarization", side_effect=slow_diarize),
        patch("app.routes.transcribe.threading.Thread", side_effect=record_thread),
    ):
        try:
            r = _transcribe(client, rec / "1715000000.ogg")
            assert entered.wait(5)
            # Diarization is still blocked, yet the request has finished.
            assert fn.call_args.kwargs["diarize"] is False
            body = r.get_json()
            assert r.status_code == 200, body
            assert body["diarizing"] is True
            assert body["text"] == "Hello. Hi there."
            saved = json.loads(path.read_text())
            assert saved["text"] == "Hello. Hi there."
            assert saved["diarization_status"] == "running"
            assert saved["provider"] == "local"
            assert "speakers" not in saved
        finally:
            release.set()
            for th in started:
                th.join(10)
    assert len(started) == 1 and not started[0].is_alive()

    saved = json.loads(path.read_text())
    assert saved["diarization_status"] == "done"
    assert saved["text"] == "Hello. Hi there."
    assert saved["provider"] == "local+diarize"
    assert saved["speakers"] == ["Speaker 1", "Speaker 2"]
    assert [s["speaker"] for s in saved["segments"]] == ["Speaker 1", "Speaker 2"]
    txt = path.with_suffix(".txt").read_text()
    assert "[Speaker 1] Hello." in txt and "[Speaker 2] Hi there." in txt


@pytest.mark.parametrize("crash", ["run", "merge"])
def test_local_transcribe_survives_crashing_diarization(route_env, crash):  # noqa: F811
    client, rec, trn = route_env
    _local_setup(rec)
    done = T.DiarizationResult("done", segments=[dict(s) for s in RAW[:2]], engine="diarize")
    run = (
        patch("src.transcribe.run_diarization", side_effect=RuntimeError("onnx segfault-ish"))
        if crash == "run"
        else patch("src.transcribe.run_diarization", return_value=done)
    )
    merge = patch("src.transcribe.merge_transcript_with_diarization", side_effect=ValueError("bad segments"))
    with (
        patch("src.transcribe.transcribe", return_value=_whisper_only()),
        patch("src.transcribe.known_unavailable_reason", return_value=None),
        patch("app.routes.transcribe.threading.Thread", _SyncThread),
        run,
        merge if crash == "merge" else nullcontext(),
        patch("app.routes.transcribe.fire_webhooks") as hooks,
    ):
        r = _transcribe(client, rec / "1715000000.ogg")
    assert r.status_code == 200, r.get_json()
    path = trn / "1715000000.json"
    saved = json.loads(path.read_text())
    assert saved["text"] == "Hello. Hi there."
    assert [s["text"] for s in saved["segments"]] == ["Hello.", "Hi there."]
    assert saved["diarization_status"] == "failed"
    assert saved["diarization_message"]
    assert saved["provider"] == "local"
    assert "speakers" not in saved
    assert "Hello." in path.with_suffix(".txt").read_text()
    assert hooks.call_count == 1  # webhook still fires once, after the job


def test_local_transcribe_without_diarize_fires_webhook_now(route_env):  # noqa: F811
    from app.utils.config import save_config

    client, rec, trn = route_env
    (rec / "1715000000.ogg").write_bytes(b"OggS" + bytes(100))
    save_config({"transcription_provider": "local", "diarize": False})
    thread = MagicMock()
    with (
        patch("src.transcribe.transcribe", return_value=_whisper_only()),
        patch("app.routes.transcribe.threading.Thread", thread),
        patch("app.routes.transcribe.fire_webhooks") as hooks,
    ):
        r = _transcribe(client, rec / "1715000000.ogg")
    assert r.get_json()["diarizing"] is False
    thread.assert_not_called()
    assert hooks.call_count == 1
    saved = json.loads((trn / "1715000000.json").read_text())
    assert "diarization_status" not in saved
