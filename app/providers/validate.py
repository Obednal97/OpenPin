"""API key validation."""

import requests as http_requests


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
            r = http_requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
                timeout=10,
            )
            if r.status_code == 200:
                return True, None
            elif r.status_code == 401:
                return False, "Invalid API key"
            elif r.status_code == 403:
                return False, "Key lacks permissions"
            else:
                return False, f"HTTP {r.status_code}"

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
