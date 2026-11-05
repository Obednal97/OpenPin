"""On-device laughter detection (the ONNX session is stubbed; no download, no model)."""

import hashlib
import io
import math
import os
import time
import types

import numpy as np
import pytest
import soundfile as sf

from app.coaching import laughter

SR = laughter.SAMPLE_RATE
W = int(laughter.WINDOW_S * SR)
H = int(laughter.STEP_S * SR)


@pytest.fixture(autouse=True)
def _model_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(laughter, "model_path", lambda: tmp_path / "models" / "ced-tiny.onnx")


# ------------------------------------------------------------------ windows


def test_window_count_and_step():
    assert laughter.window_count(0) == 0
    assert laughter.window_count(1) == 1
    assert laughter.window_count(W) == 1
    assert laughter.window_count(W + 1) == 2
    assert laughter.window_count(W + H) == 2
    assert laughter.window_count(W + H + 1) == 3
    assert laughter.window_count(10 * SR) == 17  # (10 - 2) / 0.5 + 1


def test_windows_step_and_pad_last(tmp_path):
    n = W + H + H // 2  # last window runs past the end and is zero-padded
    a = np.arange(1, n + 1, dtype=np.float32) / n
    wav = tmp_path / "a.wav"
    sf.write(wav, a, SR, subtype="FLOAT")
    batches = list(laughter.iter_windows(str(wav)))
    x = np.concatenate([b for b, _ in batches])
    starts = np.concatenate([s for _, s in batches])
    assert x.shape == (3, W)
    assert starts.tolist() == [0.0, 0.5, 1.0]
    np.testing.assert_allclose(x[1], a[H : H + W])
    tail = n - 2 * H
    np.testing.assert_allclose(x[2, :tail], a[2 * H :])
    assert not x[2, tail:].any()


def test_reads_in_bounded_chunks(tmp_path, monkeypatch):
    seconds = 120
    wav = tmp_path / "long.wav"
    sf.write(wav, np.zeros(seconds * SR, dtype=np.float32), SR)
    real_read = sf.read
    frames_seen = []

    def bounded_read(*args, **kwargs):
        frames = kwargs.get("frames", -1)
        assert 0 < frames <= (laughter.BATCH - 1) * H + W, frames
        frames_seen.append(frames)
        return real_read(*args, **kwargs)

    monkeypatch.setattr(sf, "read", bounded_read)
    total = sum(len(b) for b, _ in laughter.iter_windows(str(wav)))
    assert total == laughter.window_count(seconds * SR)
    assert len(frames_seen) > 1


# ------------------------------------------------------------ normalisation


def test_quiet_window_scaled_to_minus_20_dbfs():
    x = np.full((1, W), 0.01, dtype=np.float32)  # -40 dBFS
    y, raw_db = laughter.normalise(x)
    frame_rms = np.sqrt((y.reshape(1, -1, laughter.FRAME) ** 2).mean(2)).max()
    assert frame_rms == pytest.approx(0.1, rel=1e-4)
    assert raw_db[0] == pytest.approx(-40.0, abs=0.01)


def test_gain_uses_loudest_frame():
    x = np.zeros((1, W), dtype=np.float32)
    x[0, : laughter.FRAME] = 0.05  # one loud 25 ms frame
    x[0, laughter.FRAME :] = 0.005
    y, raw_db = laughter.normalise(x)
    assert y[0, 0] == pytest.approx(0.1, rel=1e-4)
    assert y[0, -1] == pytest.approx(0.01, rel=1e-4)
    assert raw_db[0] == pytest.approx(20 * math.log10(0.05), abs=0.01)


def test_near_silence_left_alone():
    x = np.full((1, W), 0.0005, dtype=np.float32)  # about -66 dBFS
    y, raw_db = laughter.normalise(x)
    np.testing.assert_array_equal(y, x)
    assert raw_db[0] < -60


def test_all_zero_window_has_finite_loudness():
    y, raw_db = laughter.normalise(np.zeros((1, W), dtype=np.float32))
    assert not y.any() and np.isfinite(raw_db[0])


# ------------------------------------------------------------------ scoring


def test_score_is_max_over_laughter_classes():
    p = np.zeros((3, 527), dtype=np.float32)
    p[0, 0] = 0.99  # speech only
    p[1, 16] = 0.10
    p[1, 21] = 0.30  # chuckle
    p[2, 15] = 0.90
    p[2, 22] = 0.90  # neighbours of the laughter classes
    np.testing.assert_allclose(laughter.laughter_scores(p), [0.0, 0.30, 0.0])


def test_threshold_inclusive():
    scores = np.array([0.149, 0.15, 0.0])
    loud = np.array([-30.0, -30.0, -30.0])
    starts = np.array([0.0, 0.5, 1.0])
    ev = laughter.events_from_scores(scores, loud, starts)
    assert len(ev) == 1 and ev[0]["start"] == pytest.approx(1.0)


# ------------------------------------------------------------------- events


def _starts(n):
    return np.arange(n) * laughter.STEP_S


def test_merge_with_half_second_edges():
    scores = np.array([0.0, 0.2, 0.6, 0.3, 0.0, 0.0, 0.5, 0.0])
    loud = np.array([-50, -30, -25, -28, -50, -50, -35, -50], dtype=float)
    ev = laughter.events_from_scores(scores, loud, _starts(8))
    assert len(ev) == 2
    # windows 1..3: start 0.5 + 0.5, end (1.5 + 2.0) - 0.5
    assert ev[0]["start"] == pytest.approx(1.0) and ev[0]["end"] == pytest.approx(3.0)
    assert ev[0]["score"] == pytest.approx(0.6)
    assert ev[0]["loudness_db"] == pytest.approx(-25.0)
    # a single window gives a 1 s event
    assert ev[1]["start"] == pytest.approx(3.5) and ev[1]["end"] == pytest.approx(4.5)


def test_long_events_dropped_as_background():
    # 15 windows: 0.5 .. (7.0 + 2.0 - 0.5) = 8.0 s long, kept; 16 windows = 8.5 s, dropped
    kept = laughter.events_from_scores(np.full(15, 0.5), np.full(15, -30.0), _starts(15))
    assert len(kept) == 1 and kept[0]["end"] - kept[0]["start"] == pytest.approx(8.0)
    dropped = laughter.events_from_scores(np.full(16, 0.5), np.full(16, -30.0), _starts(16))
    assert dropped == []


def test_event_end_clipped_to_audio():
    ev = laughter.events_from_scores(np.array([0.0, 0.5]), np.array([-30.0, -30.0]), _starts(2), duration=1.8)
    assert ev[0]["start"] == pytest.approx(1.0) and ev[0]["end"] == pytest.approx(1.8)


def test_tail_window_past_the_end_dropped():
    # audio ends at 1.2 s; the last window starts at 1.0 s, so its event would start at 1.5 s
    ev = laughter.events_from_scores(np.array([0.0, 0.0, 0.5]), np.full(3, -30.0), _starts(3), duration=1.2)
    assert ev == []
    for e in laughter.events_from_scores(np.array([0.0, 0.5, 0.5]), np.full(3, -30.0), _starts(3), duration=1.2):
        assert e["start"] < e["end"] <= 1.2


# ----------------------------------------------------------------- download


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _fake_download(monkeypatch, payload: bytes):
    calls = []

    def urlopen(url, timeout=None, context=None):
        calls.append((url, timeout))
        return _FakeResponse(payload)

    monkeypatch.setattr(laughter.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(laughter, "MODEL_SIZE", len(payload))
    monkeypatch.setattr(laughter, "MODEL_SHA256", hashlib.sha256(b"the real model").hexdigest())
    return calls


def test_download_bad_sha_removed(monkeypatch):
    calls = _fake_download(monkeypatch, b"tampered model")
    ok, reason = laughter.ensure_model()
    assert not ok and reason == "Couldn't download the laughter model"
    folder = laughter.model_path().parent
    assert not laughter.model_path().exists()
    assert list(folder.iterdir()) == []  # no temp file left behind
    assert calls and calls[0][1] is not None  # a timeout is always set


def test_download_wrong_size_rejected(monkeypatch):
    _fake_download(monkeypatch, b"the real model")
    monkeypatch.setattr(laughter, "MODEL_SIZE", 3)
    ok, _ = laughter.ensure_model()
    assert not ok and not laughter.model_path().exists()


def test_download_good_is_atomic(monkeypatch):
    calls = _fake_download(monkeypatch, b"the real model")
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((str(src), str(dst)))
        assert os.path.dirname(str(src)) == os.path.dirname(str(dst))  # same folder, so atomic
        real_replace(src, dst)

    monkeypatch.setattr(laughter.os, "replace", spy)
    assert laughter.ensure_model() == (True, "")
    assert laughter.model_path().read_bytes() == b"the real model"
    assert replaced and replaced[0][1] == str(laughter.model_path())
    assert calls[0][0] == laughter.MODEL_URL
    # Present and valid: no second download.
    assert laughter.ensure_model() == (True, "")
    assert len(calls) == 1


def test_corrupt_existing_file_replaced(monkeypatch):
    _fake_download(monkeypatch, b"the real model")
    laughter.model_path().parent.mkdir(parents=True)
    laughter.model_path().write_bytes(b"something else")
    assert laughter.ensure_model() == (True, "")
    assert laughter.model_path().read_bytes() == b"the real model"


def test_existing_file_not_deleted_before_download(monkeypatch):
    _fake_download(monkeypatch, b"the real model")
    laughter.model_path().parent.mkdir(parents=True)
    laughter.model_path().write_bytes(b"old copy")
    real = laughter.urllib.request.urlopen

    def urlopen(*a, **k):
        assert laughter.model_path().exists()  # replaced atomically, never removed first
        return real(*a, **k)

    monkeypatch.setattr(laughter.urllib.request, "urlopen", urlopen)
    assert laughter.ensure_model() == (True, "")
    assert laughter.model_path().read_bytes() == b"the real model"


def test_stale_part_files_swept(monkeypatch):
    _fake_download(monkeypatch, b"the real model")
    folder = laughter.model_path().parent
    folder.mkdir(parents=True)
    laughter.model_path().write_bytes(b"the real model")
    stale = folder / ".ced-tiny-old.part"
    stale.write_bytes(b"half")
    old = time.time() - 2 * laughter.DOWNLOAD_DEADLINE_S
    os.utime(stale, (old, old))
    fresh = folder / ".ced-tiny-new.part"  # another process may still be writing this one
    fresh.write_bytes(b"half")
    other = folder / "notes.part"
    other.write_bytes(b"x")
    assert laughter.ensure_model() == (True, "")
    assert not stale.exists() and fresh.exists() and other.exists()


def test_download_deadline(monkeypatch):
    _fake_download(monkeypatch, b"x")
    monkeypatch.setattr(laughter, "MODEL_SIZE", 10**9)

    class Trickle(_FakeResponse):
        def read(self, n=-1):
            return b"x"

    monkeypatch.setattr(laughter.urllib.request, "urlopen", lambda *a, **k: Trickle(b""))
    clock = iter(range(0, 10**6, 60))
    monkeypatch.setattr(laughter.time, "monotonic", lambda: next(clock))
    assert laughter.ensure_model() == (False, "Couldn't download the laughter model")
    assert list(laughter.model_path().parent.iterdir()) == []


def test_download_network_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(laughter.urllib.request, "urlopen", boom)
    assert laughter.ensure_model() == (False, "Couldn't download the laughter model")


def test_pinned_model_constants():
    assert laughter.MODEL_URL == (
        "https://huggingface.co/mispeech/ced-tiny/resolve/ace276d29dd0bb3f3517b0fa8cf300738c409019/model.onnx"
    )
    assert laughter.MODEL_SHA256 == "3890f227660be71886ff48633d11a731993e0206db8a486781412c28a0873435"
    assert laughter.MODEL_SIZE == 6_681_767
    assert laughter.THRESHOLD == 0.15 and laughter.MAX_EVENT_S == 8.0


def test_model_lives_in_support_folder(monkeypatch, tmp_path):
    monkeypatch.undo()
    from app.mcp import access

    monkeypatch.setattr(access, "support_dir", lambda: tmp_path / "support")
    assert laughter.model_path() == tmp_path / "support" / "models" / "ced-tiny.onnx"


# ------------------------------------------------------------------- detect


def test_detect_without_runtime(monkeypatch):
    monkeypatch.setattr(laughter, "runtime_available", lambda: False)
    r = laughter.detect("a.wav")
    assert r == {
        "status": "unavailable",
        "events": [],
        "reason": "Laughter detection needs the speaker model (Settings › Transcription & AI)",
    }


def test_detect_without_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(laughter, "runtime_available", lambda: True)
    for path in (None, "", str(tmp_path / "missing.opus")):
        r = laughter.detect(path)
        assert r["status"] == "unavailable" and r["reason"] == "No audio on this Mac" and r["events"] == []


def test_detect_download_failure(monkeypatch, tmp_path):
    audio = tmp_path / "a.wav"
    sf.write(audio, np.zeros(SR, dtype=np.float32), SR)
    monkeypatch.setattr(laughter, "runtime_available", lambda: True)
    monkeypatch.setattr(laughter, "ensure_model", lambda: (False, "Couldn't download the laughter model"))
    r = laughter.detect(str(audio))
    assert r["status"] == "unavailable" and r["reason"] == "Couldn't download the laughter model"


class _StubSession:
    """Laughter when the window is loud (normalised input reaches the model)."""

    def __init__(self, laugh_from_s: float, laugh_to_s: float):
        self.span = (laugh_from_s, laugh_to_s)
        self.batches = []

    def get_inputs(self):
        return [types.SimpleNamespace(name="waveform")]

    def run(self, outputs, feeds):
        x = feeds["waveform"]
        assert x.dtype == np.float32 and x.shape[1] == W and len(x) <= laughter.BATCH
        self.batches.append(len(x))
        p = np.zeros((len(x), 527), dtype=np.float32)
        p[np.abs(x).max(1) > 0.07, 18] = 0.8  # giggle: only a normalised 0.05 burst reaches 0.1
        return [p]


def _run_detect(monkeypatch, tmp_path, audio, session, seconds=None):
    path = tmp_path / "in.wav"
    sf.write(path, audio, SR)
    wav = tmp_path / "converted.wav"
    sf.write(wav, audio, SR)
    monkeypatch.setattr(laughter, "runtime_available", lambda: True)
    monkeypatch.setattr(laughter, "ensure_model", lambda: (True, ""))
    monkeypatch.setattr(laughter, "_session", lambda: session)
    monkeypatch.setattr("src.transcribe._to_wav16k", lambda p: str(wav))
    kwargs = {} if seconds is None else {"max_seconds": seconds}
    return laughter.detect(str(path), **kwargs), wav


def test_detect_finds_event_and_cleans_up(monkeypatch, tmp_path):
    a = np.zeros(40 * SR, dtype=np.float32)
    a[8 * SR : 9 * SR] = 0.05  # a quiet burst; normalisation lifts it past the stub's bar
    session = _StubSession(8, 9)
    r, wav = _run_detect(monkeypatch, tmp_path, a, session)
    assert r["status"] == "done" and r["reason"] == ""
    assert len(r["events"]) == 1
    e = r["events"][0]
    # windows starting at 6.5 .. 8.5 s overlap 8..9 s
    assert e["start"] == pytest.approx(7.0) and e["end"] == pytest.approx(10.0)
    assert e["score"] == pytest.approx(0.8)
    assert e["loudness_db"] == pytest.approx(20 * math.log10(0.05), abs=0.1)  # raw, not normalised
    assert not wav.exists()  # the temp wav is deleted
    assert session.batches == [laughter.BATCH, laughter.window_count(40 * SR) - laughter.BATCH]


def test_detect_too_long(monkeypatch, tmp_path):
    r, wav = _run_detect(monkeypatch, tmp_path, np.zeros(5 * SR, dtype=np.float32), _StubSession(0, 0), seconds=4)
    assert r == {"status": "unavailable", "events": [], "reason": "Too long to check for laughter"}
    assert not wav.exists()


def test_detect_never_raises(monkeypatch, tmp_path):
    class Broken(_StubSession):
        def run(self, outputs, feeds):
            raise RuntimeError("onnx exploded")

    r, wav = _run_detect(monkeypatch, tmp_path, np.zeros(5 * SR, dtype=np.float32), Broken(0, 0))
    assert r["status"] == "unavailable" and r["events"] == [] and r["reason"]
    assert not wav.exists()


def test_detect_conversion_failure(monkeypatch, tmp_path):
    audio = tmp_path / "a.opus"
    audio.write_bytes(b"x")
    monkeypatch.setattr(laughter, "runtime_available", lambda: True)
    monkeypatch.setattr(laughter, "ensure_model", lambda: (True, ""))
    monkeypatch.setattr("src.transcribe._to_wav16k", lambda p: None)
    r = laughter.detect(str(audio))
    assert r["status"] == "unavailable" and r["reason"]


# --------------------------------------------------------------- real model


REAL_MODEL = os.environ.get("OPEN_PIN_LAUGHTER_MODEL")


@pytest.mark.skipif(not REAL_MODEL or not os.path.isfile(REAL_MODEL), reason="OPEN_PIN_LAUGHTER_MODEL not set")
def test_real_model_noise_gives_no_events(monkeypatch, tmp_path):
    pytest.importorskip("onnxruntime")
    monkeypatch.setattr(laughter, "model_path", lambda: __import__("pathlib").Path(REAL_MODEL))
    assert laughter.ensure_model() == (True, "")  # the pinned file passes its own checks
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(30 * SR) * 0.05).astype(np.float32)
    wav = tmp_path / "noise.wav"
    sf.write(wav, noise, SR)
    r = laughter.detect(str(wav))
    assert r == {"status": "done", "events": [], "reason": ""}
