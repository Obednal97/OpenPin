"""System utility functions."""

import importlib
import logging
import os
import re
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import threading
from pathlib import Path

from app.utils.constants import BUNDLED_REQUIREMENTS_DIR, REQUIREMENTS_DIR_ENV

log = logging.getLogger(__name__)

# ---- Python package installs ----
#
# The Tauri shell runs the backend in an app-managed venv (CPython 3.12 from
# python-build-standalone, see src-tauri/src/python.rs) with the pinned core
# packages from app/requirements/core.txt already installed. Optional features
# (local Whisper, local diarization, local summaries) install their pinned extra
# into the SAME venv on first use, through ensure_installed().

# pip can take minutes for torch; a hung install shouldn't block forever.
PIP_TIMEOUT_SECONDS = 30 * 60

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")
_EXTRA_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# One pip at a time: two transcriptions starting together must not race.
_install_lock = threading.Lock()


class PackageInstallError(RuntimeError):
    """pip failed (or the package still won't import). ``output`` has pip's log."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def requirements_dir() -> Path:
    """Folder holding core.txt and the extras' lockfiles.

    The Rust shell writes the copies embedded in the app binary to Application
    Support and passes the folder in ``PLAUD_REQUIREMENTS_DIR``; dev runs and
    tests fall back to the repo's app/requirements.
    """
    env = os.environ.get(REQUIREMENTS_DIR_ENV, "").strip()
    if env and Path(env).is_dir():
        return Path(env)
    return BUNDLED_REQUIREMENTS_DIR


def in_virtualenv() -> bool:
    """True when running inside a venv (the app-managed one, or a dev venv)."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def is_externally_managed() -> bool:
    """PEP 668: the interpreter refuses pip installs without --break-system-packages."""
    try:
        return Path(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED").exists()
    except Exception:
        return False


def read_pins(path: Path) -> dict[str, str]:
    """``{normalised name: version}`` for every ``name==version`` line of a lockfile."""
    pins = {}
    for line in Path(path).read_text().splitlines():
        m = _PIN_RE.match(line.strip())
        if m:
            pins[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return pins


def collect_constraints(req_dir: Path) -> dict[str, str]:
    """Pins from every lockfile in *req_dir*. scripts/lock-python-deps.sh resolves
    each extra against core and the extras before it, so the files never disagree.
    """
    pins: dict[str, str] = {}
    for f in sorted(Path(req_dir).glob("*.txt")):
        pins.update(read_pins(f))
    return pins


def build_pip_command(
    targets: list[str],
    *,
    in_venv: bool,
    externally_managed: bool,
    python: str | None = None,
) -> list[str]:
    """The pip command line for *targets* (requirement specs, ``-r``/``-c`` options).

    Inside a venv (the app-managed one) pip installs into that venv: never
    ``--user`` or ``--break-system-packages``. Outside one (a developer running
    api_server.py with a system Python) it falls back to a user install, adding
    ``--break-system-packages`` only when the interpreter is externally managed.
    """
    cmd = [
        python or sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--prefer-binary",
    ]
    if not in_venv:
        cmd.append("--user")
        if externally_managed:
            cmd.append("--break-system-packages")
    cmd.extend(targets)
    return cmd


def _importable(package: str) -> bool:
    try:
        importlib.import_module(package)
        return True
    except ImportError:
        return False


def _tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def ensure_installed(package, pip_name=None, required: bool = False, *, extra: str | None = None) -> bool:
    """Install a Python package into the running interpreter if it's missing.

    Args:
        package:  The importable module name (e.g. ``"whisper"``).
        pip_name: The PyPI distribution name when it differs from the module
                  name (e.g. ``"openai-whisper"``). Defaults to *package*.
        required: When True and the install fails, raise
                  :exc:`PackageInstallError` (a RuntimeError) with pip's error.
                  Otherwise log the error and return False.
        extra:    Name of a pinned extra in app/requirements (``"whisper"``,
                  ``"diarize"``, ``"local-summary"``). Its lockfile (exact pins
                  + hashes) is installed instead of the bare package name.

    Every install is constrained by app/requirements/core.txt, so an optional
    feature can never move a core package (flask, bleak, cryptography...).

    Returns True when *package* imports afterwards.
    """
    if _importable(package):
        return True

    name = pip_name or package
    req_dir = requirements_dir()
    core_lock = req_dir / "core.txt"
    targets: list[str] = []
    tmp_constraints = None

    extra_lock = None
    if extra:
        if not _EXTRA_NAME_RE.match(extra):
            raise ValueError(f"Bad extra name: {extra!r}")
        candidate = req_dir / f"{extra}.txt"
        if candidate.is_file():
            extra_lock = candidate
        else:
            log.warning("[deps] Lockfile %s is missing; installing %s from core constraints only", candidate, name)

    if extra_lock is not None:
        # The extra's lockfile is a full, hashed closure, so pip runs in
        # hash-checking mode; core.txt (also hashed) can constrain it directly.
        targets += ["-r", str(extra_lock)]
        if core_lock.is_file():
            targets += ["-c", str(core_lock)]
    else:
        # A bare name can't be hash-checked, and pip turns on hash-checking for
        # the whole install if a constraints file has hashes. So pass the same
        # pins without their hashes.
        pins = collect_constraints(req_dir) if req_dir.is_dir() else {}
        if pins:
            tmp_constraints = tempfile.NamedTemporaryFile("w", prefix="plaud-constraints-", suffix=".txt", delete=False)
            with tmp_constraints:
                tmp_constraints.write("".join(f"{k}=={v}\n" for k, v in sorted(pins.items())))
            targets += ["-c", tmp_constraints.name]
        targets.append(name)

    venv = in_virtualenv()
    cmd = build_pip_command(targets, in_venv=venv, externally_managed=is_externally_managed())
    log.info("[deps] Installing %s%s: %s", name, f" (extra '{extra}')" if extra else "", " ".join(cmd))
    print(f"Installing {name}...", flush=True)

    output = ""
    ok = False
    try:
        with _install_lock:
            # Another thread may have installed it while we waited.
            importlib.invalidate_caches()
            if _importable(package):
                return True
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=PIP_TIMEOUT_SECONDS,
            )
            output = result.stdout or ""
            ok = result.returncode == 0
            if not ok:
                output = f"pip exited with code {result.returncode}\n{output}"
    except subprocess.TimeoutExpired as e:
        output = f"pip timed out after {PIP_TIMEOUT_SECONDS}s\n{e.output or ''}"
    except OSError as e:
        output = f"Could not run pip: {e}"
    finally:
        if tmp_constraints is not None:
            try:
                os.unlink(tmp_constraints.name)
            except OSError:
                pass

    if ok:
        importlib.invalidate_caches()
        if not venv:
            # A --user install may have created user site-packages just now.
            user_site = site.getusersitepackages()
            if os.path.isdir(user_site) and user_site not in sys.path:
                site.addsitedir(user_site)
        if _importable(package):
            log.info("[deps] Installed %s", name)
            return True
        output = f"pip succeeded but '{package}' still doesn't import.\n{output}"

    message = f"Could not install {'required' if required else 'optional'} package '{name}' into {sys.executable}"
    log.error("[deps] %s\n%s", message, _tail(output))
    if required:
        raise PackageInstallError(f"{message}:\n{_tail(output, 15)}", output=output)
    return False


# ---- FFmpeg auto-download ----

# Where to cache downloaded FFmpeg (persists across app updates)
_FFMPEG_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "com.plaud.v2", "bin")
_FFMPEG_PATH = os.path.join(_FFMPEG_DIR, "ffmpeg")

# Download URL for static macOS FFmpeg from evermeet.cx
_FFMPEG_URL = "https://evermeet.cx/ffmpeg/getrelease/zip"


def ensure_ffmpeg():
    """Ensure FFmpeg is available on PATH. Downloads if not found.

    Returns the path to the ffmpeg binary, or None if unavailable.
    """
    # 1. Already on PATH? (bundled in Resources/bin/ or system install)
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")

    # 2. Already downloaded to app support?
    if os.path.isfile(_FFMPEG_PATH) and os.access(_FFMPEG_PATH, os.X_OK):
        _add_to_path(_FFMPEG_DIR)
        return _FFMPEG_PATH

    # 3. Download it
    logging.info("[FFmpeg] Not found — downloading static build...")
    try:
        os.makedirs(_FFMPEG_DIR, exist_ok=True)
        zip_path = os.path.join(_FFMPEG_DIR, "ffmpeg.zip")

        # Download with curl (always available on macOS)
        subprocess.check_call(
            ["curl", "-fSL", "--retry", "2", "-o", zip_path, _FFMPEG_URL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )

        # Extract — the ZIP contains a single "ffmpeg" binary
        subprocess.check_call(
            ["unzip", "-o", "-q", zip_path, "-d", _FFMPEG_DIR],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Clean up ZIP
        os.remove(zip_path)

        # Make executable
        os.chmod(_FFMPEG_PATH, 0o755)

        if os.path.isfile(_FFMPEG_PATH):
            logging.info(f"[FFmpeg] Downloaded to {_FFMPEG_PATH}")
            _add_to_path(_FFMPEG_DIR)
            return _FFMPEG_PATH
    except Exception as e:
        logging.error(f"[FFmpeg] Download failed: {e}")

    return None


def _add_to_path(directory):
    """Add a directory to PATH if not already present."""
    path = os.environ.get("PATH", "")
    if directory not in path.split(":"):
        os.environ["PATH"] = f"{directory}:{path}"


def get_ram_info():
    """Get total and used RAM in GB (macOS only)."""
    try:
        total_bytes = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip())
        r = subprocess.run(["vm_stat"], capture_output=True, text=True)
        stats = {}
        for line in r.stdout.strip().split("\n")[1:]:
            if ":" in line:
                k, v = line.split(":")
                try:
                    stats[k.strip()] = int(v.strip().rstrip("."))
                except ValueError:
                    pass
        page_size = 16384
        used_bytes = (
            stats.get("Pages active", 0) + stats.get("Pages wired down", 0) + stats.get("Pages speculative", 0)
        ) * page_size
        return total_bytes / 1024**3, used_bytes / 1024**3
    except Exception:
        return 16.0, 8.0  # fallback
