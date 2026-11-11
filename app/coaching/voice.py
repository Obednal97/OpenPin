"""Owner voice profile and matching, entirely local (WeSpeaker embeddings).

Voice embeddings never leave the Mac. numpy, soundfile and wespeakerruntime come
from the optional ``diarize`` extra, so they are imported lazily and
``import app.coaching.voice`` always works. Vector maths is plain Python
(embeddings are lists of floats, 256 numbers each).
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import threading
from datetime import UTC, datetime

from app.utils.constants import COACHING_DIR

log = logging.getLogger(__name__)

PROFILE_PATH = COACHING_DIR / "voice_profile.json"
MAX_EXEMPLARS = 20
MATCH_THRESHOLD = 0.55  # cosine
MARGIN = 0.10  # a label counts as the owner if within this of the best match
MIN_CLIP_SECONDS = 1.0
EMBEDDING_DIM = 256

_speaker_model = None
_model_lock = threading.Lock()


# ---------------------------------------------------------------- vector maths


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


def _mean(vectors: list[list[float]]) -> list[float]:
    return [sum(col) / len(vectors) for col in zip(*vectors, strict=True)]


def _cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


# --------------------------------------------------------------------- model


def model_available() -> bool:
    """True when the WeSpeaker runtime is importable. Never installs anything."""
    try:
        import numpy  # noqa: F401
        import soundfile  # noqa: F401
        import wespeakerruntime  # noqa: F401
    except Exception:
        return False
    return True


def install_model() -> tuple[bool, str]:
    """Install the ``diarize`` extra (blocking). Used only by the voice-setup route."""
    try:
        from src.transcribe import _load_diarize_lib

        if _load_diarize_lib() is None:
            return False, "Could not install the speaker model. Check your internet connection, then try again."
    except Exception as e:
        log.warning("voice model install failed: %s", e)
        return False, f"Could not install the speaker model ({e})."
    if not model_available():
        return False, "The speaker model installed but could not be loaded."
    return True, ""


def _embed_wav(path: str) -> list[float]:
    """Embedding of one 16 kHz mono WAV clip, from a lazily built, cached model."""
    global _speaker_model
    with _model_lock:
        if _speaker_model is None:
            import wespeakerruntime as wespeaker

            _speaker_model = wespeaker.Speaker(lang="en")
        model = _speaker_model
    try:
        emb = model.extract_embedding(path)
    except ImportError:
        # torchaudio >= 2.9 can't load files without torchcodec: read the clip
        # with soundfile and feed the runtime its fbank features instead.
        emb = _embed_via_fbank(model, path)
    # The runtime returns a numpy array shaped [1, dim]; flatten to a list.
    flat = emb.reshape(-1).tolist() if hasattr(emb, "reshape") else _flatten(emb)
    if len(flat) != EMBEDDING_DIM:
        raise ValueError(f"unexpected embedding size {len(flat)}")
    return [float(x) for x in flat]


def _embed_via_fbank(model, path: str):
    import numpy as np
    import soundfile as sf
    import torch
    from torchaudio.compliance import kaldi

    data, rate = sf.read(path, dtype="float32", always_2d=True)
    wave = torch.from_numpy(np.ascontiguousarray(data.T[:1])) * (1 << 15)
    feats = kaldi.fbank(
        wave,
        num_mel_bins=80,
        frame_length=25,
        frame_shift=10,
        dither=0.0,
        sample_frequency=rate,
        window_type="hamming",
        use_energy=False,
    ).numpy()
    feats = feats - np.mean(feats, axis=0)
    return model.extract_embedding_feat(np.expand_dims(feats, 0), cmn=False)


def _flatten(v) -> list:
    out: list = []
    for x in v:
        out.extend(_flatten(x) if hasattr(x, "__iter__") else [x])
    return out


# --------------------------------------------------------------------- clips


def _speaker_clips(audio_path: str, segments: list[dict], max_seconds: float) -> dict[str, list[str]]:
    """Cut each speaker's longest segments (up to ``max_seconds`` in total) into
    temporary 16 kHz WAV clips. Returns {label: [clip paths]}; caller deletes them.
    Segments shorter than 1.0 s, or with no speaker label, are skipped."""
    import soundfile as sf

    from src.transcribe import _to_wav16k

    wav = _to_wav16k(audio_path)
    if not wav:
        return {}
    clips: dict[str, list[str]] = {}
    try:
        info = sf.info(wav)
        rate = info.samplerate
        by_speaker: dict[str, list[dict]] = {}
        for s in segments:
            if s.get("speaker") is not None and s["end"] - s["start"] >= MIN_CLIP_SECONDS:
                by_speaker.setdefault(s["speaker"], []).append(s)
        for label, segs in by_speaker.items():
            total = 0.0
            for s in sorted(segs, key=lambda s: s["start"] - s["end"]):  # longest first
                if total >= max_seconds:
                    break
                length = min(s["end"] - s["start"], max_seconds - total)
                data, _ = sf.read(wav, start=int(s["start"] * rate), frames=int(length * rate))
                if len(data) < MIN_CLIP_SECONDS * rate:
                    continue
                fd, path = tempfile.mkstemp(prefix="plaud-voice-", suffix=".wav")
                os.close(fd)
                sf.write(path, data, rate)
                clips.setdefault(label, []).append(path)
                total += length
    except Exception:
        _remove_clips(clips)
        raise
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    return clips


def _remove_clips(clips: dict[str, list[str]]) -> None:
    for paths in clips.values():
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


def speaker_embeddings(
    audio_path: str, segments: list[dict], max_seconds_per_speaker: float = 60.0
) -> dict[str, list[float]]:
    """One mean, L2-normalised embedding per speaker label, from that speaker's
    longest segments up to the cap. Speakers with no usable audio are omitted."""
    clips = _speaker_clips(audio_path, segments, max_seconds_per_speaker)
    try:
        out: dict[str, list[float]] = {}
        for label, paths in clips.items():
            vectors = [_embed_wav(p) for p in paths]
            if vectors:
                out[label] = _normalise(_mean(vectors))
        return out
    finally:
        _remove_clips(clips)


# ------------------------------------------------------------------- profile


def _load_exemplars() -> list[list[float]]:
    from app.people import store

    return [s["vec"] for s in store.load()["people"][store.ME]["samples"]]


def _save_exemplars(
    exemplars: list[list[float]], session_id: str | None = None, labels: list[str] | None = None
) -> None:
    """Replace the owner's samples with ``exemplars`` (keeping the existing sample for any
    vector already held) through the voice store, which writes atomically with mode 0600
    and mirrors the vectors to PROFILE_PATH. Keeps the newest MAX_EXEMPLARS."""
    from app.people import store

    exemplars = exemplars[-MAX_EXEMPLARS:]
    stamp = datetime.now(UTC).isoformat(timespec="seconds")

    def fn(data: dict) -> None:
        old = data["people"][store.ME]["samples"]
        out = []
        for i, vec in enumerate(exemplars):
            held = next((s for s in old if s["vec"] == vec), None)
            if held is not None:
                out.append(held)
                continue
            label = labels[i] if labels and i < len(labels) else None
            if session_id is not None:
                store.remove_sample(data, session_id, label)
            out.append({"vec": vec, "session_id": session_id, "label": label, "seconds": 0, "added_at": stamp})
        data["people"][store.ME]["samples"] = out

    store.mutate(fn)


def delete_profile() -> None:
    """Forget the owner's voice: clear the ``me`` samples and remove the v1 file."""
    from app.people import store

    store.mutate(lambda data: data["people"][store.ME].update(samples=[]))  # also unlinks the v1 mirror


def add_exemplars(audio_path: str, segments: list[dict], labels: list[str], session_id: str | None = None) -> int:
    """Add one exemplar per given label from this recording (voice setup and the
    correction path). Returns how many were added. Oldest exemplars drop past 20."""
    embeddings = speaker_embeddings(audio_path, segments)
    got = [label for label in labels if label in embeddings]
    new = [embeddings[label] for label in got]
    if new:
        _save_exemplars(_load_exemplars() + new, session_id, got)
    return len(new)


def replace_profile(audio_path: str, segments: list[dict], labels: list[str], session_id: str | None = None) -> int:
    """Redo voice setup: build exemplars from this recording and swap them in for the old
    profile, but only if at least one was produced (a failed redo keeps the old voice).
    Returns how many exemplars the new profile has (0 = nothing changed)."""
    embeddings = speaker_embeddings(audio_path, segments)
    got = [label for label in labels if label in embeddings]
    new = [embeddings[label] for label in got]
    if new:
        _save_exemplars(new, session_id, got)
    return len(new)


# ------------------------------------------------------------------ matching


def _none(reason: str) -> dict:
    return {"labels": [], "confidence": 0.0, "source": "none", "reason": reason}


def match_owner(audio_path: str, segments: list[dict]) -> dict:
    """Which speaker labels are the owner. Never installs the model, never raises."""
    try:
        exemplars = _load_exemplars()
        if not exemplars:
            return _none("no voice profile")
        if not model_available():
            return _none("voice model not installed")
        embeddings = speaker_embeddings(audio_path, segments)
        scores = {label: max(_cosine(e, ex) for ex in exemplars) for label, e in embeddings.items()}
        if not scores:
            return _none("no confident match")
        best = max(scores.values())
        if best < MATCH_THRESHOLD:
            return _none("no confident match")
        labels = sorted(label for label, s in scores.items() if s >= MATCH_THRESHOLD and s >= best - MARGIN)
        return {"labels": labels, "confidence": best, "source": "voice"}
    except Exception as e:
        log.warning("owner voice match failed: %s", e, exc_info=True)
        return _none("no confident match")


def loudest_label(audio_path: str, segments: list[dict]) -> str | None:
    """The RMS-loudest speaker. A suggestion only; never decides the owner.

    Reads one labelled segment at a time as float32, so an hour-long recording never
    sits in memory whole. Segments with no speaker label are skipped."""
    wav = None
    try:
        import numpy as np
        import soundfile as sf

        from src.transcribe import _to_wav16k

        wav = _to_wav16k(audio_path)
        if not wav:
            return None
        rate = sf.info(wav).samplerate
        sums: dict[str, list[float]] = {}  # label -> [sum of squares, samples]
        for s in segments:
            label = s.get("speaker")
            start = int(s["start"] * rate)
            frames = int(s["end"] * rate) - start
            if label is None or frames <= 0:
                continue
            chunk, _ = sf.read(wav, start=start, frames=frames, dtype="float32")
            if len(chunk):
                acc = sums.setdefault(label, [0.0, 0])
                acc[0] += float(np.sum(np.square(chunk, dtype=np.float64)))
                acc[1] += len(chunk)
        if not sums:
            return None
        return max(sums, key=lambda k: sums[k][0] / sums[k][1])
    except Exception as e:
        log.warning("loudest speaker detection failed: %s", e)
        return None
    finally:
        if wav:
            try:
                os.unlink(wav)
            except OSError:
                pass
