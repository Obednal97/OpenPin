#!/usr/bin/env python3
"""Plaud NotePin S - Open Source BLE Client, Cloud Downloader & Transcription.

Usage:
    python plaud.py scan              - Scan for nearby Plaud devices
    python plaud.py connect           - Connect and show device info
    python plaud.py list              - List recordings on device (via BLE)
    python plaud.py download          - Download all recordings from device (via BLE)
    python plaud.py cloud-list        - List recordings in cloud
    python plaud.py cloud-download    - Download all recordings from cloud
    python plaud.py battery           - Show battery level
    python plaud.py transcribe <file> - Transcribe an audio file locally
    python plaud.py transcribe-all    - Transcribe all downloaded recordings
    python plaud.py summarize <file>  - Summarize a transcript
"""

import asyncio
import sys
import json
import os
from pathlib import Path

from src.scanner import scan_for_plaud
from src.client import PlaudClient
from src.cloud_api import PlaudCloudClient


# Auth token - loaded from env or config
def get_cloud_token() -> str | None:
    """Get Plaud cloud API token from environment or config file."""
    token = os.environ.get("PLAUD_TOKEN")
    if token:
        return token

    config_path = Path(".plaud_config.json")
    if config_path.exists():
        config = json.loads(config_path.read_text())
        return config.get("token")

    return None


async def cmd_scan():
    """Scan for Plaud BLE devices."""
    devices = await scan_for_plaud(timeout=10)
    print(f"\nTotal: {len(devices)} device(s) found")


def pick_device(devices, target_sn: str | None = None):
    """Pick the right device from scan results."""
    if target_sn:
        for device, info in devices:
            if target_sn in info.get("serial_number", ""):
                return device, info
        print(f"Device with SN containing '{target_sn}' not found")
        return None, None

    if len(devices) == 1:
        return devices[0]

    print("\nMultiple devices found. Pick one:")
    for i, (d, info) in enumerate(devices):
        sn = info.get("serial_number", "?")
        model = info.get("model", "?")
        print(f"  [{i+1}] {model} SN:{sn} RSSI:{info.get('rssi', '?')}")

    try:
        choice = int(input("Enter number: ")) - 1
        if 0 <= choice < len(devices):
            return devices[choice]
    except (ValueError, EOFError):
        pass
    return devices[0]


# Target SN from config or env
TARGET_SN = os.environ.get("PLAUD_SN", "8820A0000000XXXX")


async def cmd_connect():
    """Connect to a Plaud device and show info."""
    devices = await scan_for_plaud(timeout=20)
    if not devices:
        return

    device, info = pick_device(devices, TARGET_SN)
    if not device:
        return
    client = PlaudClient(device=device)

    if not await client.connect():
        return

    try:
        battery = await client.get_battery_level()
        print(f"\nBattery: {battery}%")

        state = await client.get_device_state()
        print(f"Recording: {state.recording}")
        print(f"Privacy: {state.privacy_enabled}")
        print(f"State raw: {state.raw.hex()}")

        files = await client.get_file_list()
        print(f"\nRecordings on device: {len(files)}")
        for f in files:
            print(f"  Session {f.session_id}: {f.file_size} bytes ({f.duration_str})")
    finally:
        await client.disconnect()


async def cmd_list():
    """List recordings on the device."""
    devices = await scan_for_plaud(timeout=10)
    if not devices:
        return

    device, info = pick_device(devices, TARGET_SN)
    if not device:
        return
    client = PlaudClient(device=device)

    if not await client.connect():
        return

    try:
        files = await client.get_file_list()
        if not files:
            print("No recordings on device")
            return

        print(f"\n{'#':<4} {'Session ID':<15} {'Size':<12} {'Duration':<10}")
        print("-" * 45)
        for i, f in enumerate(files):
            print(f"{i+1:<4} {f.session_id:<15} {f.file_size:<12} {f.duration_str:<10}")
    finally:
        await client.disconnect()


async def cmd_download():
    """Download all recordings from the device via BLE."""
    devices = await scan_for_plaud(timeout=10)
    if not devices:
        return

    device, info = pick_device(devices, TARGET_SN)
    if not device:
        return
    client = PlaudClient(device=device, output_dir="recordings")

    if not await client.connect():
        return

    try:
        saved = await client.download_all()
        print(f"\nDownloaded {len(saved)} file(s) to recordings/")
    finally:
        await client.disconnect()


def cmd_cloud_list():
    """List recordings in the cloud."""
    token = get_cloud_token()
    if not token:
        print("No cloud token found. Set PLAUD_TOKEN env var or create .plaud_config.json")
        print('  export PLAUD_TOKEN="your_jwt_token_here"')
        print('  echo \'{"token":"your_jwt_token_here"}\' > .plaud_config.json')
        return

    client = PlaudCloudClient(token=token)

    try:
        devices = client.get_devices()
        print(f"Devices: {len(devices)}")
        for d in devices:
            print(f"  {d.get('name', '?')} (SN: {d.get('sn', '?')}, model: {d.get('model', '?')})")
    except Exception as e:
        print(f"Error getting devices: {e}")
        return

    recordings = client.get_recordings(limit=50)
    if not recordings:
        print("No recordings in cloud")
        return

    print(f"\n{'#':<4} {'Name':<30} {'Duration':<10} {'Size':<12} {'Device':<20}")
    print("-" * 80)
    for i, r in enumerate(recordings):
        dur = f"{r.duration // 60}m {r.duration % 60}s"
        size = f"{r.file_size // 1024}KB"
        print(f"{i+1:<4} {r.filename[:28]:<30} {dur:<10} {size:<12} {r.device_sn[:18]:<20}")


def cmd_cloud_download():
    """Download all recordings from the cloud."""
    token = get_cloud_token()
    if not token:
        print("No cloud token found. Set PLAUD_TOKEN env var or create .plaud_config.json")
        return

    client = PlaudCloudClient(token=token)
    # Use MP3 by default - Opus files from device may be encrypted (e2ee)
    saved = client.download_all(output_dir="cloud_recordings", opus=False)
    print(f"\nDownloaded {len(saved)} file(s) to cloud_recordings/")


async def cmd_battery():
    """Show battery level."""
    devices = await scan_for_plaud(timeout=10)
    if not devices:
        return

    device, info = pick_device(devices, TARGET_SN)
    if not device:
        return
    client = PlaudClient(device=device)

    if not await client.connect():
        return

    try:
        battery = await client.get_battery_level()
        print(f"\nBattery: {battery}%")
    finally:
        await client.disconnect()


def cmd_transcribe():
    """Transcribe an audio file locally."""
    from src.transcribe import transcribe, transcribe_directory

    if len(sys.argv) < 3:
        print("Usage: python plaud.py transcribe <file_or_directory>")
        return

    path = Path(sys.argv[2])
    model = "base"
    backend = "whisper"

    # Parse optional flags
    if "--fast" in sys.argv:
        backend = "fast"
        model = "distil-whisper/large-v2"
    if "--large" in sys.argv:
        model = "large-v3" if backend == "whisper" else "openai/whisper-large-v3"
    if "--medium" in sys.argv:
        model = "medium"

    if path.is_dir():
        results = transcribe_directory(str(path), backend=backend, model=model)
        print(f"\nTranscribed {len(results)} file(s)")
    elif path.is_file():
        t = transcribe(str(path), model=model, backend=backend)
        if t.text:
            out_json = str(path.with_suffix(".json"))
            out_txt = str(path.with_suffix(".txt"))
            t.save(out_json)
            Path(out_txt).write_text(t.formatted, encoding="utf-8")
            print(f"\nSaved: {out_json}")
            print(f"Saved: {out_txt}")
            # Show first part of transcript
            preview = t.formatted[:1500]
            if len(t.formatted) > 1500:
                preview += "\n..."
            print(f"\n{preview}")
    else:
        print(f"Not found: {path}")


def cmd_transcribe_all():
    """Transcribe all downloaded recordings."""
    from src.transcribe import transcribe_directory

    directory = "cloud_recordings"
    if not Path(directory).exists():
        print(f"No recordings directory found at {directory}")
        print("Run 'python plaud.py cloud-download' first")
        return

    model = "base"
    if "--medium" in sys.argv:
        model = "medium"
    if "--large" in sys.argv:
        model = "large-v3"

    results = transcribe_directory(directory, model=model)
    print(f"\nTranscribed {len(results)} file(s)")


def cmd_summarize():
    """Summarize a transcript."""
    from src.transcribe import Transcript
    from src.summarize import summarize

    if len(sys.argv) < 3:
        print("Usage: python plaud.py summarize <transcript.json>")
        print("  --provider openai|anthropic|ollama (default: ollama)")
        print("  --model <model_name>")
        return

    path = Path(sys.argv[2])
    if not path.exists():
        print(f"Not found: {path}")
        return

    provider = "ollama"
    model = None
    if "--provider" in sys.argv:
        idx = sys.argv.index("--provider")
        provider = sys.argv[idx + 1]
    if "--model" in sys.argv:
        idx = sys.argv.index("--model")
        model = sys.argv[idx + 1]

    transcript = Transcript.load(str(path))
    print(f"Transcript: {len(transcript.segments)} segments, {transcript.duration:.0f}s")
    print(f"Provider: {provider}, Model: {model or 'default'}")

    summary = summarize(transcript, provider=provider, model=model)

    summary_path = path.with_suffix(".summary.json")
    md_path = path.with_suffix(".summary.md")
    summary.save(str(summary_path))
    md_path.write_text(summary.formatted, encoding="utf-8")

    print(f"\n{summary.formatted}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    command = sys.argv[1].lower().replace("-", "_")

    # Async commands (BLE)
    async_commands = {
        "scan": cmd_scan,
        "connect": cmd_connect,
        "list": cmd_list,
        "download": cmd_download,
        "battery": cmd_battery,
    }

    # Sync commands
    sync_commands = {
        "cloud_list": cmd_cloud_list,
        "cloud_download": cmd_cloud_download,
        "transcribe": cmd_transcribe,
        "transcribe_all": cmd_transcribe_all,
        "summarize": cmd_summarize,
    }

    if command in async_commands:
        asyncio.run(async_commands[command]())
    elif command in sync_commands:
        sync_commands[command]()
    else:
        print(f"Unknown command: {sys.argv[1]}")
        print(__doc__)


if __name__ == "__main__":
    main()
