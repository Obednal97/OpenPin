"""System utility functions."""

import logging
import os
import platform
import shutil
import subprocess
import sys


def ensure_installed(package, pip_name=None):
    """Auto-install a Python package if missing."""
    try:
        __import__(package)
    except ImportError:
        name = pip_name or package
        print(f"Installing {name}...")
        # Try with --break-system-packages (pip 23.1+), fall back without (older pip)
        for extra_args in [["--break-system-packages"], []]:
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", *extra_args, "--user", "-q", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                pass
        print(f"  Could not install {name} (optional)")


# ---- FFmpeg auto-download ----

# Where to cache downloaded FFmpeg (persists across app updates)
_FFMPEG_DIR = os.path.join(
    os.path.expanduser("~"),
    "Library", "Application Support", "com.plaud.v2", "bin"
)
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
