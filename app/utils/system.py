"""System utility functions."""

import subprocess
import sys


def ensure_installed(package, pip_name=None):
    """Auto-install a Python package if missing."""
    try:
        __import__(package)
    except ImportError:
        try:
            print(f"Installing {pip_name or package}...")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--break-system-packages", "-q", pip_name or package],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            print(f"  Could not install {pip_name or package} (optional)")


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
