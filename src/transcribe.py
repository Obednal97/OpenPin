"""Local transcription using OpenAI Whisper and optional speaker diarization.

Supports two backends:
1. whisper (openai-whisper) - CPU/MPS, simple setup
2. insanely-fast-whisper - faster on Apple Silicon, supports diarization

Usage:
    python -m src.transcribe path/to/audio.mp3
    python -m src.transcribe path/to/audio.mp3 --diarize
    python -m src.transcribe cloud_recordings/ --all
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


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
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def load(path: str) -> "Transcript":
        """Load transcript from JSON."""
        with open(path) as f:
            data = json.load(f)
        t = Transcript(
            text=data.get("text", ""),
            language=data.get("language", ""),
            duration=data.get("duration", 0),
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
        print("Installing openai-whisper...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--break-system-packages", "openai-whisper"])
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


# Use the canonical DATA_DIR for the HF token — same path the settings UI writes to
try:
    from app.utils.constants import HF_TOKEN_PATH
except ImportError:
    # Fallback when running standalone (not as part of the Flask app)
    HF_TOKEN_PATH = Path(__file__).parent.parent / ".hf_token"


def _get_hf_token() -> str | None:
    """Read cached HuggingFace token."""
    if HF_TOKEN_PATH.exists():
        token = HF_TOKEN_PATH.read_text().strip()
        if token:
            return token
    return None


def _save_hf_token(token: str):
    HF_TOKEN_PATH.write_text(token)


def setup_diarization() -> bool:
    """Interactive setup for speaker diarization. Returns True if ready."""
    print("\n" + "=" * 60)
    print("  Speaker Diarization Setup")
    print("=" * 60)
    print("""
  Speaker diarization identifies WHO is speaking (Speaker 1,
  Speaker 2, etc.) throughout a recording. It uses pyannote.audio,
  which requires a free HuggingFace account.

  Steps:
  1. Create a free account at https://huggingface.co/join
  2. Accept the model licenses (one-time, instant):
     - https://huggingface.co/pyannote/segmentation-3.0
     - https://huggingface.co/pyannote/speaker-diarization-3.1
     (Click "Agree and access repository" on each page)
  3. Create an access token at:
     https://huggingface.co/settings/tokens
     (Click "New token", name it anything, select "Read")
  4. Paste the token below.
""")
    token = input("  HuggingFace token (hf_...): ").strip()
    if not token or not token.startswith("hf_"):
        print("  Invalid token. Must start with 'hf_'.")
        return False

    _save_hf_token(token)
    print(f"  Token saved to {HF_TOKEN_PATH}")

    # Install pyannote if needed
    try:
        import pyannote.audio  # noqa: F401 — checking availability

        print("  pyannote.audio already installed")
    except ImportError:
        print("  Installing pyannote.audio (this may take a minute)...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--break-system-packages", "pyannote.audio"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("  Installed!")
        except subprocess.CalledProcessError:
            print("  Installation failed. Try manually: pip install pyannote.audio")
            return False

    print("\n  Diarization is ready! Use --diarize to identify speakers.")
    print("=" * 60)
    return True


def diarize_audio_fast(audio_path: str, hf_token: str = "") -> list[dict]:
    """Run diarization using the 'diarize' library (fast, no API key needed).

    Returns list of {start, end, speaker} with friendly speaker names.
    The hf_token parameter is accepted for API compatibility but not used.
    """
    from diarize import diarize as _diarize

    print("  Running fast diarization (diarize library, CPU)...")
    start = time.time()
    result = _diarize(audio_path)
    elapsed = time.time() - start

    # Convert to list of dicts (already in {start, end, speaker} format)
    segments = result.to_list()

    # Rename speakers to friendly names (Speaker 1, Speaker 2, ...)
    speaker_map = {}
    counter = 1
    for seg in segments:
        if seg["speaker"] not in speaker_map:
            speaker_map[seg["speaker"]] = f"Speaker {counter}"
            counter += 1
        seg["speaker"] = speaker_map[seg["speaker"]]

    speakers = {s["speaker"] for s in segments}
    print(f"  Diarization: {len(speakers)} speaker(s) found in {elapsed:.1f}s (fast)")
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

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )
    pipeline.to(device)

    print("  Identifying speakers...")
    start = time.time()
    result = pipeline(audio_path)
    elapsed = time.time() - start

    # pyannote 4.x returns DiarizeOutput; extract the Annotation
    annotation = getattr(result, "speaker_diarization", result)

    segments = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        segments.append(
            {
                "start": turn.start,
                "end": turn.end,
                "speaker": speaker,
            }
        )

    speakers = {s["speaker"] for s in segments}
    print(f"  Diarization: {len(speakers)} speaker(s) found in {elapsed:.1f}s")
    return segments


def _diarize_audio_onnx(audio_path: str, hf_token: str) -> list[dict]:
    """Run diarization using pyannote-onnx-extended (faster than PyTorch pyannote, no GPU needed)."""
    from pyannote_onnx_extended import Pipeline as OnnxPipeline

    print("  Loading diarization model (ONNX Runtime)...")
    pipeline = OnnxPipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )

    print("  Identifying speakers (ONNX)...")
    start = time.time()
    result = pipeline(audio_path)
    elapsed = time.time() - start

    annotation = getattr(result, "speaker_diarization", result)
    segments = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        segments.append({"start": turn.start, "end": turn.end, "speaker": speaker})

    # Rename to friendly names
    speaker_map = {}
    counter = 1
    for seg in segments:
        if seg["speaker"] not in speaker_map:
            speaker_map[seg["speaker"]] = f"Speaker {counter}"
            counter += 1
        seg["speaker"] = speaker_map[seg["speaker"]]

    speakers = {s["speaker"] for s in segments}
    print(f"  Diarization: {len(speakers)} speaker(s) found in {elapsed:.1f}s (ONNX)")
    return segments


def diarize_audio(audio_path: str, hf_token: str) -> list[dict]:
    """Run speaker diarization with 3-tier cascade:
    1. 'diarize' library (7x faster, CPU, no API key)
    2. pyannote-onnx-extended (faster than PyTorch, no GPU)
    3. pyannote standard (PyTorch, MPS/CPU)

    Returns list of {start, end, speaker}.
    """
    # Tier 1: Fast diarize library
    try:
        return diarize_audio_fast(audio_path, hf_token)
    except ImportError:
        print("  Fast diarize library not installed, trying next...")
    except Exception as e:
        print(f"  Fast diarize failed ({e}), trying next...")

    # Tier 2: pyannote ONNX (faster than standard PyTorch)
    try:
        return _diarize_audio_onnx(audio_path, hf_token)
    except ImportError:
        print("  pyannote-onnx-extended not installed, trying standard pyannote...")
    except Exception as e:
        print(f"  ONNX diarization failed ({e}), trying standard pyannote...")

    # Tier 3: Standard pyannote (PyTorch, MPS/CPU)
    return _diarize_audio_pyannote(audio_path, hf_token)


def merge_transcript_with_diarization(transcript: Transcript, diarization: list[dict]) -> Transcript:
    """Assign speaker labels to transcript segments based on overlap."""
    for seg in transcript.segments:
        (seg.start + seg.end) / 2
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
    """Transcribe an audio file, optionally with speaker diarization.

    Args:
        audio_path: Path to audio file (mp3, wav, opus, ogg, etc.)
        model: Whisper model name (base/small/medium/turbo/large)
        diarize: Enable speaker diarization (requires HuggingFace token)
        language: Force language (e.g., "en"); None auto-detects
    """
    # Transcribe with Whisper
    transcript = transcribe_with_whisper(audio_path, model=model, language=language, initial_prompt=initial_prompt)

    # Diarize if requested — graceful fallback at every stage
    if diarize and transcript.text:
        hf_token = _get_hf_token()
        if not hf_token:
            print("  Speaker diarization skipped — no HuggingFace token configured.")
            print("  To enable: python3 sync_and_transcribe.py --setup-diarize")
        else:
            try:
                import pyannote.audio  # noqa: F401
            except ImportError:
                print("  Speaker diarization skipped — pyannote.audio not installed.")
                print("  To install: pip install pyannote.audio")
                hf_token = None

            if hf_token:
                try:
                    diar_segments = diarize_audio(audio_path, hf_token)
                    if diar_segments:
                        transcript = merge_transcript_with_diarization(transcript, diar_segments)
                    else:
                        print("  No speakers detected (single speaker or very short audio)")
                except Exception as e:
                    err = str(e)
                    if "401" in err or "403" in err or "access" in err.lower():
                        print("  Speaker diarization failed — HuggingFace token rejected.")
                        print("  Check that you accepted the model licenses:")
                        print("    https://huggingface.co/pyannote/segmentation-3.0")
                        print("    https://huggingface.co/pyannote/speaker-diarization-3.1")
                    else:
                        print(f"  Speaker diarization failed: {err}")
                    print("  (Transcript saved without speaker labels)")

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
    parser.add_argument("--diarize", action="store_true", help="Enable speaker diarization")
    parser.add_argument("--setup-diarize", action="store_true", help="Set up diarization (HuggingFace token)")
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
