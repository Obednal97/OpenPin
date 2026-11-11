# Device adapters: design

Date: 2026-11-08. Status: agreed in chat with the owner; to be planned after the code trim (`docs/reviews/2026-11-08-overengineering-review.md`, items 1 to 8) has merged.

## Why

Everything that knows about the Plaud NotePin S lives in one 2,800-line module, `app/state/device.py`, plus `utils/wifi_sync_worker.py`, `utils/ble_proto.py`, `utils/pairing_store.py`, `utils/pairing_file.py` and `src/crypto.py`. The owner wants the app open to other recorders in future, without a second device in mind today. Adding one should mean writing one new folder, not editing sync, transcription or the screens.

Downstream of a download (transcription, coaching, Notion, Google Drive, the MCP server) the app already works only on audio files on disk, so the boundary is between "talk to a device" and "everything else".

## Goals

- A small device contract and a NotePin adapter behind it, with today's NotePin code moved in.
- The generic parts of syncing live once, in the app, so a new device doesn't rewrite them.
- `app/state/device.py` no longer exists as a NotePin module.
- A paper check (written, not built) that a recorder which mounts as a USB drive would fit in about 150 lines with no changes outside its own folder. The check is part of the deliverable: a short section appended to this document, or to the plan, listing any app change it found.

## Non-goals

- No second adapter is built.
- No list of devices, device picker, or several devices paired at once. One device paired at a time, as today.
- No plugin system or loading adapters from outside the app. A new device is code added to the repo and shipped in a release.
- No new user-facing features or settings.

## Anti-goals (a failure even if it works)

- Any change in NotePin behaviour: pairing, Bluetooth sync, Wi-Fi fast sync, find-my, battery, delete-after-sync, the Settings Device panel, or the auto-sync timing.
- Existing users losing anything: config keys (`paired_devices`, `deleted_ids`, etc.), Keychain pairing backup, recordings named `<session_id>.ogg`, transcripts, Notion links. No data migration should be needed.
- A contract shaped by guesses about future devices. Every method in it must be something the NotePin does today. Anything only the NotePin needs stays inside the NotePin folder.
- More code overall than before, beyond the contract itself (the owner wants lean code; see the trim review).

## Constraints

- One HTTP API for the frontend stays as it is (`/device/*` routes in `app/routes/device.py`). The frontend should barely change; the NotePin setup and pairing screens stay as they are.
- Only three modules call the device module today: `app/routes/device.py`, `app/api_server.py`, `app/routes/diagnostics.py`. They should call the active adapter (or the shared sync code) instead.
- The Wi-Fi fast sync runs as a separate worker process (`wifi_sync_worker.py`) that joins the NotePin's hotspot. It is NotePin transport and moves with the adapter unchanged.
- Bluetooth runs on one asyncio loop via `run_async`; that stays shared app infrastructure, not part of an adapter.
- Recording ids are strings to the app (`utils/paths.validate_session_id`). The NotePin keeps its numeric session ids unchanged.
- New Python packages must be added to `bundle.resources` in `src-tauri/tauri.conf.json` (there is a test for it).
- Tests: `~/.venvs/plaud-v2/bin/python -m pytest -q`, `ruff`; many tests patch `app.state.device` names, so moving code means updating patch targets. Work in small steps with the suite green after each.

## Decided

The contract (`app/devices/base.py`), required for every device:

- find (scan) and connect, disconnect, current status (connected, name, serial)
- list recordings: id, size, a start time, duration estimate
- download one recording into the recordings folder as `<id>.ogg` (Ogg Opus, the format the library, player and transcription already use), reporting bytes as it goes
- delete one recording from the device

Optional extras the device declares, so the UI only shows what a device has: battery, signal strength, find-my (back since #126), Wi-Fi fast sync, device info for the Settings panel. (Recording start/stop over Bluetooth was deleted in the trim as unused, so it is not in the contract.)

Layout:

- `app/devices/base.py`: the contract.
- `app/devices/notepin/`: the NotePin adapter: Bluetooth handshake, pairing and serial signing, decryption, Opus framing, CRC checks, Wi-Fi worker, device info. Also the NotePin-only parts of `app/state/cloud.py`: `bind_device`, `fetch_sn_signature`, the stored bind tokens and SN signatures, and `extract_recording_date` (builder's call: moved because only the NotePin uses them).
- `app/devices/__init__.py`: returns the adapter for the paired device. With one adapter, this is a few lines.
- Shared, in the app: the sync loop (which recordings are missing locally or in Google Drive, `deleted_ids`, retry caps, progress via `utils/sync_progress.py`, sync claim/release, auto-delete of verified files), the keep-awake helper, the asyncio loop, log scrubbing.

Calls made after the builder check (owner can overrule):

- Wi-Fi fast sync skips the same recordings as Bluetooth since #127 (`device._wifi_skip_ids`: deleted, in any recordings folder, in Drive). That set is shared app code; the Wi-Fi transport stays NotePin-only. It still has no auto-delete, on purpose.
- The plan is written against master after the trim has merged. The review's "next in line" de-duplications on the device path (BLE/Wi-Fi save paths, RSA handshake and BIND_TOKEN check, credential auto-fetch: section 2.8) are out of scope. This work only moves code. Once both copies sit in one folder, merging them is an easy follow-up.
- Plaud Cloud sign-in, cloud recordings (`download_cloud_recordings`) and `routes/cloud.py` stay where they are: they are a Plaud service integration, not the device link. Plaud sign-in remains part of NotePin pairing only; a future non-Plaud device wouldn't need it.

## Left to the builder

- Exact method names and whether the contract is a `Protocol`, an abstract class or a plain module interface. Prefer the smallest.
- How download progress is reported (callback or generator).
- Where the line falls inside today's `_sync_files_inner` and `_do_wifi_sync_locked`: the generic "work out what's missing" pass versus device transport. Wi-Fi sync may stay a NotePin-only path that reuses the shared "what's missing" pass rather than the full shared loop.
- Recording start time: today parts of the app derive it from the NotePin's numeric session id (e.g. `cloud.extract_recording_date`). The paper check should say what a non-NotePin id would need; changing that code is out of scope unless it's tiny.
- Whether `utils/ble_proto.py`, `pairing_store.py`, `pairing_file.py` and `src/crypto.py` move into the NotePin folder or stay put and are imported by it. Moving is preferred where it doesn't churn many imports.
