# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for RingBufferCaptureSink (P1.F.2)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from protoskipper.core.capture import RingBufferCaptureSink


def _ts() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_default_max_frames() -> None:
    sink = RingBufferCaptureSink()
    assert sink.frame_count == 0


def test_invalid_max_frames_raises() -> None:
    with pytest.raises(ValueError, match="max_frames"):
        RingBufferCaptureSink(max_frames=0)


# ---------------------------------------------------------------------------
# write_frame / frame_count
# ---------------------------------------------------------------------------


def test_write_frame_increments_count() -> None:
    sink = RingBufferCaptureSink()
    sink.write_frame(_ts(), "tx", b"\x01\x02")
    assert sink.frame_count == 1
    sink.write_frame(_ts(), "rx", b"\x03")
    assert sink.frame_count == 2


def test_ring_evicts_oldest_when_full() -> None:
    sink = RingBufferCaptureSink(max_frames=3)
    for i in range(5):
        sink.write_frame(_ts(), "tx", bytes([i]))
    assert sink.frame_count == 3
    # The buffer should contain the last 3 payloads (2, 3, 4).
    frames = sink._buf
    payloads = [f.payload for f in frames]
    assert payloads == [b"\x02", b"\x03", b"\x04"]


# ---------------------------------------------------------------------------
# flush_to / read_frames round-trip
# ---------------------------------------------------------------------------


def test_flush_to_returns_frame_count(tmp_path: Path) -> None:
    sink = RingBufferCaptureSink()
    sink.write_frame(_ts(), "tx", b"hello")
    sink.write_frame(_ts(), "rx", b"world")
    n = sink.flush_to(tmp_path / "capture.bin")
    assert n == 2


def test_flush_to_creates_readable_file(tmp_path: Path) -> None:
    sink = RingBufferCaptureSink()
    ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    payload = b"\xde\xad\xbe\xef"
    sink.write_frame(ts, "tx", payload)

    out = tmp_path / "cap.bin"
    sink.flush_to(out)

    frames = sink.read_frames(out)
    assert len(frames) == 1
    assert frames[0].direction == "tx"
    assert frames[0].payload == payload


def test_flush_roundtrip_directions(tmp_path: Path) -> None:
    sink = RingBufferCaptureSink()
    sink.write_frame(_ts(), "tx", b"\x01")
    sink.write_frame(_ts(), "rx", b"\x02")
    out = tmp_path / "cap.bin"
    sink.flush_to(out)
    frames = sink.read_frames(out)
    assert frames[0].direction == "tx"
    assert frames[1].direction == "rx"


def test_flush_does_not_clear_buffer(tmp_path: Path) -> None:
    sink = RingBufferCaptureSink()
    sink.write_frame(_ts(), "tx", b"data")
    sink.flush_to(tmp_path / "cap.bin")
    assert sink.frame_count == 1


def test_read_frames_bad_magic(tmp_path: Path) -> None:
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"BADHD\x01")
    with pytest.raises(ValueError, match="Not a ProtoSkipper"):
        sink = RingBufferCaptureSink()
        sink.read_frames(bad)


def test_flush_to_overwrites_existing_file(tmp_path: Path) -> None:
    sink = RingBufferCaptureSink()
    out = tmp_path / "cap.bin"
    sink.write_frame(_ts(), "tx", b"first")
    sink.flush_to(out)
    sink.write_frame(_ts(), "rx", b"second")
    sink.flush_to(out)
    # After second flush the file should contain both frames.
    frames = sink.read_frames(out)
    assert len(frames) == 2


def test_empty_flush(tmp_path: Path) -> None:
    sink = RingBufferCaptureSink()
    out = tmp_path / "empty.bin"
    n = sink.flush_to(out)
    assert n == 0
    frames = sink.read_frames(out)
    assert frames == []
