"""Tests for API key validation."""

from unittest.mock import MagicMock, patch

from app.providers.validate import validate_key


def test_validate_empty_key():
    """Empty key returns invalid."""
    valid, error = validate_key("openai", "")
    assert valid is False
    assert error == "No key provided"


def test_validate_unknown_provider():
    """Unknown provider returns error."""
    valid, error = validate_key("unknown_provider", "sk-test123")
    assert valid is False
    assert error == "Unknown provider"


@patch("app.providers.validate.http_requests")
def test_validate_openai_valid(mock_requests):
    """Valid OpenAI key returns True."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_requests.get.return_value = mock_resp

    valid, error = validate_key("openai", "sk-test123")
    assert valid is True
    assert error is None


@patch("app.providers.validate.http_requests")
def test_validate_openai_invalid(mock_requests):
    """Invalid OpenAI key returns False with error."""
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_requests.get.return_value = mock_resp

    valid, error = validate_key("openai", "sk-bad")
    assert valid is False
    assert "Invalid" in error


@patch("app.providers.validate.http_requests")
def test_validate_anthropic_valid(mock_requests):
    """Valid Anthropic key returns True."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_requests.post.return_value = mock_resp

    valid, error = validate_key("anthropic", "sk-ant-test123")
    assert valid is True
    assert error is None


@patch("app.providers.validate.http_requests")
def test_validate_anthropic_forbidden(mock_requests):
    """Forbidden Anthropic key returns appropriate error."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_requests.post.return_value = mock_resp

    valid, error = validate_key("anthropic", "sk-ant-bad")
    assert valid is False
    assert "permissions" in error.lower()
