"""Webhook firing utility."""

import logging
import threading
from datetime import datetime

import requests as http_requests

from app.utils.config import load_config


def fire_webhooks(event_type, payload):
    """Fire webhooks for a given event type in a background thread."""
    config = load_config()
    webhooks = config.get("webhooks", [])
    if not webhooks:
        return

    def _send():
        for wh in webhooks:
            url = wh.get("url", "")
            triggers = wh.get("triggers", {})
            if not url or not triggers.get(event_type, False):
                continue
            try:
                http_requests.post(
                    url,
                    json={
                        "event": event_type,
                        "timestamp": datetime.now().isoformat(),
                        "data": payload,
                    },
                    timeout=10,
                )
            except Exception as e:
                logging.warning(f"Webhook failed for {url}: {e}")

    threading.Thread(target=_send, daemon=True).start()
