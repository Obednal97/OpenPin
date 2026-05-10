"""Tests for recording date extraction."""

from datetime import datetime

from app.utils.recording_utils import extract_recording_date


def testextract_recording_date_unix():
    """Unix timestamp session ID → correct datetime."""
    result = extract_recording_date("1774605121")
    assert result is not None
    assert isinstance(result, datetime)
    assert result.year >= 2026


def testextract_recording_date_cloud():
    """cloud_2026-03-27_1234_... → correct datetime."""
    result = extract_recording_date("cloud_2026-03-27_1234_recording_name")
    assert result is not None
    assert result.year == 2026
    assert result.month == 3
    assert result.day == 27
    assert result.hour == 12
    assert result.minute == 34


def testextract_recording_date_iso():
    """Filename with YYYY-MM-DD pattern."""
    result = extract_recording_date("meeting_2026-01-15_notes")
    assert result is not None
    assert result.year == 2026
    assert result.month == 1
    assert result.day == 15


def testextract_recording_date_unknown():
    """Unrecognized format returns None."""
    result = extract_recording_date("random_name_no_date")
    assert result is None


def testextract_recording_date_invalid_unix():
    """Out-of-range unix timestamp returns None."""
    result = extract_recording_date("999")
    assert result is None
