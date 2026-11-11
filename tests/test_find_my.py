"""Find My toggle: the app reports what the pin kept, not what it asked for."""

import asyncio

import pytest

from app.state import device as dev


def _state_reply(find_my):
    return bytes(15) + bytes([find_my])  # cmd 3 reply; byte 15 is the Find My state


@pytest.mark.parametrize(("kept", "expected"), [(1, True), (0, False)])
def test_set_find_my_returns_the_read_back_state(monkeypatch, kept, expected):
    sent = []

    async def fake_send(pt, timeout=5):
        sent.append(bytes(pt))
        return _state_reply(kept) if pt[1] == 3 else bytes([1, 109, 0, 1])

    monkeypatch.setattr(dev, "send_cmd", fake_send)
    assert asyncio.run(dev.set_find_my(True)) == {"find_my_enabled": expected}
    assert sent == [bytes([1, 109, 0, 1]), bytes([1, 3, 0])]


def test_set_find_my_unknown_when_no_read_back(monkeypatch):
    async def fake_send(pt, timeout=5):
        return None

    monkeypatch.setattr(dev, "send_cmd", fake_send)
    assert asyncio.run(dev.set_find_my(False)) == {"find_my_enabled": None}


def test_find_my_route_needs_a_connection(flask_client, monkeypatch):
    monkeypatch.setattr(dev, "is_connected", lambda: False)
    assert flask_client.post("/device/find-my", json={"enabled": True}).status_code == 400


def test_find_my_route_passes_the_choice_through(flask_client, monkeypatch):
    seen = {}

    async def fake_set(enabled):
        seen["enabled"] = enabled
        return {"find_my_enabled": enabled}

    monkeypatch.setattr(dev, "is_connected", lambda: True)
    monkeypatch.setattr(dev, "is_syncing", lambda: False)
    monkeypatch.setattr(dev, "set_find_my", fake_set)
    r = flask_client.post("/device/find-my", json={"enabled": True})
    assert r.get_json() == {"find_my_enabled": True}
    assert seen == {"enabled": True}
