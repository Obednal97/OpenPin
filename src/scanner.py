"""Scan for Plaud BLE devices."""

import asyncio
import struct
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .constants import SERVICE_UUID, PROJECT_CODES


def parse_manufacturer_data(mfr_data: dict) -> dict | None:
    """Parse Plaud manufacturer-specific advertising data.

    Returns dict with project_code, version, serial_number, port_version if Plaud device.
    """
    for company_id, data in mfr_data.items():
        if len(data) < 6:
            continue

        # Try to extract project code (2 bytes LE after length byte)
        try:
            length = data[0]
            if length < 2 or length > len(data):
                continue

            project_code = struct.unpack_from("<H", data, 1)[0]

            if project_code in PROJECT_CODES:
                result = {
                    "company_id": company_id,
                    "project_code": project_code,
                    "model": PROJECT_CODES[project_code],
                }

                # Try to extract more fields
                offset = 3
                if offset < len(data):
                    result["version_type"] = chr(data[offset]) if data[offset] < 128 else "?"
                    offset += 1

                # Version number and serial vary by format
                if offset + 2 <= len(data):
                    result["version"] = struct.unpack_from("<H", data, offset)[0]
                    offset += 2

                # Remaining is serial number
                if offset < len(data):
                    sn_bytes = data[offset:]
                    # Try to find port version at end (2 bytes)
                    if len(sn_bytes) > 2:
                        result["serial_number"] = sn_bytes[:-2].decode("ascii", errors="replace")
                        result["port_version"] = struct.unpack_from("<H", sn_bytes, len(sn_bytes) - 2)[0]
                    else:
                        result["serial_number"] = sn_bytes.decode("ascii", errors="replace")

                return result
        except (struct.error, IndexError):
            continue

    return None


async def scan_for_plaud(timeout: float = 10.0) -> list[tuple[BLEDevice, dict]]:
    """Scan for Plaud BLE devices.

    Returns list of (device, parsed_info) tuples.
    """
    found_devices = []

    def detection_callback(device: BLEDevice, adv: AdvertisementData):
        # Check by name first (fastest)
        name = device.name or adv.local_name or ""
        is_plaud_name = any(name.upper().startswith(p) for p in ["PLAUD", "IZYREC"])

        # Check by service UUID
        has_service = SERVICE_UUID.lower() in [s.lower() for s in adv.service_uuids]

        # Check manufacturer data
        mfr_info = parse_manufacturer_data(adv.manufacturer_data) if adv.manufacturer_data else None

        if is_plaud_name or has_service or mfr_info:
            info = mfr_info or {}
            info["name"] = name
            info["rssi"] = adv.rssi
            info["address"] = device.address
            info["service_uuids"] = adv.service_uuids

            # Check if already found
            if not any(d[0].address == device.address for d in found_devices):
                found_devices.append((device, info))
                model = info.get("model", "Unknown")
                sn = info.get("serial_number", "?")
                print(f"  Found: {name} ({device.address}) - {model} SN:{sn} RSSI:{adv.rssi}")

    print(f"Scanning for Plaud devices ({timeout}s)...")
    scanner = BleakScanner(detection_callback=detection_callback)
    await scanner.start()
    await asyncio.sleep(timeout)
    await scanner.stop()

    if not found_devices:
        print("No Plaud devices found. Make sure your device is nearby and not connected to the phone app.")

    return found_devices


async def main():
    devices = await scan_for_plaud(timeout=15)
    print(f"\nFound {len(devices)} Plaud device(s)")
    for device, info in devices:
        print(f"\n  Device: {info.get('name', '?')}")
        print(f"  Address: {device.address}")
        print(f"  Model: {info.get('model', 'Unknown')}")
        print(f"  Serial: {info.get('serial_number', '?')}")
        print(f"  Port Version: {info.get('port_version', '?')}")
        print(f"  RSSI: {info.get('rssi', '?')}")
        if info.get("service_uuids"):
            print(f"  Services: {info['service_uuids']}")


if __name__ == "__main__":
    asyncio.run(main())
