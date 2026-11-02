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


def _anthropic_status_error(cls, status):
    import httpx2

    req = httpx2.Request("GET", "https://api.anthropic.com/v1/models")
    return cls("err", response=httpx2.Response(status, request=req), body=None)


@patch("app.providers.validate.anthropic.Anthropic")
def test_validate_anthropic_valid_uses_free_models_endpoint(mock_cls):
    """Valid Anthropic key: checked with GET /v1/models (no tokens spent)."""
    valid, error = validate_key("anthropic", "sk-ant-test123")
    assert valid is True
    assert error is None
    mock_cls.assert_called_once()
    assert mock_cls.call_args.kwargs["api_key"] == "sk-ant-test123"
    mock_cls.return_value.models.list.assert_called_once_with(limit=1)
    mock_cls.return_value.messages.create.assert_not_called()


@patch("app.providers.validate.anthropic.Anthropic")
def test_validate_anthropic_invalid(mock_cls):
    import anthropic

    mock_cls.return_value.models.list.side_effect = _anthropic_status_error(anthropic.AuthenticationError, 401)
    valid, error = validate_key("anthropic", "sk-ant-bad")
    assert valid is False
    assert "Invalid" in error


@patch("app.providers.validate.anthropic.Anthropic")
def test_validate_anthropic_forbidden(mock_cls):
    """Forbidden Anthropic key returns appropriate error."""
    import anthropic

    mock_cls.return_value.models.list.side_effect = _anthropic_status_error(anthropic.PermissionDeniedError, 403)
    valid, error = validate_key("anthropic", "sk-ant-bad")
    assert valid is False
    assert "permissions" in error.lower()
