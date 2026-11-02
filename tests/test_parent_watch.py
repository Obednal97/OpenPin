"""The backend exits when the shell that spawned it disappears."""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.utils import parent_watch

ROOT = Path(__file__).resolve().parent.parent


def test_disabled_without_shell_token(monkeypatch):
    monkeypatch.delenv("PLAUD_API_TOKEN", raising=False)
    monkeypatch.setattr(parent_watch, "_started", False)
    assert parent_watch.start() is False


def test_parent_gone_detects_reparenting(monkeypatch):
    monkeypatch.setattr(os, "getppid", lambda: 1)
    assert parent_watch._parent_gone(4242) is True
    monkeypatch.setattr(os, "getppid", lambda: 4242)
    assert parent_watch._parent_gone(4242) is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process semantics")
def test_child_exits_after_parent_is_killed(tmp_path):
    """Real processes: kill a fake 'shell' and the backend-like child must exit."""
    pidfile = tmp_path / "child.pid"
    child_code = textwrap.dedent(
        f"""
        import os, sys, time
        sys.path.insert(0, {str(ROOT)!r})
        os.environ["PLAUD_API_TOKEN"] = "t"
        from app.utils import parent_watch
        assert parent_watch.start(interval=0.2)
        open({str(pidfile)!r}, "w").write(str(os.getpid()))
        time.sleep(30)
        """
    )
    shell_code = textwrap.dedent(
        f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", {child_code!r}])
        time.sleep(30)
        """
    )
    shell = subprocess.Popen([sys.executable, "-c", shell_code])
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.05)
        child_pid = int(pidfile.read_text())
        shell.kill()  # simulate SIGKILL / crash of the Tauri shell
        shell.wait(timeout=5)
        deadline = time.time() + 5
        alive = True
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.1)
        if alive:
            os.kill(child_pid, 9)
        assert not alive, "backend child outlived its parent"
    finally:
        if shell.poll() is None:
            shell.kill()
