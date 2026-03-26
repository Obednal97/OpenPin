"""Plaud BLE client - connect, handshake, file transfer."""

import asyncio
import os
import time
from pathlib import Path
from dataclasses import dataclass, field

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from .constants import (
    SERVICE_UUID, NOTIFY_CHAR_UUID, WRITE_CHAR_UUID,
    BATTERY_LEVEL_UUID, APP_KEY,
    BLE_CONNECT_TIMEOUT, HANDSHAKE_TIMEOUT, PACKET_LOSS_TIMEOUT,
)
from .protocol import (
    build_handshake, build_sync_time, build_get_state, build_get_file_list,
    build_sync_file_start, build_stop_sync_file, build_get_storage,
    build_command, unpack_u8, unpack_u16, unpack_u32,
    HandshakeResponse, BleFile, FileDataPacket, DeviceState,
    parse_file_list,
)


@dataclass
class PlaudClient:
    """BLE client for Plaud voice recorders."""

    device: BLEDevice
    output_dir: str = "recordings"
    _client: BleakClient | None = None
    _port_version: int = 0
    _serial_number: str = ""
    _connected: bool = False
    _response_event: asyncio.Event = field(default_factory=asyncio.Event)
    _response_data: bytes = b""
    _file_buffer: bytearray = field(default_factory=bytearray)
    _file_offset: int = 0
    _file_transfer_done: asyncio.Event = field(default_factory=asyncio.Event)
    _file_crc: int = 0
    _encrypted: bool = False

    async def connect(self) -> bool:
        """Connect to the Plaud device and perform handshake."""
        print(f"Connecting to {self.device.name or self.device.address}...")

        self._client = BleakClient(
            self.device,
            timeout=BLE_CONNECT_TIMEOUT,
        )

        try:
            await self._client.connect()
        except Exception as e:
            print(f"Connection failed: {e}")
            return False

        print(f"Connected! MTU: {self._client.mtu_size}")

        # Enable notifications on the data characteristic
        await self._client.start_notify(NOTIFY_CHAR_UUID, self._notification_handler)
        print("Notifications enabled on data characteristic")

        # Perform handshake
        success = await self._do_handshake()
        if not success:
            print("Handshake failed!")
            await self.disconnect()
            return False

        # Sync time
        await self._send_and_wait(build_sync_time(), timeout=5.0)
        print("Time synced")

        self._connected = True
        return True

    async def disconnect(self):
        """Disconnect from the device."""
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(NOTIFY_CHAR_UUID)
            except Exception:
                pass
            await self._client.disconnect()
        self._connected = False
        print("Disconnected")

    async def get_battery_level(self) -> int:
        """Read battery level (0-100%)."""
        try:
            data = await self._client.read_gatt_char(BATTERY_LEVEL_UUID)
            return data[0] if data else -1
        except Exception:
            return -1

    async def get_device_state(self) -> DeviceState:
        """Get current device state."""
        response = await self._send_and_wait(build_get_state(), timeout=5.0)
        return DeviceState.parse(response) if response else DeviceState()

    async def get_file_list(self) -> list[BleFile]:
        """Get list of recordings on the device."""
        response = await self._send_and_wait(build_get_file_list(), timeout=10.0)
        if not response:
            print("No response to file list request")
            return []
        files = parse_file_list(response)
        return files

    async def download_file(self, ble_file: BleFile) -> bytes | None:
        """Download a recording file from the device.

        Returns raw Opus data or None on failure.
        """
        print(f"Downloading file session={ble_file.session_id} "
              f"size={ble_file.file_size} duration={ble_file.duration_str}...")

        self._file_buffer = bytearray()
        self._file_offset = 0
        self._file_transfer_done.clear()
        self._file_crc = 0

        # Send sync file start
        cmd = build_sync_file_start(
            ble_file.session_id,
            start_offset=0,
            end_offset=ble_file.file_size,
        )
        await self._write(cmd)

        # Wait for file transfer to complete
        start_time = time.time()
        last_progress = 0

        while not self._file_transfer_done.is_set():
            try:
                await asyncio.wait_for(
                    self._file_transfer_done.wait(),
                    timeout=PACKET_LOSS_TIMEOUT,
                )
            except asyncio.TimeoutError:
                elapsed = time.time() - start_time
                if elapsed > 300:  # 5 minute absolute timeout
                    print("\nFile transfer timed out after 5 minutes")
                    await self._write(build_stop_sync_file())
                    return None

                # Show progress
                if ble_file.file_size > 0:
                    progress = len(self._file_buffer) * 100 // ble_file.file_size
                    if progress != last_progress:
                        print(f"\r  Progress: {progress}% ({len(self._file_buffer)}/{ble_file.file_size} bytes)", end="", flush=True)
                        last_progress = progress

                # Re-request from current position if no data received
                if self._file_offset > 0:
                    cmd = build_sync_file_start(
                        ble_file.session_id,
                        start_offset=self._file_offset,
                        end_offset=ble_file.file_size,
                    )
                    await self._write(cmd)

        print(f"\n  Download complete: {len(self._file_buffer)} bytes")
        return bytes(self._file_buffer)

    async def download_all(self) -> list[Path]:
        """Download all recordings and save to output directory."""
        files = await self.get_file_list()
        if not files:
            print("No files on device")
            return []

        print(f"\nFound {len(files)} recording(s):")
        for i, f in enumerate(files):
            print(f"  [{i+1}] Session {f.session_id}: {f.file_size} bytes ({f.duration_str})")

        os.makedirs(self.output_dir, exist_ok=True)
        saved_files = []

        for i, ble_file in enumerate(files):
            print(f"\n--- Downloading [{i+1}/{len(files)}] ---")
            data = await self.download_file(ble_file)
            if data:
                filename = f"recording_{ble_file.session_id}.opus"
                filepath = Path(self.output_dir) / filename
                filepath.write_bytes(data)
                print(f"  Saved: {filepath}")
                saved_files.append(filepath)
            else:
                print(f"  Failed to download session {ble_file.session_id}")

        return saved_files

    # --- Internal methods ---

    async def _do_handshake(self) -> bool:
        """Perform the BLE handshake."""
        print("Performing handshake...")
        cmd = build_handshake(APP_KEY, audio_channel=1)
        response = await self._send_and_wait(cmd, timeout=HANDSHAKE_TIMEOUT)

        if not response:
            print("No handshake response received")
            return False

        hs = HandshakeResponse.parse(response)
        print(f"  Handshake status: {hs.status}")
        print(f"  Port version: {hs.port_version}")
        print(f"  Response ({len(response)} bytes): {response.hex()}")

        self._port_version = hs.port_version

        if hs.status != 0:
            print(f"  Handshake rejected with status {hs.status}")
            # Status != 0 doesn't always mean failure - some devices return
            # status codes that require encryption negotiation
            if hs.port_version >= 20:
                print("  Device requires encrypted communication (portVersion >= 20)")
                print("  Encryption not yet implemented - trying unencrypted...")
                # For now, continue and see if basic commands work
                # The encryption negotiation happens via RSA + ChaCha20-Poly1305

        return True

    def _notification_handler(self, sender, data: bytearray):
        """Handle incoming BLE notifications."""
        if not data:
            return

        cmd_type = data[0]

        if cmd_type == 1:
            # Command response
            cmd_id = unpack_u16(data, 1) if len(data) > 2 else 0
            self._response_data = bytes(data)
            self._response_event.set()

        elif cmd_type == 2:
            # File data packet
            packet = FileDataPacket.parse(bytes(data), self._port_version)
            if packet.is_end:
                self._file_transfer_done.set()
            else:
                # Append data at the correct offset
                if packet.offset == self._file_offset:
                    self._file_buffer.extend(packet.data)
                    self._file_offset += len(packet.data)
                elif packet.offset > self._file_offset:
                    # Gap - pad with zeros (will re-request)
                    gap = packet.offset - self._file_offset
                    self._file_buffer.extend(b"\x00" * gap)
                    self._file_buffer.extend(packet.data)
                    self._file_offset = packet.offset + len(packet.data)

        elif cmd_type == 5:
            # Log data
            pass

    async def _write(self, data: bytes):
        """Write data to the device."""
        if self._client and self._client.is_connected:
            await self._client.write_gatt_char(WRITE_CHAR_UUID, data, response=False)

    async def _send_and_wait(self, data: bytes, timeout: float = 10.0) -> bytes | None:
        """Send a command and wait for response."""
        self._response_event.clear()
        self._response_data = b""

        await self._write(data)

        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=timeout)
            return self._response_data
        except asyncio.TimeoutError:
            return None
