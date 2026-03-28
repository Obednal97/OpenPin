"""Tests for summarization utilities."""

from app.providers.summarize import sanitize_for_json


def test_sanitize_for_json_special_chars():
    """Special characters are escaped correctly."""
    text = 'He said "hello"\nNew line\tTab\\Backslash'
    result = sanitize_for_json(text)
    assert '\\"' in result
    assert "\\n" not in result  # newlines are replaced with space
    assert "\\\\" in result
    assert "\t" not in result


def test_sanitize_for_json_empty():
    """Empty string returns empty."""
    assert sanitize_for_json("") == ""


def test_sanitize_for_json_normal_text():
    """Normal text passes through unchanged."""
    text = "This is a normal sentence without special characters"
    assert sanitize_for_json(text) == text
