"""Plaud BLE protocol - packet building and parsing.

All integers are little-endian.
"""

import struct
import time
from dataclasses import dataclass


def pack_u8(val: int) -> bytes:
    return struct.pack("<B", val & 0xFF)


def pack_u16(val: int) -> bytes:
    return struct.pack("<H", val & 0xFFFF)


def pack_u32(val: int) -> bytes:
    return struct.pack("<I", val & 0xFFFFFFFF)


def unpack_u8(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from("<B", data, offset)[0]


def unpack_u16(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def unpack_u32(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def unpack_u64(data: bytes, offset: int = 0) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def build_command(cmd_id: int, payload: bytes = b"") -> bytes:
    """Build a command packet: [cmd_type=1][cmd_id u16 LE][payload]."""
    return pack_u8(1) + pack_u16(cmd_id) + payload


def build_handshake(app_key: str, audio_channel: int = 1, serial_number: str = "") -> bytes:
    """Build handshake request (cmd_id=1)."""
    payload = app_key.encode("ascii")
    payload += pack_u8(audio_channel)
    payload += serial_number.encode("ascii")
    return build_command(1, payload)


def build_sync_time() -> bytes:
    """Build sync time request (cmd_id=4) with current timestamp."""
    now = int(time.time())
    # Get local timezone offset
    local_offset = time.timezone if time.daylight == 0 else time.altzone
    tz_hours = -local_offset // 3600
    tz_mins = (-local_offset % 3600) // 60
    payload = pack_u32(now) + struct.pack("<b", tz_hours) + struct.pack("<b", tz_mins)
    return build_command(4, payload)


def build_get_state() -> bytes:
    """Build get state request (cmd_id=3)."""
    return build_command(3)


def build_get_file_list() -> bytes:
    """Build get file list request (cmd_id=26)."""
    return build_command(26)


def build_sync_file_start(session_id: int, start_offset: int = 0, end_offset: int = 0) -> bytes:
    """Build sync file start request (cmd_id=28)."""
    payload = pack_u32(session_id) + pack_u32(start_offset) + pack_u32(end_offset)
    return build_command(28, payload)


def build_stop_sync_file() -> bytes:
    """Build stop sync file request (cmd_id=30)."""
    return build_command(30)


def build_get_storage() -> bytes:
    """Build get storage request (cmd_id=6)."""
    return build_command(6)


@dataclass
class HandshakeResponse:
    status: int
    port_version: int
    raw: bytes

    @staticmethod
    def parse(data: bytes) -> "HandshakeResponse":
        # data starts after cmd_type(1) + cmd_id(2) = offset 3
        status = unpack_u8(data, 3) if len(data) > 3 else -1
        port_version = unpack_u16(data, 4) if len(data) > 5 else 0
        return HandshakeResponse(status=status, port_version=port_version, raw=data)


@dataclass
class BleFile:
    session_id: int
    file_size: int
    codec_type: int = 0
    channel_type: int = 1

    @property
    def duration_ms(self) -> int:
        """Calculate duration from file size. 80 bytes per 20ms frame per channel."""
        if self.channel_type == 0:
            self.channel_type = 1
        return int((self.file_size / (self.channel_type * 80)) * 20)

    @property
    def duration_str(self) -> str:
        total_seconds = self.duration_ms // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}m {seconds}s"


def parse_file_list(data: bytes) -> list[BleFile]:
    """Parse file list response.
    After header (3 bytes), each file entry contains:
    - session_id: u32
    - file_size: u32
    - codec_type: u8
    - channel_type: u8
    Total: 10 bytes per entry
    """
    files = []
    offset = 3  # skip cmd_type + cmd_id

    # First byte after header might be count or status
    if len(data) <= offset:
        return files

    entry_size = 10  # 4 + 4 + 1 + 1
    remaining = data[offset:]

    # Try to detect the format by checking if we get sensible values
    # The response might have a status byte or count first
    # Let's try parsing from different offsets
    for start in [0, 1, 2]:
        test_data = remaining[start:]
        if len(test_data) >= entry_size:
            test_sid = unpack_u32(test_data, 0)
            test_size = unpack_u32(test_data, 4)
            # Sanity check: session_id should be > 0, file_size should be reasonable
            if 0 < test_sid < 0xFFFFFFFF and 0 < test_size < 0x80000000:
                # This offset works, parse all entries
                pos = start
                while pos + entry_size <= len(remaining):
                    sid = unpack_u32(remaining, pos)
                    fsize = unpack_u32(remaining, pos + 4)
                    codec = unpack_u8(remaining, pos + 8) if pos + 9 <= len(remaining) else 0
                    channel = unpack_u8(remaining, pos + 9) if pos + 10 <= len(remaining) else 1
                    if sid == 0 and fsize == 0:
                        break
                    files.append(
                        BleFile(
                            session_id=sid,
                            file_size=fsize,
                            codec_type=codec,
                            channel_type=max(channel, 1),
                        )
                    )
                    pos += entry_size
                if files:
                    break

    return files


@dataclass
class FileDataPacket:
    """Parsed file data packet (cmd_type=2)."""

    session_id: int
    offset: int
    data: bytes
    is_end: bool = False
    error_code: int = 0

    @staticmethod
    def parse(raw: bytes, port_version: int = 20) -> "FileDataPacket":
        """Parse a file data packet."""
        if len(raw) < 2:
            return FileDataPacket(0, 0, b"", is_end=True)

        if port_version >= 7:
            # [type=2][session_id u32][offset u32][length u8][data...]
            if len(raw) < 10:
                return FileDataPacket(0, 0, b"", is_end=True)
            session_id = unpack_u32(raw, 1)
            offset = unpack_u32(raw, 5)
            if offset == 0xFFFFFFFF:
                error_code = unpack_u8(raw, 9) if len(raw) > 9 else 0
                return FileDataPacket(session_id, 0, b"", is_end=True, error_code=error_code)
            data_len = unpack_u8(raw, 9)
            data = raw[10 : 10 + data_len]
            return FileDataPacket(session_id, offset, data)
        else:
            # [type=2][offset u32][length u8][data...]
            offset = unpack_u32(raw, 1)
            if offset == 0xFFFFFFFF:
                return FileDataPacket(0, 0, b"", is_end=True)
            data_len = unpack_u8(raw, 5)
            data = raw[6 : 6 + data_len]
            return FileDataPacket(0, offset, data)


@dataclass
class DeviceState:
    recording: bool = False
    privacy_enabled: bool = False
    key_state: int = 0
    usb_state: int = 0
    find_my_state: int = 0
    scene: int = 0
    session_id: int = 0
    raw: bytes = b""

    @staticmethod
    def parse(data: bytes) -> "DeviceState":
        """Parse device state response. Format varies by firmware."""
        state = DeviceState(raw=data)
        if len(data) > 3:
            state.recording = unpack_u8(data, 3) == 1
        if len(data) > 4:
            state.privacy_enabled = unpack_u8(data, 4) == 1
        return state
