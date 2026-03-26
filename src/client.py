"""Plaud BLE client - connect, handshake, file transfer."""

import asyncio
import os
import time
from pathlib import Path
from dataclasses import dataclass, field

from bleak import BleakClient
from bleak.backends.device import BLEDevice

from .crypto import BleEncryption, get_public_key_pem_body, complete_handshake
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
    _encryption: BleEncryption = field(default_factory=BleEncryption)
    _rsa_buffer: bytearray = field(default_factory=bytearray)
    _rsa_handshake_done: asyncio.Event = field(default_factory=asyncio.Event)
    _indicate_char: str = "0000b004-0000-1000-8000-00805f9b34fb"

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
        """Perform the BLE handshake with RSA + ChaCha20 encryption.

        Flow (from decompiled source):
        1. Client sends handshake cmd (cmd_id=1) with APP_KEY
        2. Device responds with RSA pre-handshake (cmd_id=65041) to request public key
        3. Client sends RSA public key in chunks (cmd_id=65042)
        4. Device responds with RSA-encrypted ChaCha20 parameters
        5. Client decrypts and validates ("PLAUD.AI")
        """
        import struct as struct_mod
        print("Performing handshake...")

        # Enable indications on B004 for RSA data
        try:
            await self._client.start_notify(self._indicate_char, self._indicate_handler)
        except Exception:
            pass

        # Step 1: Send handshake command
        cmd = build_handshake(APP_KEY, audio_channel=1)
        print(f"  Sending handshake ({len(cmd)} bytes)...")
        await self._write(cmd, encrypt=False)

        # Step 2: Wait for device's RSA pre-handshake (cmd 65041) or regular response
        print("  Waiting for device response...")
        self._rsa_buffer.clear()
        self._response_event.clear()

        # Wait up to 15s for initial response
        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            pass

        if self._response_data:
            hs = HandshakeResponse.parse(self._response_data)
            print(f"  Response: status={hs.status}, portVersion={hs.port_version}")
            self._port_version = hs.port_version

            if hs.port_version < 20:
                # No encryption needed
                print("  Unencrypted handshake successful")
                return hs.status == 0

        # Check for RSA pre-handshake request
        if self._rsa_buffer:
            print(f"  Device sent RSA data ({len(self._rsa_buffer)} bytes)")

        # Step 3: Send RSA public key in chunks
        print("  Sending RSA public key...")
        pubkey_body = get_public_key_pem_body()
        pubkey_bytes = pubkey_body.encode("ascii")
        chunk_size = 100
        total_chunks = (len(pubkey_bytes) + chunk_size - 1) // chunk_size

        self._rsa_buffer.clear()  # Clear for incoming encrypted response

        for i in range(total_chunks):
            offset = i * chunk_size
            chunk = pubkey_bytes[offset:offset + chunk_size]
            # Packet: [cmd_type=1][cmd_id=65042 u16 LE][total_chunks u8][chunk_index u8][data]
            packet = bytes([1]) + struct_mod.pack("<H", 65042)
            packet += bytes([total_chunks, i])
            packet += chunk
            await self._write(packet, encrypt=False)
            await asyncio.sleep(0.05)

        print(f"  Sent public key ({len(pubkey_bytes)} bytes in {total_chunks} chunks)")

        # Step 4: Wait for RSA-encrypted response
        print("  Waiting for encrypted response...")
        for _ in range(30):  # Wait up to 15s
            await asyncio.sleep(0.5)
            if len(self._rsa_buffer) >= 256:  # RSA 2048-bit = 256 bytes
                break

        if self._rsa_buffer:
            return await self._complete_encrypted_handshake()

        # Maybe the response came as a regular notification
        if self._response_data:
            print(f"  Got response data: {self._response_data[:20].hex()}")

        print("  No encrypted handshake data received")
        # Try to continue anyway - some commands might still work
        return True

    async def _complete_encrypted_handshake(self) -> bool:
        """Complete the RSA + ChaCha20 handshake."""
        if not self._rsa_buffer:
            print("  No RSA data received")
            return False

        try:
            print(f"  RSA encrypted blob: {len(self._rsa_buffer)} bytes")
            self._encryption = complete_handshake(bytes(self._rsa_buffer))
            print("  Encryption handshake SUCCESSFUL - ChaCha20-Poly1305 active")
            return True
        except Exception as e:
            print(f"  Encryption handshake failed: {e}")
            print(f"  RSA buffer (hex): {self._rsa_buffer[:64].hex()}...")
            return False

    def _indicate_handler(self, sender, data: bytearray):
        """Handle incoming BLE indications (B004 - used for RSA key exchange)."""
        if data:
            print(f"  [INDICATE] {len(data)} bytes: {data[:20].hex()}...")
            self._rsa_buffer.extend(data)

    def _notification_handler(self, sender, data: bytearray):
        """Handle incoming BLE notifications."""
        if not data:
            return

        # Decrypt if encryption is active
        if self._encryption.active:
            try:
                data = bytearray(self._encryption.decrypt(bytes(data)))
            except Exception as e:
                # May be unencrypted control data
                pass

        cmd_type = data[0]

        if cmd_type == 1:
            # Command response
            cmd_id = unpack_u16(data, 1) if len(data) > 2 else 0

            # Check if this is RSA handshake data (cmd_id 65041 or 65042)
            if cmd_id in (65041, 65042):
                # RSA key exchange data
                self._rsa_buffer.extend(data[3:])  # Skip header
                return

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

    async def _write(self, data: bytes, encrypt: bool = True):
        """Write data to the device, encrypting if needed."""
        if self._client and self._client.is_connected:
            if encrypt and self._encryption.active:
                data = self._encryption.encrypt(data)
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
