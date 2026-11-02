"""API key validation."""

import requests as http_requests

try:  # Official Anthropic SDK (also used for summaries).
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None


def _validate_anthropic(key):
    """Check an Anthropic key with GET /v1/models — free, no tokens used."""
    if anthropic is None:
        return False, "The Anthropic SDK (Python package 'anthropic') isn't installed"
    client = anthropic.Anthropic(api_key=key, timeout=10.0, max_retries=0)
    try:
        client.models.list(limit=1)
        return True, None
    except anthropic.AuthenticationError:
        return False, "Invalid API key"
    except anthropic.PermissionDeniedError:
        return False, "Key lacks permissions"
    except anthropic.APIStatusError as e:
        return False, f"HTTP {e.status_code}"
    except anthropic.APIConnectionError:
        return False, "Connection failed — check your internet"


def validate_key(provider, key):
    """Validate an API key by making a lightweight test request.
    Returns (valid: bool, error: str or None).
    """
    if not key:
        return False, "No key provided"

    try:
        if provider == "openai":
            r = http_requests.get(
                "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=10
            )
            if r.status_code == 200:
                return True, None
            elif r.status_code == 401:
                return False, "Invalid API key"
            else:
                return False, f"HTTP {r.status_code}"

        elif provider == "anthropic":
            return _validate_anthropic(key)

        elif provider == "groq":
            r = http_requests.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            if r.status_code == 200:
                return True, None
            elif r.status_code == 401:
                return False, "Invalid API key"
            else:
                return False, f"HTTP {r.status_code}"

        elif provider == "deepgram":
            r = http_requests.get(
                "https://api.deepgram.com/v1/projects",
                headers={"Authorization": f"Token {key}"},
                timeout=10,
            )
            if r.status_code == 200:
                return True, None
            elif r.status_code == 401:
                return False, "Invalid API key"
            else:
                return False, f"HTTP {r.status_code}"

        elif provider == "openrouter":
            r = http_requests.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            if r.status_code == 200:
                return True, None
            elif r.status_code == 401:
                return False, "Invalid API key"
            else:
                return False, f"HTTP {r.status_code}"

        return False, "Unknown provider"
    except Exception as e:
        return False, str(e)[:100]
