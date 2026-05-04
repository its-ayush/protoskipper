# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the pcapng writer/reader (P2.A.1)."""

from __future__ import annotations

import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from protoskipper.core.capture import CapturedFrame, RingBufferCaptureSink
from protoskipper.core.capture.pcapng import (
    _BT_CUSTOM,
    _BT_IDB,
    _BT_SHB,
    _BYTE_ORDER_MAGIC,
    _PEN,
    read_pcapng,
    write_pcapng,
)


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _frame(direction: str = "tx", payload: bytes = b"\x01\x02") -> CapturedFrame:
    return CapturedFrame(timestamp=_ts(), direction=direction, payload=payload)


# ---------------------------------------------------------------------------
# Structural compliance
# ---------------------------------------------------------------------------


def test_file_starts_with_shb(tmp_path: Path) -> None:
    out = tmp_path / "cap.pcapng"
    write_pcapng([], out)
    data = out.read_bytes()
    block_type = struct.unpack_from("<I", data, 0)[0]
    assert block_type == _BT_SHB


def test_shb_contains_byte_order_magic(tmp_path: Path) -> None:
    out = tmp_path / "cap.pcapng"
    write_pcapng([], out)
    data = out.read_bytes()
    bom = struct.unpack_from("<I", data, 8)[0]
    assert bom == _BYTE_ORDER_MAGIC


def test_idb_follows_shb(tmp_path: Path) -> None:
    out = tmp_path / "cap.pcapng"
    write_pcapng([], out)
    data = out.read_bytes()
    # Skip SHB: type(4) + length(4) + body + length(4)
    shb_len = struct.unpack_from("<I", data, 4)[0]
    idb_type = struct.unpack_from("<I", data, shb_len)[0]
    assert idb_type == _BT_IDB


def test_block_lengths_are_multiples_of_4(tmp_path: Path) -> None:
    """Every block's total length must be a multiple of 4 (pcapng spec §3.1)."""
    out = tmp_path / "cap.pcapng"
    write_pcapng([_frame("tx", b"\xde\xad\xbe")], out)
    data = out.read_bytes()
    pos = 0
    while pos < len(data):
        if pos + 8 > len(data):
            break
        block_len = struct.unpack_from("<I", data, pos + 4)[0]
        assert block_len % 4 == 0, f"Block at {pos} has length {block_len} (not multiple of 4)"
        pos += block_len


def test_trailing_length_matches_header(tmp_path: Path) -> None:
    """Each block must have the same length value at start and end."""
    out = tmp_path / "cap.pcapng"
    write_pcapng([_frame()], out)
    data = out.read_bytes()
    pos = 0
    while pos < len(data):
        if pos + 8 > len(data):
            break
        block_len = struct.unpack_from("<I", data, pos + 4)[0]
        tail_len = struct.unpack_from("<I", data, pos + block_len - 4)[0]
        assert block_len == tail_len, f"Mismatch at offset {pos}"
        pos += block_len


def test_custom_block_has_correct_pen(tmp_path: Path) -> None:
    out = tmp_path / "cap.pcapng"
    write_pcapng([_frame("tx", b"hello")], out, protocol_id="modbus.tcp")
    data = out.read_bytes()

    # Find the custom block (skip SHB + IDB)
    pos = 0
    found = False
    while pos < len(data):
        if pos + 8 > len(data):
            break
        block_type, block_len = struct.unpack_from("<II", data, pos)
        if block_type == _BT_CUSTOM:
            pen = struct.unpack_from("<I", data, pos + 8)[0]
            assert pen == _PEN
            found = True
        pos += block_len
    assert found, "No custom block found in file"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_roundtrip_empty_file(tmp_path: Path) -> None:
    out = tmp_path / "empty.pcapng"
    n = write_pcapng([], out)
    assert n == 0
    frames = read_pcapng(out)
    assert frames == []


def test_roundtrip_single_frame(tmp_path: Path) -> None:
    ts = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    frame = CapturedFrame(timestamp=ts, direction="tx", payload=b"\xde\xad\xbe\xef")
    out = tmp_path / "single.pcapng"
    write_pcapng([frame], out, protocol_id="modbus.tcp")
    result = read_pcapng(out)
    assert len(result) == 1
    assert result[0].direction == "tx"
    assert result[0].payload == b"\xde\xad\xbe\xef"


def test_roundtrip_preserves_direction(tmp_path: Path) -> None:
    frames = [_frame("tx", b"\x01"), _frame("rx", b"\x02"), _frame("tx", b"\x03")]
    out = tmp_path / "multi.pcapng"
    write_pcapng(frames, out)
    result = read_pcapng(out)
    assert [f.direction for f in result] == ["tx", "rx", "tx"]


def test_roundtrip_preserves_payload(tmp_path: Path) -> None:
    payload = bytes(range(256))
    out = tmp_path / "big.pcapng"
    write_pcapng([_frame("rx", payload)], out)
    result = read_pcapng(out)
    assert result[0].payload == payload


def test_write_returns_frame_count(tmp_path: Path) -> None:
    out = tmp_path / "count.pcapng"
    n = write_pcapng([_frame(), _frame(), _frame()], out)
    assert n == 3


def test_atomic_write_does_not_leave_tmp_on_success(tmp_path: Path) -> None:
    out = tmp_path / "cap.pcapng"
    write_pcapng([_frame()], out)
    tmp = out.with_suffix(".pcapng.pcaptmp")
    assert not tmp.exists()


# ---------------------------------------------------------------------------
# Flush-to-pcapng integration via RingBufferCaptureSink
# ---------------------------------------------------------------------------


def test_ring_buffer_flush_to_pcapng(tmp_path: Path) -> None:
    """RingBufferCaptureSink.flush_to creates a binary file; write_pcapng creates pcapng."""
    sink = RingBufferCaptureSink(max_frames=100)
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    sink.write_frame(ts, "tx", b"\x01\x02\x03")
    sink.write_frame(ts, "rx", b"\x04\x05\x06")

    # Convert buffered frames to pcapng
    raw_path = tmp_path / "cap.bin"
    sink.flush_to(raw_path)
    frames = sink.read_frames(raw_path)  # read back from binary format

    pcapng_path = tmp_path / "cap.pcapng"
    write_pcapng(frames, pcapng_path, protocol_id="modbus.tcp")
    result = read_pcapng(pcapng_path)

    assert len(result) == 2
    assert result[0].payload == b"\x01\x02\x03"
    assert result[1].payload == b"\x04\x05\x06"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_read_invalid_file_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pcapng"
    bad.write_bytes(b"NOTAPCAPNG_FILE_AT_ALL")
    # read_pcapng should not crash — unknown block types are skipped.
    # However if the SHB is missing or has wrong magic, it raises.
    with pytest.raises((ValueError, struct.error)):
        read_pcapng(bad)


def test_write_overwrites_existing_file(tmp_path: Path) -> None:
    out = tmp_path / "cap.pcapng"
    write_pcapng([_frame("tx", b"first")], out)
    write_pcapng([_frame("rx", b"second"), _frame("tx", b"third")], out)
    result = read_pcapng(out)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# P2.A.1 Acceptance criteria tests (explicit value assertions)
# ---------------------------------------------------------------------------


def test_custom_block_type_is_0x40000001(tmp_path: Path) -> None:
    """P2.A.1 AC: Custom Block type must be exactly 0x40000001.

    We hardcode the expected value so a constant rename cannot silently
    invalidate the requirement.
    """
    out = tmp_path / "cap.pcapng"
    write_pcapng([_frame("tx", b"payload")], out)
    data = out.read_bytes()
    pos = 0
    found = False
    while pos < len(data) - 8:
        block_type, block_len = struct.unpack_from("<II", data, pos)
        if block_type == 0x40000001:
            found = True
            break
        if block_len == 0:
            break
        pos += block_len
    assert found, "No block with type 0x40000001 found — P2.A.1 AC not met"


def test_custom_block_carries_protocol_id(tmp_path: Path) -> None:
    """P2.A.1 AC: Custom Block body must embed the protocol_id string."""
    protocol_id = "modbus.tcp"
    out = tmp_path / "cap.pcapng"
    write_pcapng([_frame("tx", b"\xff")], out, protocol_id=protocol_id)
    raw = out.read_bytes()
    # protocol_id is stored as UTF-8 inside the custom block body
    assert protocol_id.encode() in raw, "protocol_id not found in raw file bytes"


def test_custom_block_carries_payload(tmp_path: Path) -> None:
    """P2.A.1 AC: Custom Block body must embed the raw frame payload."""
    payload = b"\xde\xad\xbe\xef\xca\xfe"
    out = tmp_path / "cap.pcapng"
    write_pcapng([_frame("rx", payload)], out)
    raw = out.read_bytes()
    assert payload in raw, "raw payload not found in pcapng file bytes"
