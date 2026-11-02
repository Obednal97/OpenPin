"""App-managed Python runtime: lockfiles, ensure_installed, data-dir isolation,
and no side effects (network) when importing the backend."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import TEST_DATA_DIR

ROOT = Path(__file__).parent.parent
REQ_DIR = ROOT / "app" / "requirements"
LOCKFILES = sorted(REQ_DIR.glob("*.txt"))


def _run_python(code: str, env_overrides: dict, cwd=ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# Lockfiles
# ---------------------------------------------------------------------------


class TestLockfiles:
    def test_expected_lockfiles_exist(self):
        names = {f.name for f in LOCKFILES}
        assert {"core.txt", "whisper.txt", "diarize.txt", "local-summary.txt"} <= names

    @pytest.mark.parametrize("lockfile", LOCKFILES, ids=lambda p: p.name)
    def test_every_requirement_is_pinned_and_hashed(self, lockfile):
        """Each requirement line is ``name==version`` followed by --hash lines."""
        from app.utils.system import read_pins

        text = lockfile.read_text()
        logical = text.replace("\\\n", " ")
        reqs = [ln.strip() for ln in logical.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        assert reqs, f"{lockfile.name} is empty"
        for req in reqs:
            assert "==" in req.split()[0], f"unpinned requirement in {lockfile.name}: {req[:80]}"
            assert "--hash=sha256:" in req, f"no hash for {req.split()[0]} in {lockfile.name}"
        assert len(read_pins(lockfile)) == len(reqs)

    def test_core_has_everything_the_backend_imports(self):
        from app.utils.system import read_pins

        pins = read_pins(REQ_DIR / "core.txt")
        for pkg in (
            "flask",
            "requests",
            "bleak",
            "cryptography",
            "websockets",
            "pyobjc-core",
            "pyobjc-framework-corebluetooth",
            "pyobjc-framework-corewlan",
            "pyobjc-framework-cocoa",
            "anthropic",
            "pydantic",
            "httpx2",
        ):
            assert pkg in pins, f"{pkg} missing from core.txt"
        assert pins["anthropic"] == "2.3.0"

    def test_extras_pin_their_packages(self):
        from app.utils.system import read_pins

        assert {"openai-whisper", "torch"} <= set(read_pins(REQ_DIR / "whisper.txt"))
        assert {"diarize", "onnxruntime"} <= set(read_pins(REQ_DIR / "diarize.txt"))
        assert {"transformers", "sentencepiece", "torch"} <= set(read_pins(REQ_DIR / "local-summary.txt"))

    def test_lockfiles_never_disagree(self):
        """A package pinned in two files must have the same version in both
        (ensure_installed uses the union as constraints)."""
        from app.utils.system import read_pins

        seen: dict[str, tuple[str, str]] = {}
        for f in LOCKFILES:
            for name, version in read_pins(f).items():
                if name in seen:
                    assert seen[name][0] == version, (
                        f"{name}: {seen[name][1]} has {seen[name][0]}, {f.name} has {version}"
                    )
                else:
                    seen[name] = (version, f.name)

    def test_rust_embeds_every_lockfile(self):
        """python.rs embeds the lockfiles with include_str! so the packaged app
        doesn't depend on them being in the bundle's resources."""
        src = (ROOT / "src-tauri" / "src" / "python.rs").read_text()
        for f in LOCKFILES:
            assert f'include_str!("../../app/requirements/{f.name}")' in src, f.name


# ---------------------------------------------------------------------------
# ensure_installed
# ---------------------------------------------------------------------------


class _FakeRun:
    """Stands in for subprocess.run; records the pip command line."""

    def __init__(self, returncode=0, stdout="", on_call=None):
        self.returncode = returncode
        self.stdout = stdout
        self.calls = []
        self.constraint_text = None
        self.on_call = on_call

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if "-c" in cmd:
            path = Path(cmd[cmd.index("-c") + 1])
            if path.exists():
                self.constraint_text = path.read_text()
        if self.on_call:
            self.on_call()
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=self.stdout)


class TestBuildPipCommand:
    def test_in_venv_never_user_or_break_system(self):
        from app.utils.system import build_pip_command

        cmd = build_pip_command(["foo"], in_venv=True, externally_managed=True, python="/venv/bin/python3")
        assert cmd[:4] == ["/venv/bin/python3", "-m", "pip", "install"]
        assert "--user" not in cmd
        assert "--break-system-packages" not in cmd
        assert cmd[-1] == "foo"

    def test_outside_venv_user_install(self):
        from app.utils.system import build_pip_command

        cmd = build_pip_command(["foo"], in_venv=False, externally_managed=False)
        assert cmd[0] == sys.executable
        assert "--user" in cmd
        assert "--break-system-packages" not in cmd

    def test_outside_venv_externally_managed(self):
        from app.utils.system import build_pip_command

        cmd = build_pip_command(["foo"], in_venv=False, externally_managed=True)
        assert "--user" in cmd and "--break-system-packages" in cmd


class TestEnsureInstalled:
    @pytest.fixture(autouse=True)
    def _req_dir(self, monkeypatch):
        monkeypatch.setenv("PLAUD_REQUIREMENTS_DIR", str(REQ_DIR))

    def test_already_importable_does_nothing(self):
        from app.utils import system

        with patch.object(system.subprocess, "run") as run:
            assert system.ensure_installed("json") is True
        run.assert_not_called()

    def test_extra_installs_lockfile_with_core_constraints(self):
        from app.utils import system

        state = {"installed": False}
        fake = _FakeRun(on_call=lambda: state.update(installed=True))
        with (
            patch.object(system, "_importable", side_effect=lambda pkg: state["installed"]),
            patch.object(system, "in_virtualenv", return_value=True),
            patch.object(system.subprocess, "run", fake),
        ):
            assert system.ensure_installed("whisper", "openai-whisper", required=True, extra="whisper") is True

        ((cmd, kwargs),) = fake.calls
        assert cmd[:4] == [sys.executable, "-m", "pip", "install"]
        assert cmd[cmd.index("-r") + 1] == str(REQ_DIR / "whisper.txt")
        assert cmd[cmd.index("-c") + 1] == str(REQ_DIR / "core.txt")
        assert "openai-whisper" not in cmd  # the lockfile names it, pinned + hashed
        assert "--user" not in cmd and "--break-system-packages" not in cmd
        # pip's output is captured (and surfaced on failure), not thrown away
        assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.STDOUT

    def test_bare_package_gets_unhashed_constraints_from_all_lockfiles(self):
        from app.utils import system

        fake = _FakeRun()
        with (
            patch.object(system, "_importable", side_effect=[False, False, True]),
            patch.object(system, "in_virtualenv", return_value=True),
            patch.object(system.subprocess, "run", fake),
        ):
            assert system.ensure_installed("pyannote.audio", "pyannote.audio") is True

        ((cmd, _),) = fake.calls
        assert cmd[-1] == "pyannote.audio"
        constraints_path = Path(cmd[cmd.index("-c") + 1])
        assert not constraints_path.exists(), "temp constraints file not cleaned up"
        text = fake.constraint_text
        assert "--hash" not in text  # hashes would force hash-checking mode for the bare name
        assert "flask==" in text and "bleak==" in text  # core
        assert "transformers==" in text  # extras too

    def test_missing_extra_lockfile_falls_back_to_name(self, tmp_path, monkeypatch):
        from app.utils import system

        (tmp_path / "core.txt").write_text((REQ_DIR / "core.txt").read_text())
        monkeypatch.setenv("PLAUD_REQUIREMENTS_DIR", str(tmp_path))
        fake = _FakeRun()
        with (
            patch.object(system, "_importable", side_effect=[False, False, True]),
            patch.object(system, "in_virtualenv", return_value=True),
            patch.object(system.subprocess, "run", fake),
        ):
            assert system.ensure_installed("whisper", "openai-whisper", extra="whisper") is True
        ((cmd, _),) = fake.calls
        assert "-r" not in cmd
        assert cmd[-1] == "openai-whisper"

    def test_required_failure_raises_with_pip_output(self):
        from app.utils import system

        fake = _FakeRun(returncode=1, stdout="ERROR: No matching distribution found for nope==1.0")
        with (
            patch.object(system, "_importable", return_value=False),
            patch.object(system, "in_virtualenv", return_value=True),
            patch.object(system.subprocess, "run", fake),
            pytest.raises(system.PackageInstallError) as exc,
        ):
            system.ensure_installed("nope", required=True)
        assert "No matching distribution" in str(exc.value)
        assert "No matching distribution" in exc.value.output
        assert isinstance(exc.value, RuntimeError)  # old callers catch RuntimeError

    def test_optional_failure_returns_false_and_logs(self, caplog):
        from app.utils import system

        fake = _FakeRun(returncode=1, stdout="ERROR: boom")
        with (
            patch.object(system, "_importable", return_value=False),
            patch.object(system, "in_virtualenv", return_value=True),
            patch.object(system.subprocess, "run", fake),
            caplog.at_level("ERROR"),
        ):
            assert system.ensure_installed("nope") is False
        assert "ERROR: boom" in caplog.text

    def test_pip_success_but_still_not_importable_is_a_failure(self):
        from app.utils import system

        with (
            patch.object(system, "_importable", return_value=False),
            patch.object(system, "in_virtualenv", return_value=True),
            patch.object(system.subprocess, "run", _FakeRun()),
            pytest.raises(system.PackageInstallError, match="still doesn't import"),
        ):
            system.ensure_installed("nope", "some-dist", required=True)

    def test_bad_extra_name_rejected(self):
        from app.utils import system

        with patch.object(system, "_importable", return_value=False), pytest.raises(ValueError):
            system.ensure_installed("x", extra="../../etc/passwd")

    def test_requirements_dir_env_and_fallback(self, tmp_path, monkeypatch):
        from app.utils import system

        monkeypatch.setenv("PLAUD_REQUIREMENTS_DIR", str(tmp_path))
        assert system.requirements_dir() == tmp_path
        monkeypatch.setenv("PLAUD_REQUIREMENTS_DIR", str(tmp_path / "missing"))
        assert system.requirements_dir() == REQ_DIR
        monkeypatch.delenv("PLAUD_REQUIREMENTS_DIR")
        assert system.requirements_dir() == REQ_DIR


class TestTranscribeInstallsThroughEnsureInstalled:
    def test_whisper_install_uses_pinned_extra(self, monkeypatch):
        import src.transcribe as tr
        from app.utils import system

        class Stop(Exception):
            pass

        calls = []

        def fake_ensure(*args, **kwargs):
            calls.append((args, kwargs))
            raise Stop

        monkeypatch.setitem(sys.modules, "whisper", None)  # makes `import whisper` fail
        monkeypatch.setattr(system, "ensure_installed", fake_ensure)
        with pytest.raises(Stop):
            tr.transcribe_with_whisper("x.ogg")
        assert calls == [(("whisper", "openai-whisper"), {"required": True, "extra": "whisper"})]

    def test_diarize_install_uses_pinned_extra_and_falls_through(self, monkeypatch):
        import src.transcribe as tr
        from app.utils import system

        calls = []
        monkeypatch.setitem(sys.modules, "diarize", None)
        monkeypatch.setattr(system, "ensure_installed", lambda *a, **k: calls.append((a, k)) or False)
        with pytest.raises(ImportError):
            tr.diarize_audio_fast("x.wav")
        assert calls == [(("diarize",), {"extra": "diarize"})]

    def test_no_ad_hoc_pip_left(self):
        for rel in ("src/transcribe.py", "app/utils/system.py"):
            src = (ROOT / rel).read_text()
            assert '"--break-system-packages", "' not in src, rel


# ---------------------------------------------------------------------------
# Data dir isolation
# ---------------------------------------------------------------------------


class TestDataDir:
    def test_suite_runs_against_temp_data_dir(self):
        from app.utils import config, constants

        assert Path(constants.DATA_DIR) == TEST_DATA_DIR
        assert Path(constants.CONFIG_PATH).parent == TEST_DATA_DIR
        assert Path(config.CONFIG_PATH).parent == TEST_DATA_DIR
        assert Path(constants.PLAUD_CONFIG_PATH).parent == TEST_DATA_DIR
        assert TEST_DATA_DIR != ROOT

    def test_env_var_wins(self, tmp_path):
        target = tmp_path / "custom data"
        r = _run_python(
            "from app.utils import constants; print(constants.DATA_DIR)",
            {"PLAUD_DATA_DIR": str(target)},
        )
        assert r.returncode == 0, r.stderr
        assert Path(r.stdout.strip()) == target.resolve()
        assert target.is_dir()

    def test_env_var_wins_over_the_packaged_folder(self, tmp_path):
        # Even with a fake HOME whose packaged folder already exists, and when
        # run from a source checkout: PLAUD_DATA_DIR is what counts.
        home = tmp_path / "home"
        (home / "Library/Application Support/Plaud V2").mkdir(parents=True)
        target = tmp_path / "dev data"
        r = _run_python(
            "from app.utils import constants; print(constants.DATA_DIR)",
            {"PLAUD_DATA_DIR": str(target), "HOME": str(home)},
        )
        assert r.returncode == 0, r.stderr
        assert Path(r.stdout.strip()) == target.resolve()

    def test_tilde_in_env_var_is_expanded(self, tmp_path):
        r = _run_python(
            "from app.utils import constants; print(constants.DATA_DIR)",
            {"PLAUD_DATA_DIR": "~/plaud dev", "HOME": str(tmp_path)},
        )
        assert r.returncode == 0, r.stderr
        assert Path(r.stdout.strip()) == (tmp_path / "plaud dev").resolve()

    def test_default_is_the_packaged_folder_even_in_a_checkout(self, tmp_path):
        # No repo-root fallback: the checkout may be synced to other machines
        # and the data folder holds API keys. HOME is faked so the real
        # Application Support folder is never touched.
        r = _run_python(
            "from app.utils import constants; print(constants.DATA_DIR)",
            {"PLAUD_DATA_DIR": None, "HOME": str(tmp_path)},
        )
        assert r.returncode == 0, r.stderr
        expected = tmp_path / "Library/Application Support/Plaud V2"
        assert Path(r.stdout.strip()) == expected
        assert expected.is_dir()
        assert Path(r.stdout.strip()).resolve() != ROOT.resolve()

    def test_resolve_data_dir(self, tmp_path):
        from app.utils.constants import PACKAGED_DATA_DIR, resolve_data_dir

        assert resolve_data_dir(None) == PACKAGED_DATA_DIR
        assert resolve_data_dir("  ") == PACKAGED_DATA_DIR
        assert resolve_data_dir(str(tmp_path)) == tmp_path.resolve()


# ---------------------------------------------------------------------------
# No side effects on import
# ---------------------------------------------------------------------------

_NO_NETWORK_SCRIPT = """
import importlib, pkgutil, socket, sys, threading

attempts = []

def _blocked(*args, **kwargs):
    attempts.append(repr(args)[:200])
    raise OSError("network disabled in test")

real_connect = socket.socket.connect
def _connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        return _blocked(address)
    return real_connect(self, address)

socket.socket.connect = _connect
socket.socket.connect_ex = _connect
socket.create_connection = _blocked
socket.getaddrinfo = _blocked

import requests
requests.Session.request = _blocked
requests.api.request = _blocked

threads_before = set(threading.enumerate())

import app.api_server  # imports every route and state module
import app
for mod in pkgutil.walk_packages(app.__path__, "app."):
    importlib.import_module(mod.name)
import src.transcribe

from app.state import cloud
assert cloud._cloud_state["token"] is None, "cloud token loaded at import"
assert cloud._cloud_state["logged_in"] is False

new_threads = [t.name for t in threading.enumerate() if t not in threads_before]
print("ATTEMPTS", attempts)
print("THREADS", new_threads)
"""


class TestNoSideEffectsOnImport:
    def test_importing_the_backend_makes_no_network_calls(self, tmp_path):
        # Plant a saved Plaud token: before the fix, importing app.state.cloud
        # loaded it and started a validation request to the Plaud API.
        data = tmp_path / "data"
        data.mkdir()
        (data / ".plaud_config.json").write_text('{"token": "eyJfake", "api_domain": "https://api-euc1.plaud.ai"}')
        home = tmp_path / "home"
        home.mkdir()
        r = _run_python(
            _NO_NETWORK_SCRIPT,
            {"PLAUD_DATA_DIR": str(data), "HOME": str(home), "PLAUD_API_TOKEN": "x" * 32},
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ATTEMPTS []" in r.stdout, r.stdout
        assert "THREADS []" in r.stdout, r.stdout

    def test_cloud_init_loads_the_saved_token(self, tmp_path, monkeypatch):
        from app.state import cloud

        cfg = tmp_path / ".plaud_config.json"
        cfg.write_text('{"token": "eyJsaved", "api_domain": "https://api.plaud.ai"}')
        state = {"logged_in": False, "token": None, "api_domain": "x", "user": None, "desktop_uuid": None}
        monkeypatch.setattr(cloud, "PLAUD_CONFIG_PATH", cfg)
        monkeypatch.setattr(cloud, "_cloud_state", state)
        with patch.object(cloud.threading, "Thread") as thread:
            assert cloud.init() is True
        assert state["token"] == "eyJsaved" and state["logged_in"] is True
        thread.return_value.start.assert_called_once()  # validation runs in the background

    def test_api_server_calls_cloud_init_only_under_main(self):
        src = (ROOT / "app" / "api_server.py").read_text()
        main_block = src.split('if __name__ == "__main__":', 1)
        assert len(main_block) == 2
        assert "_cloud_state.init()" in main_block[1]
        assert "init()" not in main_block[0].replace("def init", "")
        cloud_src = (ROOT / "app" / "state" / "cloud.py").read_text()
        assert not any(line.startswith("load_cloud_token()") for line in cloud_src.splitlines())


# ---------------------------------------------------------------------------
# Real install of core.txt (opt-in: PLAUD_SLOW_TESTS=1)
# ---------------------------------------------------------------------------

_PYTHON_RS = ROOT / "src-tauri" / "src" / "python.rs"

# What the backend can't start without (python.rs CORE_IMPORT_CHECK, plus a
# full anthropic import and a check that the interpreter is the arm64 3.12).
_CORE_IMPORTS = """
import platform, sys
assert sys.version_info[:2] == (3, 12), sys.version
assert platform.machine() == "arm64", platform.machine()
import flask, requests, bleak, cryptography, websockets, anthropic
import objc, CoreBluetooth, CoreWLAN, Foundation
print("ok", sys.version.split()[0], anthropic.__version__)
"""


def _rust_const(name: str) -> str:
    import re

    m = re.search(rf'const {name}: &str =\s*"([^"]+)";', _PYTHON_RS.read_text())
    assert m, f"{name} not found in python.rs"
    return m.group(1)


def _clean_env() -> dict:
    """The environment python.rs gives pip (python_command): no user site, no
    PYTHONPATH/PYTHONHOME, no inherited PIP_USER/PIP_REQUIRE_HASHES."""
    drop = {"PYTHONHOME", "PYTHONPATH", "PIP_USER", "PIP_REQUIRE_HASHES"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(PYTHONNOUSERSITE="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    return env


def _managed_python(workdir: Path) -> Path:
    """The pinned python-build-standalone CPython the app downloads (URL and
    SHA-256 read from python.rs), fetched into `workdir`. PLAUD_PYTHON312 points
    at an existing CPython 3.12 instead."""
    import hashlib
    import urllib.request

    override = os.environ.get("PLAUD_PYTHON312")
    if override:
        return Path(override)
    tag = _rust_const("STANDALONE_TAG")
    version = _rust_const("STANDALONE_PY_VERSION")
    sha256 = _rust_const("STANDALONE_SHA256_AARCH64")
    name = f"cpython-{version}+{tag}-aarch64-apple-darwin-install_only_stripped.tar.gz"
    url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{tag}/{name}"
    tarball = workdir / name
    with urllib.request.urlopen(url, timeout=300) as resp, open(tarball, "wb") as out:
        out.write(resp.read())
    assert hashlib.sha256(tarball.read_bytes()).hexdigest() == sha256, "pinned runtime checksum mismatch"
    dest = workdir / "python"
    dest.mkdir()
    subprocess.run(["tar", "-xzf", str(tarball), "--strip-components=1", "-C", str(dest)], check=True)
    return dest / "bin" / "python3"


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("PLAUD_SLOW_TESTS") != "1", reason="set PLAUD_SLOW_TESTS=1 (network, ~1 min)")
class TestCoreLockInstallsForReal:
    """What a first launch does (python.rs ensure_venv): a clean venv on the
    managed CPython 3.12, then pip install core.txt with hashes, wheels only.
    Catches a missing hash, a pin with no cp312/arm64 wheel, or a package that
    installs but doesn't import."""

    def test_clean_venv_installs_core_lock_and_imports(self, tmp_path):
        base = _managed_python(tmp_path)
        r = subprocess.run([str(base), "--version"], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and r.stdout.startswith("Python 3.12."), (r.stdout, r.stderr)

        env = _clean_env()
        venv = tmp_path / "venv"
        subprocess.run([str(base), "-m", "venv", str(venv)], check=True, env=env, timeout=300)
        vpy = str(venv / "bin" / "python3")
        pip = subprocess.run(
            [vpy, "-m", "pip", "install", "--no-input", "--require-hashes", "--only-binary", ":all:"]
            + ["-r", str(REQ_DIR / "core.txt")],
            capture_output=True,
            text=True,
            env=env,
            timeout=1800,
        )
        assert pip.returncode == 0, pip.stdout[-4000:] + pip.stderr[-4000:]

        check = subprocess.run(
            [vpy, "-c", _CORE_IMPORTS], capture_output=True, text=True, env=env, timeout=300, cwd=tmp_path
        )
        assert check.returncode == 0, check.stdout + check.stderr
        assert check.stdout.startswith("ok 3.12."), check.stdout

        # The lock is self-consistent: no missing or conflicting requirements.
        consistent = subprocess.run([vpy, "-m", "pip", "check"], capture_output=True, text=True, env=env, timeout=300)
        assert consistent.returncode == 0, consistent.stdout + consistent.stderr
