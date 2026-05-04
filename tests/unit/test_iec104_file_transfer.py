# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for P4.B.8 / P4.C.5 — IEC 104 file transfer.

Tests verify:
  * The file_transfer.py codec (builders + parsers round-trip).
  * Master browse_files() against Iec104SlaveServer.
  * Master download_file() for a small and a medium-size file.
  * Slave add_file() / directory service (P4.C.5).
"""

from __future__ import annotations

import pytest

from protoskipper.builtin_drivers.iec104.asdu import TypeID
from protoskipper.builtin_drivers.iec104.file_transfer import (
    MAX_SEGMENT_PAYLOAD,
    DirectoryEntry,
    build_ack_file,
    build_ack_section,
    build_call_directory,
    build_call_section,
    build_directory_entry,
    build_file_ready,
    build_last_section,
    build_section_ready,
    build_segment,
    build_select_file,
    parse_ack,
    parse_directory_entries,
    parse_file_ready,
    parse_last_section,
    parse_section_ready,
    parse_segment,
)
from protoskipper.builtin_drivers.iec104.master import Iec104MasterSession, MasterConfig
from protoskipper.builtin_drivers.iec104.slave import Iec104SlaveServer, SlaveConfig

# ---------------------------------------------------------------------------
# Codec round-trip tests (no networking)
# ---------------------------------------------------------------------------


def test_build_call_directory_type() -> None:
    a = build_call_directory(ca=1)
    assert a.type_id is TypeID.F_SC_NA_1
    assert a.objects[0].ioa == 0
    assert a.objects[0].value == 0


def test_build_select_file() -> None:
    a = build_select_file(ca=1, name_ioa=5001)
    assert a.type_id is TypeID.F_SC_NA_1
    assert a.objects[0].ioa == 5001
    assert a.objects[0].value == 1


def test_build_call_section() -> None:
    a = build_call_section(ca=1, name_ioa=5001, section=2)
    assert a.type_id is TypeID.F_SC_NA_1
    assert a.objects[0].value == 3  # call_type=3
    assert a.objects[0].quality == 2  # section=2


def test_build_ack_file_and_section() -> None:
    ack_file = build_ack_file(ca=1, name_ioa=5001)
    ack_sec = build_ack_section(ca=1, name_ioa=5001)
    assert ack_file.type_id is TypeID.F_AF_NA_1
    assert ack_sec.type_id is TypeID.F_AF_NA_1
    assert parse_ack(ack_file) == (5001, 0)
    assert parse_ack(ack_sec) == (5001, 1)


def test_build_file_ready_roundtrip() -> None:
    a = build_file_ready(ca=1, name_ioa=5001, length=1024)
    ioa, length = parse_file_ready(a)
    assert ioa == 5001
    assert length == 1024


def test_build_section_ready_roundtrip() -> None:
    a = build_section_ready(ca=1, name_ioa=5001, section=1, length=512)
    ioa, section, length = parse_section_ready(a)
    assert ioa == 5001
    assert section == 1
    assert length == 512


def test_build_segment_roundtrip() -> None:
    payload = b"hello world"
    a = build_segment(ca=1, name_ioa=5001, section=1, payload=payload)
    ioa, section, data = parse_segment(a)
    assert ioa == 5001
    assert section == 1
    assert data == payload


def test_build_segment_too_large_raises() -> None:
    with pytest.raises(ValueError, match="Segment payload"):
        build_segment(ca=1, name_ioa=5001, section=1, payload=b"x" * (MAX_SEGMENT_PAYLOAD + 1))


def test_build_last_section_roundtrip() -> None:
    a_sec = build_last_section(ca=1, name_ioa=5001, section=1, file_done=False)
    a_file = build_last_section(ca=1, name_ioa=5001, section=1, file_done=True)
    _, _, done_sec = parse_last_section(a_sec)
    _, _, done_file = parse_last_section(a_file)
    assert done_sec is False
    assert done_file is True


def test_build_directory_entry_roundtrip() -> None:
    entry = DirectoryEntry(ioa=5001, name="log.bin", length=8192)
    a = build_directory_entry(ca=1, entry=entry)
    assert a.type_id is TypeID.F_DR_TA_1
    parsed = parse_directory_entries(a)
    assert len(parsed) == 1
    assert parsed[0].ioa == 5001
    assert parsed[0].name == "log.bin"
    assert parsed[0].length == 8192


def test_max_segment_payload_constant() -> None:
    assert MAX_SEGMENT_PAYLOAD == 238  # IEC 104: 249 max ASDU - 6 hdr - 3 IOA - 2 elem hdr


# ---------------------------------------------------------------------------
# Integration: slave file service + master download
# ---------------------------------------------------------------------------


def _start_slave(port: int, files: dict[int, tuple[str, bytes]]) -> Iec104SlaveServer:
    """Start a slave with the given files registered, listening on *port*."""
    srv = Iec104SlaveServer(SlaveConfig(port=port, ca=1))
    for ioa, (name, data) in files.items():
        srv.add_file(ioa, name, data)
    srv.start()
    return srv


def _make_master(port: int) -> Iec104MasterSession:
    return Iec104MasterSession(
        MasterConfig(
            host="127.0.0.1",
            port=port,
            ca=1,
            t1=5.0,
            auto_reconnect=False,
        )
    )


@pytest.fixture()
def file_server():
    """Slave with two files: one tiny, one 1-segment boundary."""
    small = b"hello\n"
    exact = b"A" * MAX_SEGMENT_PAYLOAD  # exactly one segment
    srv = _start_slave(0, {5001: ("sml.txt", small), 5002: ("ex.bin", exact)})
    yield srv
    srv.stop()


def test_browse_files_returns_directory(file_server: Iec104SlaveServer) -> None:
    m = _make_master(file_server.port)
    m.connect()
    try:
        entries = m.browse_files(timeout=3.0)
        ioas = {e.ioa for e in entries}
        assert 5001 in ioas
        assert 5002 in ioas
        names = {e.name for e in entries}
        assert "sml.txt" in names
        assert "ex.bin" in names
    finally:
        m.close()


def test_browse_empty_directory(tmp_path: object) -> None:
    """Slave with no files returns an empty list."""
    srv = Iec104SlaveServer(SlaveConfig(port=0, ca=1))
    srv.start()
    try:
        m = _make_master(srv.port)
        m.connect()
        try:
            entries = m.browse_files(timeout=1.0)
            assert entries == []
        finally:
            m.close()
    finally:
        srv.stop()


def test_download_small_file(file_server: Iec104SlaveServer) -> None:
    small = b"hello\n"
    m = _make_master(file_server.port)
    m.connect()
    try:
        data = m.download_file(5001, timeout=5.0)
        assert data == small
    finally:
        m.close()


def test_download_exact_one_segment(file_server: Iec104SlaveServer) -> None:
    exact = b"A" * MAX_SEGMENT_PAYLOAD
    m = _make_master(file_server.port)
    m.connect()
    try:
        data = m.download_file(5002, timeout=5.0)
        assert data == exact
    finally:
        m.close()


def test_download_large_file() -> None:
    """Download a 1 MB file; must complete in < 30 s (AC from P4.B.8)."""
    one_mb = bytes(range(256)) * (1024 * 1024 // 256)
    srv = _start_slave(0, {9001: ("big.bin", one_mb)})  # 7 chars, OK
    try:
        m = _make_master(srv.port)
        m.connect()
        try:
            import time as _time

            t0 = _time.monotonic()
            data = m.download_file(9001, timeout=30.0)
            elapsed = _time.monotonic() - t0
            assert data == one_mb, f"Data mismatch: got {len(data)} bytes, expected {len(one_mb)}"
            assert elapsed < 30.0, f"Transfer took {elapsed:.1f}s, must be < 30s"
        finally:
            m.close()
    finally:
        srv.stop()
