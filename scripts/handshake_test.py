#!/usr/bin/env python3
"""One-shot handshake test. Press device button, then run immediately."""
import asyncio, struct, base64, json, sys
from pathlib import Path
from bleak import BleakClient, BleakScanner
from src.crypto import rsa_decrypt, load_rsa_private_key, get_public_key_pem_body
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305, AESGCM

# Device credentials — read from production app config
CONFIG_PATH = Path.home() / "Library/Application Support/Plaud V2/.plaud_v2_config.json"
try:
    with open(CONFIG_PATH) as _f:
        _cfg = json.load(_f)
except FileNotFoundError:
    sys.exit(f"Config not found at {CONFIG_PATH}. Run the Plaud V2 app first to pair a device.")

SN = "8820B50256947317"
SN_SIG = _cfg.get("device_sn_signatures", {}).get(SN)
if not SN_SIG:
    sys.exit(f"No SN_SIG for SN {SN} in config — re-pair device via the Plaud V2 app first.")

W = "00002bb1-0000-1000-8000-00805f9b34fb"
N = "00002bb0-0000-1000-8000-00805f9b34fb"

async def main():
    print("Scanning 12s...")
    found = await BleakScanner.discover(timeout=12, return_adv=True)
    t = None
    for a,(d,adv) in found.items():
        for c in adv.manufacturer_data:
            if c==0x5D: t=d
    if not t: print("NOT FOUND - press button and retry"); return

    for i in range(3):
        try:
            cl = BleakClient(t, timeout=30.0)
            await cl.connect(); print("Connected!"); break
        except: await asyncio.sleep(2)
    else: print("Connect failed"); return

    # Collect everything
    phase1 = []  # Before pubkey send
    phase2 = []  # After pubkey send
    current_phase = [1]

    def h(sender, data):
        data = bytes(data)
        cid = struct.unpack_from("<H",data,0)[0] if len(data)>=2 else 0
        phase = current_phase[0]
        target = phase1 if phase == 1 else phase2
        target.append(data)
        print(f"  P{phase} cmd={cid}(0x{cid:04X}) [{len(data)}B]: {data.hex()}")

    await cl.start_notify(N, h)
    try: await cl.start_notify("0000b004-0000-1000-8000-00805f9b34fb", h)
    except: pass

    # Step 1: SN Sig
    sig = base64.b64decode(SN_SIG)
    tc = (len(sig)+99)//100
    print(f"\n--- Step 1: SN Sig ({tc} chunks) ---")
    for i in range(tc):
        c = sig[i*100:(i+1)*100]
        await cl.write_gatt_char(W, struct.pack("<H",65040)+bytes([tc,i])+c, response=False)
        await asyncio.sleep(0.05)
    await asyncio.sleep(5)
    print(f"Phase 1: {len(phase1)} notifications")

    # Step 2: Public key
    current_phase[0] = 2
    pk = get_public_key_pem_body().encode()
    pc = (len(pk)+99)//100
    print(f"\n--- Step 2: PubKey ({pc} chunks) ---")
    for i in range(pc):
        c = pk[i*100:(i+1)*100]
        await cl.write_gatt_char(W, struct.pack("<H",65042)+bytes([pc,i])+c, response=False)
        await asyncio.sleep(0.1)
    await asyncio.sleep(10)
    print(f"Phase 2: {len(phase2)} notifications")

    # Save everything
    save = {
        "phase1": [p.hex() for p in phase1],
        "phase1_count": len(phase1),
        "phase2": [p.hex() for p in phase2],
        "phase2_count": len(phase2),
    }

    # Try RSA decrypt on phase 2 data (the real RSA blob)
    for phase_name, packets in [("phase2", phase2), ("phase1", phase1)]:
        chunks = {}
        for pkt in packets:
            if len(pkt)>=4:
                cid = struct.unpack_from("<H",pkt,0)[0]
                if cid == 65042:
                    idx = pkt[3]
                    if idx not in chunks:
                        chunks[idx] = pkt

        if not chunks: continue

        blob = bytearray()
        for i in sorted(chunks.keys()):
            blob.extend(chunks[i][4:])

        print(f"\n{phase_name} RSA blob: {len(blob)}B")
        save[f"{phase_name}_blob"] = blob.hex()
        save[f"{phase_name}_blob_len"] = len(blob)

        if len(blob) == 256:
            dec = rsa_decrypt(bytes(blob), load_rsa_private_key())
            print(f"Decrypted: {len(dec)}B")
            print(f"Hex: {dec.hex()}")
            save[f"{phase_name}_dec"] = dec.hex()
            save[f"{phase_name}_dec_len"] = len(dec)

            if len(dec) >= 56:
                k,n2,a2,ct = dec[:32],dec[32:44],dec[44:56],dec[56:]
                print(f"key:   {k.hex()}")
                print(f"nonce: {n2.hex()}")
                print(f"aad:   {a2.hex()}")
                print(f"ct:    {ct.hex()} ({len(ct)}B)")
                for nm,fn in [
                    ("ChaCha20+AAD", lambda: ChaCha20Poly1305(k).decrypt(n2,ct,a2)),
                    ("AES-GCM+AAD", lambda: AESGCM(k).decrypt(n2,ct,a2)),
                ]:
                    try:
                        pt=fn()
                        print(f"\n*** {nm}: {pt} ***")
                        if pt==b"PLAUD.AI":
                            print("========= HANDSHAKE COMPLETE! =========")
                            save["status"]="SUCCESS"
                            save["cipher"]=nm
                    except Exception as e:
                        print(f"  {nm}: {e}")

    with open("rsa_session.json","w") as f: json.dump(save,f,indent=2)
    print("\nSaved to rsa_session.json")
    await cl.disconnect()

asyncio.run(main())
