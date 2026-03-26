#!/usr/bin/env python3
"""Plaud NotePin S - Open Source BLE Client & Cloud Downloader.

Usage:
    python plaud.py scan              - Scan for nearby Plaud devices
    python plaud.py connect           - Connect and show device info
    python plaud.py list              - List recordings on device (via BLE)
    python plaud.py download          - Download all recordings from device (via BLE)
    python plaud.py cloud-list        - List recordings in cloud
    python plaud.py cloud-download    - Download all recordings from cloud
    python plaud.py battery           - Show battery level
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
TARGET_SN = os.environ.get("PLAUD_SN", "8820B50256947317")


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

    # Sync commands (cloud API)
    sync_commands = {
        "cloud_list": cmd_cloud_list,
        "cloud_download": cmd_cloud_download,
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
