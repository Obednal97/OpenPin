"""Local transcription using OpenAI Whisper and optional speaker diarization.

Supports two backends:
1. whisper (openai-whisper) - CPU/MPS, simple setup
2. insanely-fast-whisper - faster on Apple Silicon, supports diarization

Usage:
    python -m src.transcribe path/to/audio.mp3
    python -m src.transcribe path/to/audio.mp3 --diarize
    python -m src.transcribe cloud_recordings/ --all
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field


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
            t.segments.append(TranscriptSegment(
                start=seg["start"],
                end=seg["end"],
                text=seg["text"],
                speaker=seg.get("speaker", ""),
            ))
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
) -> Transcript:
    """Transcribe using openai-whisper (simpler, works everywhere)."""
    try:
        import whisper
    except ImportError:
        print("Installing openai-whisper...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                             "--break-system-packages", "openai-whisper"])
        import whisper

    print(f"Loading Whisper model '{model}'...")
    m = whisper.load_model(model)

    print(f"Transcribing {audio_path}...")
    start = time.time()
    result = m.transcribe(
        audio_path,
        language=language,
        verbose=False,
    )
    elapsed = time.time() - start

    transcript = Transcript(
        text=result["text"],
        language=result.get("language", ""),
    )

    for seg in result.get("segments", []):
        transcript.segments.append(TranscriptSegment(
            start=seg["start"],
            end=seg["end"],
            text=seg["text"],
        ))

    if transcript.segments:
        transcript.duration = transcript.segments[-1].end

    print(f"Transcribed in {elapsed:.1f}s ({transcript.duration:.0f}s audio)")
    return transcript


def transcribe_with_insanely_fast_whisper(
    audio_path: str,
    model: str = "distil-whisper/large-v2",
    diarize: bool = False,
    hf_token: str | None = None,
) -> Transcript:
    """Transcribe using insanely-fast-whisper (faster on Apple Silicon).

    Requires: pip install insanely-fast-whisper
    For diarization: requires HuggingFace token with pyannote access
    """
    output_path = audio_path + ".whisper.json"

    cmd = [
        sys.executable, "-m", "insanely_fast_whisper",
        "--file-name", audio_path,
        "--model-name", model,
        "--transcript-path", output_path,
        "--device-id", "mps",  # Apple Silicon
        "--batch-size", "4",
    ]

    if diarize:
        cmd.extend(["--diarize", "True"])
        if hf_token:
            cmd.extend(["--hf-token", hf_token])

    print(f"Running insanely-fast-whisper on {audio_path}...")
    start = time.time()

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        print("insanely-fast-whisper not found. Install with:")
        print("  pip install insanely-fast-whisper")
        return Transcript()
    except subprocess.CalledProcessError as e:
        print(f"Transcription failed: {e.stderr}")
        return Transcript()

    elapsed = time.time() - start

    # Parse output
    with open(output_path) as f:
        result = json.load(f)

    transcript = Transcript(
        text=result.get("text", ""),
    )

    for chunk in result.get("chunks", result.get("segments", [])):
        ts = chunk.get("timestamp", [0, 0])
        transcript.segments.append(TranscriptSegment(
            start=ts[0] if ts[0] else 0,
            end=ts[1] if ts[1] else 0,
            text=chunk.get("text", ""),
            speaker=chunk.get("speaker", ""),
        ))

    if transcript.segments:
        transcript.duration = max(s.end for s in transcript.segments if s.end)

    print(f"Transcribed in {elapsed:.1f}s ({transcript.duration:.0f}s audio)")

    # Clean up temp file
    try:
        os.remove(output_path)
    except OSError:
        pass

    return transcript


def transcribe(
    audio_path: str,
    model: str = "base",
    backend: str = "whisper",
    diarize: bool = False,
    language: str | None = None,
    hf_token: str | None = None,
) -> Transcript:
    """Transcribe an audio file.

    Args:
        audio_path: Path to audio file (mp3, wav, opus, etc.)
        model: Model name (default "base" for whisper, "distil-whisper/large-v2" for fast)
        backend: "whisper" or "fast" (insanely-fast-whisper)
        diarize: Enable speaker diarization (fast backend only)
        language: Force language (e.g., "en")
        hf_token: HuggingFace token for diarization
    """
    if backend == "fast":
        if model == "base":
            model = "distil-whisper/large-v2"
        return transcribe_with_insanely_fast_whisper(
            audio_path, model=model, diarize=diarize, hf_token=hf_token,
        )
    else:
        return transcribe_with_whisper(
            audio_path, model=model, language=language,
        )


def transcribe_directory(
    directory: str,
    output_dir: str | None = None,
    backend: str = "whisper",
    model: str = "base",
    diarize: bool = False,
) -> list[tuple[str, Transcript]]:
    """Transcribe all audio files in a directory."""
    audio_extensions = {".mp3", ".wav", ".opus", ".m4a", ".ogg", ".flac"}
    dir_path = Path(directory)
    out_path = Path(output_dir) if output_dir else dir_path / "transcripts"
    out_path.mkdir(parents=True, exist_ok=True)

    audio_files = [
        f for f in sorted(dir_path.iterdir())
        if f.suffix.lower() in audio_extensions
    ]

    if not audio_files:
        print(f"No audio files found in {directory}")
        return []

    print(f"Found {len(audio_files)} audio file(s) to transcribe")
    results = []

    for i, audio_file in enumerate(audio_files):
        transcript_path = out_path / (audio_file.stem + ".json")
        formatted_path = out_path / (audio_file.stem + ".txt")

        if transcript_path.exists():
            print(f"[{i+1}/{len(audio_files)}] Skipping (exists): {audio_file.name}")
            t = Transcript.load(str(transcript_path))
            results.append((str(audio_file), t))
            continue

        print(f"\n[{i+1}/{len(audio_files)}] {audio_file.name}")
        t = transcribe(str(audio_file), model=model, backend=backend, diarize=diarize)

        if t.text:
            t.save(str(transcript_path))
            formatted_path.write_text(t.formatted, encoding="utf-8")
            print(f"  Saved: {transcript_path.name}")

        results.append((str(audio_file), t))

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Transcribe audio files locally")
    parser.add_argument("path", help="Audio file or directory path")
    parser.add_argument("--model", default="base", help="Whisper model (default: base)")
    parser.add_argument("--backend", choices=["whisper", "fast"], default="whisper",
                       help="Transcription backend (default: whisper)")
    parser.add_argument("--diarize", action="store_true", help="Enable speaker diarization")
    parser.add_argument("--language", default=None, help="Force language (e.g., en)")
    parser.add_argument("--all", action="store_true", help="Transcribe all files in directory")
    parser.add_argument("--output", default=None, help="Output directory for transcripts")

    args = parser.parse_args()
    path = Path(args.path)

    if path.is_dir() or args.all:
        results = transcribe_directory(
            str(path), output_dir=args.output,
            backend=args.backend, model=args.model, diarize=args.diarize,
        )
        print(f"\nTranscribed {len(results)} file(s)")
    elif path.is_file():
        t = transcribe(
            str(path), model=args.model, backend=args.backend,
            diarize=args.diarize, language=args.language,
        )
        if t.text:
            out = str(path.with_suffix(".json"))
            t.save(out)
            print(f"\nSaved: {out}")
            print(f"\n--- Transcript ---\n{t.formatted[:2000]}")
    else:
        print(f"Path not found: {path}")
