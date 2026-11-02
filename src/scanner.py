"""Parse Plaud BLE advertising data (used by app/state/device.py scan_devices)."""

from __future__ import annotations

import struct

# Project codes (device models), as they appear in serial-number prefixes and
# the older advertising format.
PROJECT_CODES = {
    712: "IzyRec",
    880: "PLAUD NotePin",
    881: "PLAUD Note Pro",
    882: "Plaud NotePin S",
    888: "Plaud Note",
    901: "G101 Glasses",
}


def parse_manufacturer_data(mfr_data: dict) -> dict | None:
    """Parse Plaud manufacturer-specific advertising data.

    Returns dict with project_code, version, serial_number, port_version if Plaud device.
    """
    for company_id, data in mfr_data.items():
        if len(data) < 6:
            continue

        try:
            # Plaud NotePin S uses company ID 0x005D with this format:
            # [length(1)][version_type(1)][version_number(2 LE)][unknown(1)][sn_length(1)]
            # [serial_number(sn_length bytes, raw hex)][...][port_version(1)]
            if len(data) >= 14 and data[1] == 0x56:  # 'V' = version type marker
                version_number = struct.unpack_from("<H", data, 2)[0]
                sn_length = data[5]

                if 6 + sn_length <= len(data):
                    sn_raw = data[6 : 6 + sn_length]
                    serial_number = sn_raw.hex().upper()

                    # Detect model from SN prefix
                    model = "Unknown Plaud"
                    sn_prefix = serial_number[:3]
                    for code, name in PROJECT_CODES.items():
                        if serial_number.startswith(str(code)):
                            model = name
                            break

                    # Port version is at offset 6 + sn_length + 1
                    port_version = 0
                    pv_offset = 6 + sn_length + 1
                    if pv_offset < len(data):
                        port_version = data[pv_offset]

                    return {
                        "company_id": company_id,
                        "project_code": int(sn_prefix) if sn_prefix.isdigit() else 0,
                        "model": model,
                        "version_type": chr(data[1]),
                        "version": version_number,
                        "serial_number": serial_number,
                        "port_version": port_version,
                    }

            # Fallback: try original project code parsing
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

                offset = 3
                if offset < len(data):
                    result["version_type"] = chr(data[offset]) if data[offset] < 128 else "?"
                    offset += 1

                if offset + 2 <= len(data):
                    result["version"] = struct.unpack_from("<H", data, offset)[0]
                    offset += 2

                if offset < len(data):
                    sn_bytes = data[offset:]
                    if len(sn_bytes) > 2:
                        result["serial_number"] = sn_bytes[:-2].decode("ascii", errors="replace")
                        result["port_version"] = struct.unpack_from("<H", sn_bytes, len(sn_bytes) - 2)[0]
                    else:
                        result["serial_number"] = sn_bytes.decode("ascii", errors="replace")

                return result
        except (struct.error, IndexError):
            continue

    return None
