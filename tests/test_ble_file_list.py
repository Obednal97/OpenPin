"""cmd 26 file list: start index, entry width, multi-notification replies (no hardware)."""

import asyncio
import struct

import pytest

import app.state.device as dev
from app.utils.ble_proto import FileListAssembler, file_list_entry_width
from tests.test_ble_sync_integrity import FakeDevice


def entry(sid, size, width=10):
    return struct.pack("<II", sid, size) + bytes(width - 8)


def page(total, start, entries, extra=b""):
    """One cmd-26 notification: [1,26,0] + 4 unknown + total + start index + entries."""
    return bytes([1, 26, 0]) + bytes(4) + struct.pack("<HH", total, start) + b"".join(entries) + extra


def sids(lst):
    return [r["session_id"] for r in lst.records]


def test_single_notification():
    lst = FileListAssembler()
    assert lst.add(page(2, 0, [entry(11, 1000), entry(12, 2000)]))
    assert lst.complete and sids(lst) == [11, 12]
    assert lst.records[0] == {"session_id": 11, "size": 1000, "duration_est": round((1000 - 512) / 80 * 0.02, 1)}


def test_empty_list_is_complete():
    lst = FileListAssembler()
    lst.add(page(0, 0, []))
    assert lst.complete and lst.records == []


def test_multi_notification_in_order():
    lst = FileListAssembler()
    lst.add(page(5, 0, [entry(1, 900), entry(2, 900)]))
    assert not lst.complete
    lst.add(page(5, 2, [entry(3, 900), entry(4, 900)]))
    lst.add(page(5, 4, [entry(5, 900)]))
    assert lst.complete and sids(lst) == [1, 2, 3, 4, 5]


def test_retransmitted_notification_is_skipped():
    lst = FileListAssembler()
    first = page(4, 0, [entry(1, 900), entry(2, 900)])
    lst.add(first)
    lst.add(first)  # retransmission: start index 0 again
    lst.add(page(4, 1, [entry(2, 900), entry(3, 900)]))  # overlaps: only entry 3 is new
    lst.add(page(4, 3, [entry(4, 900)]))
    assert lst.complete and sids(lst) == [1, 2, 3, 4]


def test_out_of_order_notification_waits_for_the_gap():
    lst = FileListAssembler()
    lst.add(page(4, 2, [entry(3, 900), entry(4, 900)]))
    assert lst.records == [] and not lst.complete
    lst.add(page(4, 0, [entry(1, 900), entry(2, 900)]))
    assert lst.complete and sids(lst) == [1, 2, 3, 4]


def test_missing_notification_leaves_the_list_incomplete():
    lst = FileListAssembler()
    lst.add(page(4, 0, [entry(1, 900)]))
    lst.add(page(4, 2, [entry(3, 900), entry(4, 900)]))  # index 1 never arrives
    assert not lst.complete and sids(lst) == [1]


@pytest.mark.parametrize("junk", [b"\x00", b"\x01\x02\x03", bytes(7)])
def test_odd_length_notification_is_dropped_not_misread(junk, caplog):
    lst = FileListAssembler()
    with caplog.at_level("WARNING"):
        assert not lst.add(page(2, 0, [entry(1, 900), entry(2, 900)], extra=junk))
    assert lst.records == [] and not lst.complete
    assert "not a multiple of the entry width" in caplog.text


def test_short_packet_is_dropped():
    lst = FileListAssembler()
    assert not lst.add(bytes([1, 26, 0, 0, 0]))
    assert lst.total is None


def test_port_version_width_used_only_when_ten_does_not_fit():
    # Port version 3 suggests 9-byte entries; 18 bytes fit 9 and not 10.
    lst = FileListAssembler(port_version=3)
    lst.add(page(2, 0, [entry(1, 900, width=9), entry(2, 900, width=9)]))
    assert lst.width == 9 and sids(lst) == [1, 2]
    # 90 bytes fit both: the confirmed 10-byte layout wins.
    lst = FileListAssembler(port_version=3)
    lst.add(page(9, 0, [entry(i, 900) for i in range(1, 10)]))
    assert lst.width == 10 and len(lst.records) == 9
    assert [file_list_entry_width(v) for v in (None, 1, 2, 6, 7, 9)] == [10, 8, 9, 9, 10, 10]


def test_invalid_entries_skipped_but_counted():
    lst = FileListAssembler()
    lst.add(page(4, 0, [entry(0, 900), entry(1, 0), entry(2, 900), entry(2, 900)]))
    # sid 0, size 0 and the duplicate sid are left out, but all 4 entries count.
    assert lst.complete and sids(lst) == [2]


def test_total_change_mid_list_is_rejected():
    lst = FileListAssembler()
    lst.add(page(3, 0, [entry(1, 900)]))
    assert not lst.add(page(5, 1, [entry(2, 900)]))
    assert sids(lst) == [1]


# -- list_device_files() through the real notify handler --------------------------


def run_list(notifications, port_version=None):
    device = FakeDevice({}, scripted={26: notifications})

    async def go():
        device.install()
        dev._device_state["port_version"] = port_version
        return await dev.list_device_files()

    try:
        return asyncio.run(go())
    finally:
        dev._reset_state()


def test_list_device_files_collects_several_notifications():
    pages = [page(3, 0, [entry(1, 900), entry(2, 900)]), page(3, 2, [entry(3, 900)])]
    assert [f["session_id"] for f in run_list(pages)] == [1, 2, 3]


def test_list_device_files_ignores_a_bad_notification(caplog, monkeypatch):
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        return await real_sleep(min(delay, 0.001), *a, **k)

    monkeypatch.setattr(dev.asyncio, "sleep", fast_sleep)  # the 3 s wait for the missing page
    pages = [page(2, 0, [entry(1, 900)]), page(2, 1, [entry(2, 900)], extra=b"\x00")]
    with caplog.at_level("WARNING"):
        files = run_list(pages)
    assert [f["session_id"] for f in files] == [1]
    assert "file list incomplete: 1 of 2" in caplog.text
