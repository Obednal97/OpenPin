"""Local transcription (openai-whisper) and local speaker identification.

Speaker identification ("diarization") tries, in order:
1. the ``diarize`` package (Silero VAD + WeSpeaker ONNX, CPU). No account or
   token needed; the pinned ``diarize`` extra is installed on first use.
2. pyannote (``pyannote/speaker-diarization-3.1``), only when a Hugging Face
   token is saved and pyannote.audio is already installed. The app never
   installs pyannote itself (it isn't in the pinned lockfiles).

Usage:
    python -m src.transcribe path/to/audio.mp3
    python -m src.transcribe path/to/audio.mp3 --diarize
    python -m src.transcribe cloud_recordings/ --all
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    start: float  # seconds
    end: float
    text: str
    speaker: str = ""


@dataclass
class Transcript:
    segments: list[TranscriptSegment] = field(default_factory=list)
    text: str = ""
    language: str = ""
    duration: float = 0.0
    # Outcome of speaker identification when it was requested: see
    # DiarizationResult.status. Empty when it wasn't requested.
    diarization_status: str = ""
    diarization_message: str = ""

    @property
    def formatted(self) -> str:
        """Format transcript with timestamps and optional speaker labels."""
        lines = []
        for seg in self.segments:
            ts_start = _format_time(seg.start)
            ts_end = _format_time(seg.end)
            speaker = f"[{seg.speaker}] " if seg.speaker else ""
            lines.append(f"[{ts_start} -> {ts_end}] {speaker}{seg.text.strip()}")
        return "\n".join(lines)

    def save(self, path: str):
        """Save transcript to JSON."""
        data = {
            "text": self.text,
            "language": self.language,
            "duration": self.duration,
            "segments": [
                {
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                    "speaker": s.speaker,
                }
                for s in self.segments
            ],
        }
        if self.diarization_status:
            data["diarization_status"] = self.diarization_status
            data["diarization_message"] = self.diarization_message
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def load(path: str) -> Transcript:
        """Load transcript from JSON."""
        with open(path) as f:
            data = json.load(f)
        t = Transcript(
            text=data.get("text", ""),
            language=data.get("language", ""),
            duration=data.get("duration", 0),
            diarization_status=data.get("diarization_status", ""),
            diarization_message=data.get("diarization_message", ""),
        )
        for seg in data.get("segments", []):
            t.segments.append(
                TranscriptSegment(
                    start=seg["start"],
                    end=seg["end"],
                    text=seg["text"],
                    speaker=seg.get("speaker", ""),
                )
            )
        return t


def _format_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def transcribe_with_whisper(
    audio_path: str,
    model: str = "base",
    language: str | None = None,
    initial_prompt: str | None = None,
) -> Transcript:
    """Transcribe using openai-whisper (simpler, works everywhere)."""
    try:
        import whisper
    except ImportError:
        # Pinned openai-whisper + torch into the app's venv (raises with pip's
        # error if the install fails).
        from app.utils.system import ensure_installed

        ensure_installed("whisper", "openai-whisper", required=True, extra="whisper")
        import whisper

    print(f"Loading Whisper model '{model}'...")
    m = whisper.load_model(model)

    print(f"Transcribing {audio_path}...")
    start = time.time()
    transcribe_kwargs = {"language": language, "verbose": False}
    if initial_prompt:
        transcribe_kwargs["initial_prompt"] = initial_prompt
    result = m.transcribe(audio_path, **transcribe_kwargs)
    elapsed = time.time() - start

    transcript = Transcript(
        text=result["text"],
        language=result.get("language", ""),
    )

    for seg in result.get("segments", []):
        transcript.segments.append(
            TranscriptSegment(
                start=seg["start"],
                end=seg["end"],
                text=seg["text"],
            )
        )

    if transcript.segments:
        transcript.duration = transcript.segments[-1].end

    print(f"Transcribed in {elapsed:.1f}s ({transcript.duration:.0f}s audio)")
    return transcript


# ---------------------------------------------------------------------------
# Hugging Face token (optional; only the pyannote fallback uses it)
# ---------------------------------------------------------------------------

try:
    from app.utils.constants import HF_TOKEN_PATH, PYANNOTE_GATED_MODELS, PYANNOTE_PIPELINE_MODEL
except ImportError:
    # Fallback when running standalone (not as part of the Flask app)
    HF_TOKEN_PATH = Path(__file__).parent.parent / ".hf_token"
    PYANNOTE_PIPELINE_MODEL = "pyannote/speaker-diarization-3.1"
    PYANNOTE_GATED_MODELS = (PYANNOTE_PIPELINE_MODEL, "pyannote/segmentation-3.0")


def get_hf_token() -> str | None:
    """The saved Hugging Face token, or None."""
    try:
        token = HF_TOKEN_PATH.read_text().strip()
    except OSError:
        return None
    return token or None


def save_hf_token(token: str) -> None:
    """Write the token readable by the owner only (0600).

    Written in place (no temp file): in a dev checkout DATA_DIR is the repo
    root, where only ``.hf_token`` itself is gitignored.
    """
    path = Path(HF_TOKEN_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # O_CREAT's mode doesn't apply to an existing file
        os.write(fd, token.strip().encode())
    finally:
        os.close(fd)


def delete_hf_token() -> None:
    try:
        Path(HF_TOKEN_PATH).unlink()
    except FileNotFoundError:
        pass


def setup_diarization() -> bool:
    """Command-line setup for the optional pyannote fallback. Returns True if a token was saved."""
    models = "\n".join(f"     - https://huggingface.co/{m}" for m in PYANNOTE_GATED_MODELS)
    print("\n" + "=" * 60)
    print("  Speaker identification: optional pyannote fallback")
    print("=" * 60)
    print(f"""
  The built-in speaker model (the 'diarize' package) needs no account
  or token. A Hugging Face token only enables the pyannote fallback,
  which also needs pyannote.audio installed in this Python.

  Steps:
  1. Create a free account at https://huggingface.co/join
  2. Accept the model terms (one-time):
{models}
  3. Create a "Read" access token at https://huggingface.co/settings/tokens
  4. Paste the token below. (In the app: Settings > Processing >
     Speaker Identification.)
""")
    token = input("  HuggingFace token (hf_...): ").strip()
    if not token or not token.startswith("hf_"):
        print("  Invalid token. Must start with 'hf_'.")
        return False

    save_hf_token(token)
    print(f"  Token saved to {HF_TOKEN_PATH}")
    if not _pyannote_installed():
        print("  pyannote.audio isn't installed here; install it with pip to use the fallback.")
    print("=" * 60)
    return True


# ---------------------------------------------------------------------------
# Speaker identification
# ---------------------------------------------------------------------------


class DiarizationError(RuntimeError):
    """No speaker identification tier could run. The message is for the user."""


@dataclass
class DiarizationResult:
    """Outcome of run_diarization().

    status: "done" (speakers found), "none" (ran, found no speech/speakers)
    or "failed" (couldn't run; ``message`` says why, for the user).
    """

    status: str
    segments: list[dict] = field(default_factory=list)
    engine: str = ""
    message: str = ""


# A failed install of the 'diarize' extra isn't retried for this long, so a
# batch of auto-transcriptions doesn't run pip (up to 30 min each) per file.
_DIARIZE_INSTALL_RETRY_SECONDS = 30 * 60
_diarize_install_failed_at: float | None = None

# One diarization at a time: it saturates the CPU and loads models into RAM.
_diarize_lock = threading.Lock()


def _pyannote_installed() -> bool:
    try:
        return importlib.util.find_spec("pyannote.audio") is not None
    except (ImportError, ValueError):
        return False


def _install_soundfile_audio_loader() -> None:
    """Let torchaudio.load read audio through soundfile.

    torchaudio >= 2.9 hands decoding to torchcodec, which isn't in the pinned
    'diarize' extra (and needs FFmpeg's shared libraries, which the app
    doesn't ship). Silero VAD and WeSpeaker both read audio with
    torchaudio.load, so without this every run fails with "torchaudio ...
    requires torchcodec". soundfile (libsndfile) reads WAV, FLAC, Ogg
    Vorbis/Opus and MP3; anything else still goes to the original loader.
    """
    import torchaudio

    if getattr(torchaudio.load, "_plaud_soundfile", False):
        return
    original = torchaudio.load

    def load(uri, *args, **kwargs):
        try:
            import soundfile as sf
            import torch

            data, sample_rate = sf.read(str(uri), dtype="float32", always_2d=True)
        except Exception:
            return original(uri, *args, **kwargs)
        return torch.from_numpy(data.T.copy()), sample_rate

    load._plaud_soundfile = True
    torchaudio.load = load


def _load_diarize_lib():
    """The ``diarize.diarize`` function, installing the pinned extra on first use.

    Returns None when the package can't be imported or installed.
    """
    global _diarize_install_failed_at
    try:
        from diarize import diarize as diarize_fn
    except ImportError:
        if (
            _diarize_install_failed_at is not None
            and time.monotonic() - _diarize_install_failed_at < _DIARIZE_INSTALL_RETRY_SECONDS
        ):
            return None
        try:
            from app.utils.system import ensure_installed

            ok = ensure_installed("diarize", extra="diarize")
        except Exception as e:  # standalone run without app/, pip crash...
            log.warning("Could not install the 'diarize' extra: %s", e)
            ok = False
        if not ok:
            _diarize_install_failed_at = time.monotonic()
            return None
        _diarize_install_failed_at = None
        try:
            from diarize import diarize as diarize_fn
        except ImportError:
            return None
    _install_soundfile_audio_loader()
    return diarize_fn


def _friendly_speaker_names(segments: list[dict]) -> list[dict]:
    """Rename raw labels (SPEAKER_00...) to Speaker 1, Speaker 2... in order of appearance."""
    speaker_map: dict[str, str] = {}
    for seg in segments:
        if seg["speaker"] not in speaker_map:
            speaker_map[seg["speaker"]] = f"Speaker {len(speaker_map) + 1}"
        seg["speaker"] = speaker_map[seg["speaker"]]
    return segments


def _to_wav16k(audio_path: str) -> str | None:
    """Convert to a temporary 16 kHz mono WAV with ffmpeg. None if ffmpeg is unavailable/fails."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            from app.utils.system import ensure_ffmpeg

            ffmpeg = ensure_ffmpeg()
        except Exception:
            ffmpeg = None
    if not ffmpeg:
        return None
    fd, wav = tempfile.mkstemp(prefix="plaud-diarize-", suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", audio_path, "-ac", "1", "-ar", "16000", wav],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=600,
        )
        return wav
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("ffmpeg could not convert %s for diarization: %s", audio_path, e)
        try:
            os.unlink(wav)
        except OSError:
            pass
        return None


def _run_diarize_lib(diarize_fn, audio_path: str) -> list[dict]:
    """Run the 'diarize' package. Returns [{start, end, speaker}] with friendly names."""
    print("  Running speaker identification (diarize, CPU)...")
    start = time.time()
    wav = _to_wav16k(audio_path)
    try:
        result = diarize_fn(wav or audio_path)
    finally:
        if wav:
            try:
                os.unlink(wav)
            except OSError:
                pass
    segments = _friendly_speaker_names([dict(s) for s in result.to_list()])
    speakers = {s["speaker"] for s in segments}
    print(f"  Diarization: {len(speakers)} speaker(s) found in {time.time() - start:.1f}s (diarize)")
    return segments


def _diarize_audio_pyannote(audio_path: str, hf_token: str) -> list[dict]:
    """Run pyannote speaker diarization. Returns list of {start, end, speaker}."""
    import torch
    from pyannote.audio import Pipeline

    # Use Apple Silicon GPU if available, otherwise CPU with max threads
    if torch.backends.mps.is_available():
        device = torch.device("mps")
        print("  Loading diarization model (Apple GPU)...")
    else:
        device = torch.device("cpu")
        torch.set_num_threads(os.cpu_count() or 4)
        print(f"  Loading diarization model (CPU, {os.cpu_count()} threads)...")

    pipeline = Pipeline.from_pretrained(PYANNOTE_PIPELINE_MODEL, token=hf_token)
    pipeline.to(device)

    print("  Identifying speakers...")
    start = time.time()
    result = pipeline(audio_path)
    elapsed = time.time() - start

    # pyannote 4.x returns DiarizeOutput; extract the Annotation
    annotation = getattr(result, "speaker_diarization", result)

    segments = [
        {"start": turn.start, "end": turn.end, "speaker": speaker}
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    ]
    segments = _friendly_speaker_names(segments)
    speakers = {s["speaker"] for s in segments}
    print(f"  Diarization: {len(speakers)} speaker(s) found in {elapsed:.1f}s (pyannote)")
    return segments


def _short(err: Exception, limit: int = 160) -> str:
    text = str(err).strip().splitlines()[0] if str(err).strip() else type(err).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pyannote_error_message(err: Exception) -> str:
    text = str(err)
    if "401" in text or "403" in text or "gated" in text.lower() or "access" in text.lower():
        models = " and ".join(PYANNOTE_GATED_MODELS)
        return f"Hugging Face refused the pyannote models: accept the terms for {models} on huggingface.co"
    return f"pyannote failed ({_short(err)})"


def diarize_audio(audio_path: str, hf_token: str | None = None) -> tuple[list[dict], str]:
    """Identify speakers. Returns ``(segments, engine)``; segments are {start, end, speaker}.

    Tier 1, the 'diarize' package, needs no token. Tier 2, pyannote, runs only
    if tier 1 couldn't, a Hugging Face token is given and pyannote.audio is
    installed. Raises DiarizationError (message for the user) if no tier ran.
    """
    reasons = []
    diarize_fn = _load_diarize_lib()
    if diarize_fn is not None:
        try:
            return _run_diarize_lib(diarize_fn, audio_path), "diarize"
        except Exception as e:
            log.warning("diarize failed for %s: %s", audio_path, e, exc_info=True)
            reasons.append(f"speaker detection failed ({_short(e)})")
    else:
        reasons.append(
            "the speaker detection package couldn't be installed (check your internet connection, then try again)"
        )

    if hf_token and _pyannote_installed():
        try:
            return _diarize_audio_pyannote(audio_path, hf_token), "pyannote"
        except Exception as e:
            log.warning("pyannote failed for %s: %s", audio_path, e, exc_info=True)
            reasons.append(_pyannote_error_message(e))

    raise DiarizationError("; ".join(reasons))


def known_unavailable_reason() -> str | None:
    """Why diarization can't run right now, if that's already known without trying.

    True only after a recent failed install of the 'diarize' extra with no
    pyannote fallback available, so callers can skip a pointless background
    job and tell the user straight away.
    """
    if _diarize_install_failed_at is None:
        return None
    if time.monotonic() - _diarize_install_failed_at >= _DIARIZE_INSTALL_RETRY_SECONDS:
        return None
    if importlib.util.find_spec("diarize") is not None:
        return None
    if get_hf_token() and _pyannote_installed():
        return None
    return (
        "Speaker labels skipped: the speaker detection package couldn't be installed "
        "(check your internet connection, then try again)"
    )


def run_diarization(audio_path: str) -> DiarizationResult:
    """Identify speakers in *audio_path* and never raise: the result says what happened."""
    with _diarize_lock:
        try:
            segments, engine = diarize_audio(audio_path, get_hf_token())
        except DiarizationError as e:
            print(f"  Speaker labels skipped: {e}")
            return DiarizationResult("failed", message=f"Speaker labels skipped: {e}")
        except Exception as e:  # defensive: diarize_audio wraps tier errors already
            log.warning("Diarization crashed for %s", audio_path, exc_info=True)
            return DiarizationResult("failed", message=f"Speaker labels skipped: {_short(e)}")
    if not segments:
        print("  No speakers detected (no speech found, or very short audio)")
        return DiarizationResult("none", engine=engine, message="No speakers detected in this recording")
    return DiarizationResult("done", segments=segments, engine=engine)


def merge_transcript_with_diarization(transcript: Transcript, diarization: list[dict]) -> Transcript:
    """Assign speaker labels to transcript segments based on overlap."""
    for seg in transcript.segments:
        best_speaker = ""
        best_overlap = 0
        for d in diarization:
            # Calculate overlap between transcript segment and diarization segment
            overlap_start = max(seg.start, d["start"])
            overlap_end = min(seg.end, d["end"])
            overlap = max(0, overlap_end - overlap_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = d["speaker"]
        if best_speaker:
            seg.speaker = best_speaker

    # Rename speakers to friendly names (Speaker 1, Speaker 2, ...)
    speaker_map = {}
    counter = 1
    for seg in transcript.segments:
        if seg.speaker and seg.speaker not in speaker_map:
            speaker_map[seg.speaker] = f"Speaker {counter}"
            counter += 1
        if seg.speaker:
            seg.speaker = speaker_map[seg.speaker]

    # Merge consecutive segments with the same speaker into sentence-based blocks
    transcript.segments = _merge_speaker_segments(transcript.segments)

    return transcript


def _split_long_segment(seg: TranscriptSegment, max_duration: float = 30.0) -> list[TranscriptSegment]:
    """Split a single long segment at sentence boundaries."""
    import re

    text = seg.text.strip()
    duration = seg.end - seg.start

    if duration <= max_duration:
        return [seg]

    # Find all sentence boundaries
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) <= 1:
        return [seg]  # Can't split — single sentence

    # Distribute time proportionally by character count
    total_chars = sum(len(s) for s in sentences)
    if total_chars == 0:
        return [seg]

    result = []
    current_start = seg.start
    current_text = ""
    current_chars = 0

    for sentence in sentences:
        current_text = (current_text + " " + sentence).strip() if current_text else sentence
        current_chars += len(sentence)
        current_end = seg.start + (current_chars / total_chars) * duration
        block_dur = current_end - current_start

        if block_dur >= max_duration or sentence == sentences[-1]:
            result.append(
                TranscriptSegment(
                    start=round(current_start, 2),
                    end=round(current_end, 2),
                    text=current_text,
                    speaker=seg.speaker,
                )
            )
            current_start = current_end
            current_text = ""

    return result if result else [seg]


def _merge_speaker_segments(segments: list[TranscriptSegment], max_duration: float = 30.0) -> list[TranscriptSegment]:
    """Merge consecutive segments with the same speaker, then split long blocks at sentences.

    1. First merge consecutive same-speaker segments
    2. Split at sentence boundaries when block > 10s
    3. Split any remaining blocks > max_duration at sentence boundaries
    """
    if not segments:
        return segments

    # Step 1: Merge consecutive same-speaker segments
    merged = []
    current = TranscriptSegment(
        start=segments[0].start,
        end=segments[0].end,
        text=segments[0].text.strip(),
        speaker=segments[0].speaker,
    )

    for seg in segments[1:]:
        same_speaker = seg.speaker == current.speaker
        block_duration = seg.end - current.start
        ends_sentence = current.text.rstrip().endswith((".", "!", "?"))

        should_split = not same_speaker or (ends_sentence and block_duration > 10)

        if should_split:
            merged.append(current)
            current = TranscriptSegment(
                start=seg.start,
                end=seg.end,
                text=seg.text.strip(),
                speaker=seg.speaker,
            )
        else:
            current.end = seg.end
            current.text = current.text.strip() + " " + seg.text.strip()

    merged.append(current)

    # Step 2: Split any blocks that are still too long
    final = []
    for seg in merged:
        final.extend(_split_long_segment(seg, max_duration))

    return final


def transcribe(
    audio_path: str,
    model: str = "base",
    diarize: bool = False,
    language: str | None = None,
    initial_prompt: str | None = None,
) -> Transcript:
    """Transcribe an audio file, optionally with speaker identification.

    Args:
        audio_path: Path to audio file (mp3, wav, opus, ogg, etc.)
        model: Whisper model name (base/small/medium/turbo/large)
        diarize: Label speakers (no token needed; see diarize_audio). If it
            can't run, the transcript is returned without speakers and
            ``diarization_status``/``diarization_message`` say why.
        language: Force language (e.g., "en"); None auto-detects
    """
    transcript = transcribe_with_whisper(audio_path, model=model, language=language, initial_prompt=initial_prompt)

    if diarize and transcript.text:
        result = run_diarization(audio_path)
        transcript.diarization_status = result.status
        transcript.diarization_message = result.message
        if result.status == "done":
            transcript = merge_transcript_with_diarization(transcript, result.segments)

    return transcript


def transcribe_directory(
    directory: str,
    output_dir: str | None = None,
    model: str = "base",
    diarize: bool = False,
) -> list[tuple[str, Transcript]]:
    """Transcribe all audio files in a directory."""
    audio_extensions = {".mp3", ".wav", ".opus", ".m4a", ".ogg", ".flac"}
    dir_path = Path(directory)
    out_path = Path(output_dir) if output_dir else dir_path / "transcripts"
    out_path.mkdir(parents=True, exist_ok=True)

    audio_files = [f for f in sorted(dir_path.iterdir()) if f.suffix.lower() in audio_extensions]

    if not audio_files:
        print(f"No audio files found in {directory}")
        return []

    print(f"Found {len(audio_files)} audio file(s) to transcribe")
    results = []

    for i, audio_file in enumerate(audio_files):
        transcript_path = out_path / (audio_file.stem + ".json")
        formatted_path = out_path / (audio_file.stem + ".txt")

        if transcript_path.exists():
            print(f"[{i + 1}/{len(audio_files)}] Skipping (exists): {audio_file.name}")
            t = Transcript.load(str(transcript_path))
            results.append((str(audio_file), t))
            continue

        print(f"\n[{i + 1}/{len(audio_files)}] {audio_file.name}")
        t = transcribe(str(audio_file), model=model, diarize=diarize)

        if t.text:
            t.save(str(transcript_path))
            formatted_path.write_text(t.formatted, encoding="utf-8")
            print(f"  Saved: {transcript_path.name}")

        results.append((str(audio_file), t))

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Transcribe audio files locally")
    parser.add_argument("path", nargs="?", help="Audio file or directory path")
    parser.add_argument("--model", default="small", help="Whisper model: base/small/medium/turbo/large (default: small)")
    parser.add_argument("--diarize", action="store_true", help="Label speakers (Speaker 1, Speaker 2...)")
    parser.add_argument(
        "--setup-diarize", action="store_true", help="Save a Hugging Face token for the optional pyannote fallback"
    )
    parser.add_argument("--language", default=None, help="Force language (e.g., en)")
    parser.add_argument("--all", action="store_true", help="Transcribe all files in directory")
    parser.add_argument("--output", default=None, help="Output directory for transcripts")

    args = parser.parse_args()

    if args.setup_diarize:
        setup_diarization()
        sys.exit(0)

    if not args.path:
        parser.print_help()
        sys.exit(1)

    path = Path(args.path)

    if path.is_dir() or args.all:
        results = transcribe_directory(
            str(path),
            output_dir=args.output,
            model=args.model,
            diarize=args.diarize,
        )
        print(f"\nTranscribed {len(results)} file(s)")
    elif path.is_file():
        t = transcribe(
            str(path),
            model=args.model,
            diarize=args.diarize,
            language=args.language,
        )
        if t.text:
            out = str(path.with_suffix(".json"))
            t.save(out)
            print(f"\nSaved: {out}")
            print(f"\n--- Transcript ---\n{t.formatted[:2000]}")
    else:
        print(f"Path not found: {path}")
