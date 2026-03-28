"""Tests for transcription utilities — segment merging, splitting, date extraction."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.transcribe import (
    Transcript,
    TranscriptSegment,
    _format_time,
    _merge_speaker_segments,
    _split_long_segment,
)


def test_merge_same_speaker():
    """Consecutive same-speaker segments merge into one."""
    segments = [
        TranscriptSegment(start=0, end=5, text="Hello there.", speaker="A"),
        TranscriptSegment(start=5, end=8, text="How are you?", speaker="A"),
    ]
    # Block is < 10s so they merge
    result = _merge_speaker_segments(segments)
    assert len(result) == 1
    assert result[0].speaker == "A"
    assert "Hello there." in result[0].text
    assert "How are you?" in result[0].text


def test_merge_speaker_change():
    """Different speakers don't merge."""
    segments = [
        TranscriptSegment(start=0, end=5, text="Hello.", speaker="A"),
        TranscriptSegment(start=5, end=10, text="Hi there.", speaker="B"),
    ]
    result = _merge_speaker_segments(segments)
    assert len(result) == 2
    assert result[0].speaker == "A"
    assert result[1].speaker == "B"


def test_merge_empty():
    """Empty segments list returns empty."""
    result = _merge_speaker_segments([])
    assert result == []


def test_split_long_segment():
    """A long segment splits at sentence boundaries."""
    long_text = (
        "First sentence is here. Second sentence follows. Third sentence comes along. Fourth and final sentence."
    )
    seg = TranscriptSegment(start=0, end=120, text=long_text, speaker="A")
    result = _split_long_segment(seg, max_duration=30)
    assert len(result) > 1
    # Each result should have the same speaker
    for r in result:
        assert r.speaker == "A"
    # Reconstructed text should contain all sentences
    full = " ".join(r.text for r in result)
    assert "First sentence" in full
    assert "final sentence" in full


def test_split_preserves_short_segment():
    """Short segment returns unchanged."""
    seg = TranscriptSegment(start=0, end=10, text="Short text.", speaker="A")
    result = _split_long_segment(seg, max_duration=30)
    assert len(result) == 1
    assert result[0].text == "Short text."


def test_split_single_sentence():
    """Single sentence can't be split — returns as-is."""
    seg = TranscriptSegment(start=0, end=60, text="One very long sentence without any breaks", speaker="A")
    result = _split_long_segment(seg, max_duration=30)
    assert len(result) == 1


def test_merge_splits_at_sentence_boundary_after_10s():
    """Same speaker, but splits at sentence boundary when block > 10s."""
    segments = [
        TranscriptSegment(start=0, end=6, text="First part of the speech.", speaker="A"),
        TranscriptSegment(start=6, end=12, text="Second part continues.", speaker="A"),
    ]
    result = _merge_speaker_segments(segments)
    # Block would be 12s and first chunk ends with '.', so should split
    assert len(result) == 2


def test_format_time():
    """_format_time produces correct MM:SS format."""
    assert _format_time(0) == "00:00"
    assert _format_time(65) == "01:05"
    assert _format_time(3661) == "1:01:01"


def test_transcript_save_load(tmp_path):
    """Transcript save/load roundtrip."""
    t = Transcript(
        text="Hello world",
        language="en",
        duration=10.5,
        segments=[
            TranscriptSegment(start=0, end=5, text="Hello", speaker="A"),
            TranscriptSegment(start=5, end=10.5, text="world", speaker="B"),
        ],
    )
    path = str(tmp_path / "test.json")
    t.save(path)
    loaded = Transcript.load(path)
    assert loaded.text == "Hello world"
    assert loaded.language == "en"
    assert loaded.duration == 10.5
    assert len(loaded.segments) == 2
    assert loaded.segments[0].speaker == "A"
    assert loaded.segments[1].text == "world"


def test_transcript_formatted():
    """formatted property produces readable output."""
    t = Transcript(
        segments=[
            TranscriptSegment(start=0, end=5, text="Hello there", speaker="Alice"),
            TranscriptSegment(start=5, end=10, text="Hi Bob", speaker=""),
        ]
    )
    formatted = t.formatted
    assert "[Alice]" in formatted
    assert "Hello there" in formatted
    assert "Hi Bob" in formatted
