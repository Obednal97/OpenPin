"""run_async: a status poll that can't get the BLE lock must not leave an
un-awaited coroutine behind ("coroutine '_poll_device' was never awaited")."""

import threading
import warnings

import pytest

import app.state.device as dev


def test_busy_lock_closes_the_coroutine():
    async def poll():
        return 1

    coro = poll()
    holder = threading.Event()
    release = threading.Event()

    def hold():
        with dev._ble_lock:
            holder.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    holder.wait(5)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with pytest.raises(TimeoutError):
                dev.run_async(coro, lock_timeout=0.01)
        assert coro.cr_frame is None  # closed
    finally:
        release.set()
        t.join()


def test_run_async_still_runs_coroutines():
    async def poll():
        return 42

    assert dev.run_async(poll(), lock_timeout=1) == 42
    assert dev.run_async(poll()) == 42


def test_connect_route_timeout_has_a_message(monkeypatch):
    """Issue #93: run_async's TimeoutError has an empty str(), which left the
    connect route returning a bare 'HTTP 500'."""
    from flask import Flask

    from app.routes import device as routes

    def timing_out(coro, **kw):
        coro.close()
        raise TimeoutError()

    monkeypatch.setattr(routes.dev, "run_async", timing_out)
    app = Flask(__name__)
    app.register_blueprint(routes.bp, url_prefix="/device")
    with app.test_request_context("/device/connect", method="POST", json={}):
        response, status = routes.connect()
    assert status == 500
    body = response.get_json()
    assert body["error"].startswith("Connection timed out")
    assert body["raw_error"] == "TimeoutError"
