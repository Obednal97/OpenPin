#!/usr/bin/env python3
"""Device pairing — no RSA path (firmware v1.0.10 doesn't support RSA).

Press device button, then run immediately.
Retries automatically every 5s until device responds.
"""
import asyncio, struct, base64
from bleak import BleakClient, BleakScanner

SN_SIG = "<REDACTED_SN_SIGNATURE>"
BIND_TOKEN = "<REDACTED_JWT>"
W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"


async def attempt():
    found = await BleakScanner.discover(timeout=5, return_adv=True)
    t = None
    for a, (d, adv) in found.items():
        for c in adv.manufacturer_data:
            if c == 0x5D:
                t = d
    if not t:
        return None

    try:
        cl = BleakClient(t, timeout=15.0)
        await cl.connect()
    except:
        return None

    got_response = asyncio.Event()
    all_data = []

    def h(sender, data):
        data = bytes(data)
        all_data.append(data)
        cid = struct.unpack_from("<H", data, 0)[0] if len(data) >= 2 else 0

        # Log everything
        if cid == 65041:
            print(f"  [65041 ACK]")
        elif cid == 65042:
            idx = data[3] if len(data) > 3 else -1
            print(f"  [65042 chunk {idx}]")
        elif len(data) >= 3 and data[0] == 1:
            cid2 = struct.unpack_from("<H", data, 1)[0]
            print(f"  [HANDSHAKE RESPONSE cmd={cid2} {len(data)}B]: {data.hex()}")
            got_response.set()
        else:
            print(f"  [cmd={cid} {len(data)}B]")

    await cl.start_notify(N, h)

    # === Step 1: SN sig with force clear ===
    sig = base64.b64decode(SN_SIG)
    tc = (len(sig) + 99) // 100
    print("  Sending SN sig (65056)...", end="", flush=True)
    for i in range(tc):
        c = sig[i * 100:(i + 1) * 100]
        await cl.write_gatt_char(W, struct.pack("<H", 65056) + bytes([tc, i]) + c, response=False)
        await asyncio.sleep(0.05)
    print(" done")

    # Wait for ANY notification (65041 or 65042)
    await asyncio.sleep(3)
    if not all_data:
        print("  No response to SN sig")
        await cl.disconnect()
        return None

    # === Step 2: Regular handshake (no RSA — the else branch of m38308D2) ===
    all_data.clear()
    got_response.clear()

    # C9555a0 format: [1][01 00][02][00][00][token32][zeros to 255]
    buf = bytearray(255)
    buf[0] = 1
    buf[1] = 1
    buf[2] = 0
    buf[3] = 2
    buf[4] = 0
    buf[5] = 0
    buf[6:6 + 32] = BIND_TOKEN[:32].encode("ascii")

    print("  Sending handshake...", end="", flush=True)
    await cl.write_gatt_char(W, bytes(buf), response=False)
    print(" done")

    # Wait for handshake response
    try:
        await asyncio.wait_for(got_response.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass

    # Show everything we got
    hs = [r for r in all_data if len(r) >= 3 and r[0] == 1]
    others = [r for r in all_data if not (len(r) >= 3 and r[0] == 1)]

    print(f"\n  Handshake responses: {len(hs)}")
    for r in hs:
        print(f"    {r.hex()}")

    print(f"  Other data: {len(others)}")
    for r in others[:5]:
        cid = struct.unpack_from("<H", r, 0)[0] if len(r) >= 2 else 0
        print(f"    cmd={cid}: {r.hex()[:60]}")

    await cl.disconnect()
    return hs


async def main():
    print("Pairing device (retrying every 5s)...\n")
    for i in range(30):
        print(f"--- Attempt {i + 1} ---")
        result = await attempt()
        if result is not None:
            if result:
                print("\n*** GOT HANDSHAKE RESPONSE! ***")
            break
        await asyncio.sleep(5)


asyncio.run(main())
