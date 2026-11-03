"""Pairing backup: Keychain copy of the NotePin credentials, restore after a
wiped data folder, and Settings › Device export / import.

`security` is always faked here (FakeSecurity): the suite never touches a real
keychain. All credentials below are made up.
"""

import base64
import json
import logging
import os
import shlex
import stat
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.utils import pairing_file, pairing_store
from app.utils.config import load_config, save_config

SN = "8820A00012345678"
SN2 = "8820A000ABCDEF01"
SIG = base64.b64encode(bytes(range(256))).decode()
SIG2 = base64.b64encode(bytes(255 - i for i in range(256))).decode()
TOKEN = "Tk3nFakeBindToken0123456789abcdE"
TOKEN2 = "AnotherFakeToken_9876543210zyxwv"
SECRETS = (SIG, SIG2, TOKEN, TOKEN2)


class FakeSecurity:
    """Stands in for subprocess.run(["/usr/bin/security", ...]): an in-memory
    keychain that records every argv and stdin it was given."""

    def __init__(self):
        self.items = {}  # (service, account) -> password
        self.calls = []  # (argv, stdin)
        self.fail_rc = None  # e.g. 36: keychain locked
        self.fail_reads = set()  # accounts whose find-generic-password fails
        self.raise_exc = None

    def __call__(self, argv, input=None, capture_output=True, text=True, timeout=None):
        self.calls.append((list(argv), input))
        assert argv[0] == pairing_store.SECURITY
        assert timeout, "every security call needs a timeout"
        if self.raise_exc:
            raise self.raise_exc
        if self.fail_rc is not None:
            return subprocess.CompletedProcess(
                argv, self.fail_rc, "", "security: SecKeychainItemCreateFromContent: User interaction is not allowed.\n"
            )
        if argv[1] == "-i":
            words = shlex.split(input)
            return self._run(words, argv)
        return self._run(argv[1:], argv)

    @staticmethod
    def _opts(words):
        opts, i = {}, 1
        while i < len(words):
            w = words[i]
            if w in ("-U", "-w"):
                opts[w] = True
                i += 1
            elif w.startswith("-") and i + 1 < len(words):
                opts[w] = words[i + 1]
                i += 2
            else:
                opts.setdefault("keychain", w)
                i += 1
        return opts

    def _run(self, words, argv):
        cmd, o = words[0], self._opts(words)
        key = (o.get("-s"), o.get("-a"))
        if cmd == "add-generic-password":
            if key in self.items and not o.get("-U"):
                return subprocess.CompletedProcess(argv, 45, "", "already exists")
            self.items[key] = bytes.fromhex(o["-X"]).decode()
            return subprocess.CompletedProcess(argv, 0, "", "")
        if cmd == "find-generic-password":
            if o.get("-a") in self.fail_reads:
                return subprocess.CompletedProcess(argv, 36, "", "User interaction is not allowed.")
            if key not in self.items:
                return subprocess.CompletedProcess(argv, 44, "", "could not be found")
            return subprocess.CompletedProcess(argv, 0, self.items[key] + "\n", "")
        if cmd == "delete-generic-password":
            if self.items.pop(key, None) is None:
                return subprocess.CompletedProcess(argv, 44, "", "could not be found")
            return subprocess.CompletedProcess(argv, 0, "", "password has been deleted.\n")
        raise AssertionError(f"unexpected security command {cmd}")

    def device_item(self, serial):
        text = self.items.get((pairing_store.service_name(), serial))
        return json.loads(text) if text else None

    def put_device(self, serial, sig=SIG, token=TOKEN, name="Desk NotePin"):
        self.items[(pairing_store.service_name(), serial)] = json.dumps(
            {"serial": serial, "name": name, "sn_signature": sig, "bind_token": token}
        )
        index = set(json.loads(self.items.get((pairing_store.service_name(), "index"), "[]")))
        self.items[(pairing_store.service_name(), "index")] = json.dumps(sorted(index | {serial}))

    def assert_no_secret_in_argv(self):
        for argv, _stdin in self.calls:
            joined = " ".join(argv)
            for secret in SECRETS:
                assert secret not in joined
                assert secret.encode().hex() not in joined


@pytest.fixture
def kc(monkeypatch, tmp_config):
    """Keychain backup switched on, with `security` faked."""
    fake = FakeSecurity()
    monkeypatch.setattr(pairing_store, "available", lambda: True)
    monkeypatch.setattr(pairing_store.subprocess, "run", fake)
    monkeypatch.setattr(pairing_store, "_KEYCHAIN", None)
    pairing_store._warned.clear()
    yield fake
    pairing_store.flush(timeout=5)
    fake.assert_no_secret_in_argv()


def _paired_config(serials=((SN, SIG, TOKEN),)):
    return {
        "device_sn_signatures": {s: sig for s, sig, _ in serials},
        "device_bind_tokens": {s: tok for s, _, tok in serials},
        "paired_devices": {
            s: {"name": "Desk NotePin", "last_address": "ADDR", "last_connected": "x"} for s, *_ in serials
        },
    }


# ---------------------------------------------------------------------------
# Keychain wrapper
# ---------------------------------------------------------------------------


class TestKeychainWrapper:
    def test_save_and_load_round_trip(self, kc):
        assert pairing_store.save(SN, SIG, TOKEN, "Desk NotePin")
        assert pairing_store.load(SN) == {
            "serial": SN,
            "name": "Desk NotePin",
            "sn_signature": SIG,
            "bind_token": TOKEN,
        }
        assert pairing_store.list_serials() == [SN]

    def test_secret_goes_over_stdin_as_hex_never_argv(self, kc):
        pairing_store.save(SN, SIG, TOKEN)
        writes = [(argv, stdin) for argv, stdin in kc.calls if argv[1] == "-i"]
        assert writes, "writes must use `security -i`"
        for argv, stdin in writes:
            assert argv == [pairing_store.SECURITY, "-i"]
            assert stdin.endswith("\n") and stdin.count("\n") == 1
        device_write = next(stdin for _argv, stdin in writes if f'-a "{SN}"' in stdin)
        # Only hex on stdin, so the JSON (and its quotes) need no escaping.
        assert TOKEN not in device_write and SIG not in device_write
        kc.assert_no_secret_in_argv()

    def test_reads_pass_only_service_and_account(self, kc):
        kc.put_device(SN)
        pairing_store.load(SN)
        argv = kc.calls[-1][0]
        assert argv == [
            pairing_store.SECURITY,
            "find-generic-password",
            "-s",
            pairing_store.DEV_SERVICE,
            "-a",
            SN,
            "-w",
        ]

    def test_missing_item_is_none_without_warning(self, kc, caplog):
        with caplog.at_level(logging.WARNING):
            assert pairing_store.load(SN) is None
            assert pairing_store.list_serials() == []
        assert not caplog.records

    def test_warning_redacts_credential_like_text(self, caplog):
        pairing_store._warned.clear()
        with caplog.at_level(logging.WARNING):
            pairing_store._warn("write", 1, f"SecKeychainItemCreateFromContent {TOKEN.encode().hex()} {SIG} {TOKEN}")
        message = caplog.records[-1].getMessage()
        assert "SecKeychainItemCreateFromContent" in message
        for secret in (TOKEN, SIG, TOKEN.encode().hex()):
            assert secret not in message
        pairing_store._warned.clear()

    def test_locked_keychain_fails_soft_and_logs_once_without_values(self, kc, caplog):
        kc.fail_rc = 36
        with caplog.at_level(logging.DEBUG):
            assert pairing_store.save(SN, SIG, TOKEN) is False
            assert pairing_store.save(SN, SIG, TOKEN) is False
            assert pairing_store.load(SN) is None
            assert pairing_store.delete(SN) is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len([r for r in warnings if "write failed" in r.getMessage()]) == 1
        assert any("read failed" in r.getMessage() for r in warnings)
        for r in caplog.records:
            for secret in SECRETS:
                assert secret not in r.getMessage()

    @pytest.mark.parametrize(
        "exc", [subprocess.TimeoutExpired("security", 15), FileNotFoundError("security"), PermissionError()]
    )
    def test_security_that_cannot_run_fails_soft(self, kc, exc):
        kc.raise_exc = exc
        assert pairing_store.save(SN, SIG, TOKEN) is False
        assert pairing_store.load(SN) is None
        assert pairing_store.list_serials() == []
        assert pairing_store.restore_serial(SN) is None

    def test_malformed_item_is_ignored(self, kc):
        kc.items[(pairing_store.service_name(), SN)] = json.dumps(
            {"serial": SN, "sn_signature": "nope", "bind_token": TOKEN}
        )
        assert pairing_store.load(SN) is None
        kc.items[(pairing_store.service_name(), SN)] = "not json"
        assert pairing_store.load(SN) is None

    def test_malformed_credentials_are_not_saved(self, kc):
        assert pairing_store.save(SN, "not base64!", TOKEN) is False
        assert pairing_store.save("lowercase8820a000", SIG, TOKEN) is False
        assert not kc.calls

    def test_delete_removes_item_and_index_entry(self, kc):
        pairing_store.save(SN, SIG, TOKEN)
        pairing_store.save(SN2, SIG2, TOKEN2)
        assert pairing_store.delete(SN)
        assert pairing_store.load(SN) is None
        assert pairing_store.list_serials() == [SN2]
        assert pairing_store.delete(SN)  # already gone is fine

    def test_unreadable_index_is_not_overwritten(self, kc):
        kc.put_device(SN2, SIG2, TOKEN2)
        kc.fail_reads.add("index")
        assert pairing_store.save(SN, SIG, TOKEN)
        assert pairing_store.delete(SN2)
        kc.fail_reads.clear()
        assert pairing_store.list_serials() == [SN2]  # untouched, not reset to [SN]

    def test_deleting_without_an_index_creates_none(self, kc):
        kc.items[(pairing_store.service_name(), SN)] = "{}"
        assert pairing_store.delete(SN)
        assert kc.items == {}

    def test_disabled_backup_never_runs_security(self, monkeypatch, tmp_config):
        monkeypatch.setenv(pairing_store.ENABLE_ENV, "0")
        called = []
        monkeypatch.setattr(pairing_store.subprocess, "run", lambda *a, **k: called.append(a))
        assert pairing_store.available() is False
        assert pairing_store.save(SN, SIG, TOKEN) is False
        assert pairing_store.load(SN) is None
        assert pairing_store.restore_serial(SN) is None
        assert pairing_store.backup_serial_async(SN) is False
        assert pairing_store.sync_at_startup() == {"backed_up": 0, "restored": 0}
        assert not called

    def test_missing_keychain_file_is_refused_not_defaulted(self, kc, tmp_path, monkeypatch):
        # `security` silently falls back to the login keychain for a missing path.
        monkeypatch.setattr(pairing_store, "_KEYCHAIN", str(tmp_path / "gone.keychain-db"))
        assert pairing_store.save(SN, SIG, TOKEN) is False
        assert pairing_store.load(SN) is None
        assert not kc.calls

    def test_quote(self):
        assert pairing_store._quote("Plaud V2 pairing") == '"Plaud V2 pairing"'
        assert shlex.split(pairing_store._quote('a"b\\c')) == ['a"b\\c']
        with pytest.raises(ValueError):
            pairing_store._quote("a\nb")

    def test_service_name_separates_dev_from_release(self, tmp_path):
        from app.utils.constants import PACKAGED_DATA_DIR

        assert pairing_store.service_name(PACKAGED_DATA_DIR) == pairing_store.RELEASE_SERVICE
        assert pairing_store.service_name(tmp_path / "Plaud V2 (Dev)") == pairing_store.DEV_SERVICE
        assert pairing_store.service_name() == pairing_store.DEV_SERVICE  # the test data dir

    @pytest.mark.parametrize(
        "serial,ok",
        [
            (SN, True),
            ("8820A000", True),
            ("8820a00012345678", False),
            ("8820A0001234567", False),
            ("", False),
            ("index", False),
            ("SN_ABC", False),
            ("88" * 33, False),
        ],
    )
    def test_serial_validation(self, serial, ok):
        assert pairing_store.is_valid_serial(serial) is ok

    @pytest.mark.parametrize(
        "sig,ok", [(SIG, True), ("QUJD", False), (SIG[:-1], False), (SIG.replace("A", "-"), False), (None, False)]
    )
    def test_signature_validation(self, sig, ok):
        assert pairing_store.is_valid_signature(sig) is ok

    @pytest.mark.parametrize(
        "token,ok",
        [(TOKEN, True), ("x" * 64, True), ("x" * 65, False), ("has space", False), ("tökén", False), ("", False)],
    )
    def test_token_validation(self, token, ok):
        assert pairing_store.is_valid_token(token) is ok


# ---------------------------------------------------------------------------
# Restore after a wiped data folder
# ---------------------------------------------------------------------------


class TestRestore:
    def test_getters_restore_from_keychain_when_config_is_empty(self, kc, caplog):
        from app.state.cloud import get_device_bind_token, get_device_sn_signature

        kc.put_device(SN)
        with caplog.at_level(logging.INFO):
            assert get_device_sn_signature(SN) == SIG
        assert get_device_bind_token(SN) == TOKEN
        cfg = load_config()
        assert cfg["device_sn_signatures"][SN] == SIG
        assert cfg["device_bind_tokens"][SN] == TOKEN
        assert cfg["paired_devices"][SN] == {"name": "Desk NotePin"}  # no last_address
        messages = [r.getMessage() for r in caplog.records]
        assert any("Restored pairing for serial ending 5678 from the Keychain" in m for m in messages)
        for m in messages:
            assert SN not in m and SIG not in m and TOKEN not in m

    def test_restore_never_overwrites_config_values(self, kc):
        from app.state.cloud import get_device_bind_token

        save_config({"device_sn_signatures": {SN: SIG2}, "paired_devices": {SN: {"name": "Mine", "last_address": "A"}}})
        kc.put_device(SN)
        assert get_device_bind_token(SN) == TOKEN
        cfg = load_config()
        assert cfg["device_sn_signatures"][SN] == SIG2
        assert cfg["paired_devices"][SN] == {"name": "Mine", "last_address": "A"}

    def test_config_hit_skips_the_keychain(self, kc):
        from app.state.cloud import get_device_sn_signature

        save_config({"device_sn_signatures": {SN: SIG}})
        assert get_device_sn_signature(SN) == SIG
        assert not kc.calls

    def test_nothing_backed_up_returns_none(self, kc):
        from app.state.cloud import get_device_bind_token

        assert get_device_bind_token(SN) is None
        assert get_device_bind_token("") is None
        assert load_config() == {}

    def test_paired_serial_for_address_ignores_restored_entry(self, kc):
        from app.state import device as dev

        kc.put_device(SN)
        pairing_store.restore_serial(SN)
        assert dev.get_paired_serials() == {SN}
        assert dev.paired_serial_for_address("ADDR") is None


class TestBackupWriteSites:
    def _cloud_logged_in(self):
        # The bind token is the login JWT's `sub` claim, so no /user/me call is needed.
        part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
        jwt = f"{part({'alg': 'none'})}.{part({'sub': TOKEN})}.sig"
        return patch.dict("app.state.cloud._cloud_state", {"logged_in": True, "token": jwt, "api_domain": "https://x"})

    def test_bind_then_sign_backs_up_once_both_exist(self, kc):
        from app.state import cloud

        with self._cloud_logged_in(), patch.object(cloud, "cloud_api") as api:
            api.return_value = {"status": 0, "data": {}}
            assert cloud.bind_device(SN, name="Desk NotePin")["token"] == TOKEN
            pairing_store.flush(timeout=5)
            assert kc.device_item(SN) is None  # no signature yet
            # sn-sign no longer goes through cloud_api (Android headers, direct POST)
            sign_reply = Mock(
                status_code=200, content=json.dumps({"status": 0, "data": {"signature": {SN: SIG}}}).encode()
            )
            with patch.object(cloud.http_requests, "post", return_value=sign_reply):
                assert cloud.fetch_sn_signature(SN)["signature"] == SIG
            pairing_store.flush(timeout=5)
        item = kc.device_item(SN)
        assert item["sn_signature"] == SIG and item["bind_token"] == TOKEN
        assert pairing_store.list_serials() == [SN]

    def test_save_known_device_backs_up_with_its_name(self, kc):
        from app.state import device as dev

        save_config({"device_sn_signatures": {SN: SIG}, "device_bind_tokens": {SN: TOKEN}})
        dev.save_known_device("ADDR", name="Kitchen pin", serial_number=SN)
        pairing_store.flush(timeout=5)
        assert kc.device_item(SN)["name"] == "Kitchen pin"

    def test_unchanged_backup_is_not_rewritten(self, kc):
        save_config(_paired_config())
        assert pairing_store.backup_serial(SN)
        writes = sum(1 for argv, _ in kc.calls if argv[1] == "-i")
        assert pairing_store.backup_serial(SN)
        assert sum(1 for argv, _ in kc.calls if argv[1] == "-i") == writes

    def test_forget_deletes_backups(self, kc):
        kc.put_device(SN)
        kc.put_device(SN2, SIG2, TOKEN2)
        pairing_store.forget_async([SN, "unknown"])
        pairing_store.flush(timeout=5)
        assert kc.device_item(SN) is None
        assert pairing_store.list_serials() == [SN2]


class TestStartupSync:
    def test_backs_up_paired_devices_and_restores_lost_ones(self, kc):
        save_config(_paired_config())
        kc.put_device(SN2, SIG2, TOKEN2)
        assert pairing_store.sync_at_startup() == {"backed_up": 1, "restored": 1}
        assert kc.device_item(SN)["bind_token"] == TOKEN
        cfg = load_config()
        assert cfg["device_bind_tokens"][SN2] == TOKEN2
        assert SN2 in cfg["paired_devices"]

    def test_unpaired_credentials_are_not_backed_up(self, kc):
        # After "Erase & Disconnect" the credentials stay in the config but the
        # device isn't paired, so startup must not re-create its backup.
        save_config({"device_sn_signatures": {SN: SIG}, "device_bind_tokens": {SN: TOKEN}, "paired_devices": {}})
        assert pairing_store.sync_at_startup() == {"backed_up": 0, "restored": 0}
        assert kc.device_item(SN) is None

    def test_locked_keychain_at_startup_is_harmless(self, kc):
        save_config(_paired_config())
        kc.fail_rc = 36
        assert pairing_store.sync_at_startup() == {"backed_up": 0, "restored": 0}
        assert load_config() == _paired_config()

    def test_start_startup_sync_runs_in_background(self, kc):
        save_config(_paired_config())
        assert pairing_store.start_startup_sync()
        assert pairing_store.flush(timeout=5)
        assert kc.device_item(SN) is not None


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------


def _export_doc(devices=None, **overrides):
    doc = {
        "format": "plaud-v2-pairing",
        "version": 1,
        "exported_at": "2026-11-02T10:00:00+00:00",
        "devices": devices
        if devices is not None
        else [{"serial": SN, "name": "Desk NotePin", "sn_signature": SIG, "bind_token": TOKEN}],
    }
    doc.update(overrides)
    return doc


class TestPairingFileFormat:
    def test_build_and_parse_round_trip(self):
        devices = pairing_store.paired_devices_with_credentials(_paired_config())
        doc = pairing_file.build_export(devices)
        assert doc["format"] == "plaud-v2-pairing" and doc["version"] == 1 and doc["exported_at"]
        assert pairing_file.parse_export(json.loads(json.dumps(doc))) == devices

    def test_name_is_optional(self):
        devs = pairing_file.parse_export(_export_doc([{"serial": SN, "sn_signature": SIG, "bind_token": TOKEN}]))
        assert devs[0]["name"] == pairing_store.DEFAULT_DEVICE_NAME

    @pytest.mark.parametrize(
        "doc,fragment",
        [
            ([], "isn't a Plaud V2 pairing export"),
            (_export_doc(format="plaud-pairing"), "isn't a Plaud V2 pairing export"),
            (_export_doc(version=2), "version 2"),
            (_export_doc(version="1"), "version"),
            (_export_doc(version=True), "version"),
            (_export_doc(extra=1), "unexpected fields"),
            (_export_doc(devices=[]), "no devices"),
            (_export_doc(exported_at=5), "exported_at"),
            (_export_doc([{"serial": SN, "sn_signature": SIG}]), "missing: bind_token"),
            (_export_doc([{"serial": SN, "sn_signature": SIG, "bind_token": TOKEN, "x": 1}]), "unexpected fields"),
            (_export_doc([{"serial": SN.lower(), "sn_signature": SIG, "bind_token": TOKEN}]), "invalid serial"),
            (_export_doc([{"serial": "8820A", "sn_signature": SIG, "bind_token": TOKEN}]), "invalid serial"),
            (_export_doc([{"serial": SN, "sn_signature": "%%%", "bind_token": TOKEN}]), "invalid SN signature"),
            (_export_doc([{"serial": SN, "sn_signature": SIG, "bind_token": "t" * 65}]), "invalid bind token"),
            (_export_doc([{"serial": SN, "sn_signature": SIG, "bind_token": "tökén"}]), "invalid bind token"),
            (_export_doc([{"serial": SN, "sn_signature": SIG, "bind_token": TOKEN, "name": ""}]), "invalid name"),
            (_export_doc([{"serial": SN, "sn_signature": SIG, "bind_token": TOKEN}] * 2), "twice"),
            (_export_doc(["nope"]), "isn't an object"),
        ],
    )
    def test_strict_validation(self, doc, fragment):
        with pytest.raises(pairing_file.PairingFileError) as e:
            pairing_file.parse_export(doc)
        assert fragment in str(e.value)
        for secret in SECRETS:
            assert secret not in str(e.value)

    def test_too_many_devices(self):
        devs = [{"serial": f"8820A000{i:08X}", "sn_signature": SIG, "bind_token": TOKEN} for i in range(33)]
        with pytest.raises(pairing_file.PairingFileError, match="more than 32"):
            pairing_file.parse_export(_export_doc(devs))

    def test_bytes_that_are_not_json_or_too_big(self):
        with pytest.raises(pairing_file.PairingFileError, match="not JSON"):
            pairing_file.parse_export_bytes(b"\xff\xfe")
        with pytest.raises(pairing_file.PairingFileError, match="too big"):
            pairing_file.parse_export_bytes(b" " * (pairing_file.MAX_FILE_BYTES + 1))


class TestPairingRoutes:
    @pytest.fixture
    def client(self, flask_client, monkeypatch):
        fake = FakeSecurity()
        monkeypatch.setattr(pairing_store, "available", lambda: True)
        monkeypatch.setattr(pairing_store.subprocess, "run", fake)
        flask_client.fake_kc = fake
        yield flask_client
        fake.assert_no_secret_in_argv()

    @pytest.fixture
    def out_dir(self):
        # Outside the test data dir (exports there are refused) and the repo.
        import tempfile

        d = Path(tempfile.mkdtemp(prefix="plaud-pairing-export-")).resolve()
        yield d
        import shutil

        shutil.rmtree(d, ignore_errors=True)

    def test_status_reports_exportable_devices_without_credentials(self, client):
        r = client.get("/device/pairing")
        assert r.get_json() == {"devices": [], "exportable": False, "keychain_available": True}
        save_config(_paired_config())
        body = client.get("/device/pairing").get_json()
        assert body["exportable"] is True
        assert body["devices"] == [{"serial_tail": "5678", "name": "Desk NotePin", "in_keychain": False}]
        assert SIG not in json.dumps(body) and TOKEN not in json.dumps(body)
        client.fake_kc.put_device(SN)
        assert client.get("/device/pairing").get_json()["devices"][0]["in_keychain"] is True

    def test_export_import_round_trip(self, client, out_dir):
        save_config(_paired_config([(SN, SIG, TOKEN), (SN2, SIG2, TOKEN2)]))
        target = out_dir / "pairing.json"
        r = client.post("/device/pairing/export", json={"path": str(target)})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["exported"] == 2 and SIG not in json.dumps(body) and TOKEN not in json.dumps(body)
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
        doc = json.loads(target.read_text())
        assert doc["format"] == "plaud-v2-pairing" and doc["version"] == 1
        assert {d["serial"] for d in doc["devices"]} == {SN, SN2}
        assert not list(out_dir.glob(".plaud-pairing-*"))  # no temp file left

        # A cleaner wipes the data folder and the Keychain backup is gone too.
        save_config({})
        client.fake_kc.items.clear()
        r = client.post("/device/pairing/import", json={"path": str(target)})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert sorted(d["serial_tail"] for d in body["imported"]) == ["5678", "EF01"]
        assert body["keychain_saved"] == 2
        assert SIG not in json.dumps(body) and TOKEN not in json.dumps(body)
        cfg = load_config()
        assert cfg["device_sn_signatures"] == {SN: SIG, SN2: SIG2}
        assert cfg["device_bind_tokens"] == {SN: TOKEN, SN2: TOKEN2}
        assert cfg["paired_devices"][SN] == {"name": "Desk NotePin"}
        assert client.fake_kc.device_item(SN2)["bind_token"] == TOKEN2

    def test_import_overwrites_old_credentials_but_keeps_paired_entry(self, client, out_dir):
        save_config(_paired_config([(SN, SIG2, TOKEN2)]))
        target = out_dir / "p.json"
        target.write_text(json.dumps(_export_doc()))
        assert client.post("/device/pairing/import", json={"path": str(target)}).status_code == 200
        cfg = load_config()
        assert cfg["device_bind_tokens"][SN] == TOKEN
        assert cfg["paired_devices"][SN]["last_address"] == "ADDR"

    def test_import_with_locked_keychain_still_saves_config(self, client, out_dir):
        client.fake_kc.fail_rc = 36
        target = out_dir / "p.json"
        target.write_text(json.dumps(_export_doc()))
        r = client.post("/device/pairing/import", json={"path": str(target)})
        assert r.status_code == 200
        assert r.get_json()["keychain_saved"] == 0
        assert load_config()["device_bind_tokens"][SN] == TOKEN

    def test_invalid_import_changes_nothing(self, client, out_dir):
        save_config(_paired_config())
        target = out_dir / "bad.json"
        target.write_text(json.dumps(_export_doc([{"serial": SN2, "sn_signature": SIG2, "bind_token": "bad token"}])))
        r = client.post("/device/pairing/import", json={"path": str(target)})
        assert r.status_code == 400
        assert "invalid bind token" in r.get_json()["error"]
        assert load_config() == _paired_config()

    def test_export_with_nothing_paired_is_refused(self, client, out_dir):
        r = client.post("/device/pairing/export", json={"path": str(out_dir / "p.json")})
        assert r.status_code == 400
        assert "no paired device" in r.get_json()["error"]
        assert not (out_dir / "p.json").exists()

    def test_export_overwrites_an_existing_file_with_0600(self, client, out_dir):
        save_config(_paired_config())
        target = out_dir / "p.json"
        target.write_text("old")
        os.chmod(target, 0o644)
        assert client.post("/device/pairing/export", json={"path": str(target)}).status_code == 200
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
        assert json.loads(target.read_text())["devices"][0]["serial"] == SN

    def test_export_rejects_bad_paths(self, client, out_dir):
        from app.utils.constants import DATA_DIR, PROJECT_ROOT

        save_config(_paired_config())
        (out_dir / "dir.json").mkdir()
        (out_dir / "Fake.app").mkdir()
        (out_dir / "real.json").write_text("{}")
        (out_dir / "link.json").symlink_to(out_dir / "real.json")
        bad = [
            None,
            "",
            "pairing.json",  # relative
            str(out_dir / "pairing.txt"),
            str(out_dir / "missing" / "pairing.json"),
            str(out_dir / "dir.json"),
            str(out_dir / "link.json"),
            str(out_dir / "Fake.app" / "pairing.json"),
            str(DATA_DIR / "pairing.json"),
            str(PROJECT_ROOT / "pairing.json"),
            str(PROJECT_ROOT / "app" / "pairing.json"),
            str(out_dir / "a\x00.json"),
        ]
        for path in bad:
            r = client.post("/device/pairing/export", json={"path": path})
            assert r.status_code == 400, path
            assert r.get_json()["error"]
        assert (out_dir / "real.json").read_text() == "{}"
        assert not (PROJECT_ROOT / "pairing.json").exists()
        assert not (DATA_DIR / "pairing.json").exists()

    def test_import_rejects_bad_paths(self, client, out_dir):
        (out_dir / "folder.json").mkdir()
        big = out_dir / "big.json"
        big.write_bytes(b" " * (pairing_file.MAX_FILE_BYTES + 1))
        for path in [None, "", "p.json", str(out_dir / "missing.json"), str(out_dir / "folder.json"), str(big)]:
            r = client.post("/device/pairing/import", json={"path": path})
            assert r.status_code == 400, path
        r = client.post("/device/pairing/import", data="not json", content_type="application/json")
        assert r.status_code == 400

    def test_routes_need_the_api_token(self, client, out_dir):
        r = client.post(
            "/device/pairing/export", json={"path": str(out_dir / "p.json")}, headers={"X-Plaud-Token": "wrong"}
        )
        assert r.status_code in (401, 403)
