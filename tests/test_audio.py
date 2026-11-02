"""Tests for audio processing utilities."""


def test_wrap_opus_ogg_roundtrip(sample_ogg):
    """OGG wrapping produces valid OGG container."""
    ogg_data, raw_frames = sample_ogg
    assert ogg_data[:4] == b"OggS"
    assert len(ogg_data) > len(raw_frames)  # OGG adds headers


def test_unwrap_ogg_roundtrip(sample_ogg, tmp_path):
    """unwrap(wrap(frames)) returns the original frames."""
    from app.utils.audio import unwrap_ogg_to_raw_opus

    ogg_data, raw_frames = sample_ogg
    ogg_path = tmp_path / "test.ogg"
    ogg_path.write_bytes(ogg_data)
    unwrapped = unwrap_ogg_to_raw_opus(str(ogg_path))
    assert unwrapped == raw_frames


def test_unwrap_already_raw(tmp_path):
    """unwrap() on raw data returns it unchanged."""
    from app.utils.audio import unwrap_ogg_to_raw_opus

    raw = bytes([0xB8]) * 80 * 5  # Fake raw Opus frames
    raw_path = tmp_path / "test.raw"
    raw_path.write_bytes(raw)
    result = unwrap_ogg_to_raw_opus(str(raw_path))
    assert result == raw


def test_ogg_crc_consistency():
    """CRC function produces consistent results."""
    from app.utils.audio import _ogg_crc

    data = b"Hello, OGG!"
    crc1 = _ogg_crc(data)
    crc2 = _ogg_crc(data)
    assert crc1 == crc2
    assert isinstance(crc1, int)


def test_decrypt_ble_file_bad_magic():
    """decrypt_ble_file rejects files without PLAUD.AI magic."""
    import pytest

    from app.utils.audio import BleDecryptError, decrypt_ble_file

    with pytest.raises(BleDecryptError):
        decrypt_ble_file(b"NOT_PLAUD" + b"\x00" * 0x300, None)


def test_decrypt_ble_file_too_small():
    """decrypt_ble_file rejects files smaller than 0x300."""
    import pytest

    from app.utils.audio import BleDecryptError, decrypt_ble_file

    with pytest.raises(BleDecryptError):
        decrypt_ble_file(b"PLAUD.AI" + b"\x00" * 10, None)


def test_wrap_ogg_empty():
    """wrap_opus_ogg with empty data produces minimal OGG."""
    from app.utils.audio import wrap_opus_ogg

    result = wrap_opus_ogg(b"")
    assert result[:4] == b"OggS"


def test_wrap_ogg_single_frame():
    """wrap_opus_ogg with exactly one 80-byte frame."""
    from app.utils.audio import wrap_opus_ogg

    frame = bytes([0xB8]) + bytes(79)
    result = wrap_opus_ogg(frame)
    assert result[:4] == b"OggS"
    assert len(result) > 80
