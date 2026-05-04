# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the IEC 60870-5-104 APCI codec."""

from __future__ import annotations

import pytest

from protoskipper.builtin_drivers.iec104.apci import (
    Apdu,
    FrameFormat,
    UType,
    build_i_frame,
    build_s_frame,
    build_u_frame,
    parse_apdu,
    peek_apdu_length,
    seq_inc,
)
from protoskipper.core.errors import EncodingError

# ---------------------------------------------------------------------------
# U-frames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utype, expected_byte",
    [
        (UType.STARTDT_ACT, 0x07),
        (UType.STARTDT_CON, 0x0B),
        (UType.STOPDT_ACT, 0x13),
        (UType.STOPDT_CON, 0x23),
        (UType.TESTFR_ACT, 0x43),
        (UType.TESTFR_CON, 0x83),
    ],
)
def test_u_frame_round_trip(utype: UType, expected_byte: int) -> None:
    frame = build_u_frame(utype)
    assert frame == bytes([0x68, 0x04, expected_byte, 0x00, 0x00, 0x00])
    parsed = parse_apdu(frame)
    assert parsed.fmt is FrameFormat.U
    assert parsed.utype is utype


def test_u_frame_must_have_zero_padding() -> None:
    bad = bytes([0x68, 0x04, 0x07, 0x00, 0x01, 0x00])
    with pytest.raises(EncodingError):
        parse_apdu(bad)


def test_u_frame_unknown_function() -> None:
    bad = bytes([0x68, 0x04, 0xFF, 0x00, 0x00, 0x00])
    with pytest.raises(EncodingError):
        parse_apdu(bad)


# ---------------------------------------------------------------------------
# S-frames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("nr", [0, 1, 127, 128, 1000, 32767])
def test_s_frame_round_trip(nr: int) -> None:
    frame = build_s_frame(nr)
    parsed = parse_apdu(frame)
    assert parsed.fmt is FrameFormat.S
    assert parsed.recv_seq == nr


def test_s_frame_canonical_bytes() -> None:
    # NR=0 -> bytes 4-5 = 00 00
    assert build_s_frame(0) == bytes([0x68, 0x04, 0x01, 0x00, 0x00, 0x00])
    # NR=1 -> bytes 4-5 = 02 00
    assert build_s_frame(1) == bytes([0x68, 0x04, 0x01, 0x00, 0x02, 0x00])
    # NR=128 -> 0x100 << 1 = 0x200 -> bytes 0x00 0x01
    assert build_s_frame(128) == bytes([0x68, 0x04, 0x01, 0x00, 0x00, 0x01])


def test_s_frame_seq_overflow() -> None:
    with pytest.raises(EncodingError):
        build_s_frame(0x8000)
    with pytest.raises(EncodingError):
        build_s_frame(-1)


# ---------------------------------------------------------------------------
# I-frames
# ---------------------------------------------------------------------------


def test_i_frame_round_trip_simple() -> None:
    asdu = b"\x01\x02\x03\x04"
    frame = build_i_frame(send_seq=5, recv_seq=7, asdu=asdu)
    parsed = parse_apdu(frame)
    assert parsed.fmt is FrameFormat.I
    assert parsed.send_seq == 5
    assert parsed.recv_seq == 7
    assert parsed.asdu == asdu


@pytest.mark.parametrize("ns", [0, 1, 63, 64, 127, 128, 16383, 16384, 32767])
@pytest.mark.parametrize("nr", [0, 1, 127, 32767])
def test_i_frame_seq_round_trip(ns: int, nr: int) -> None:
    asdu = bytes(range(8))
    frame = build_i_frame(send_seq=ns, recv_seq=nr, asdu=asdu)
    parsed = parse_apdu(frame)
    assert parsed.send_seq == ns
    assert parsed.recv_seq == nr


def test_i_frame_requires_asdu() -> None:
    with pytest.raises(EncodingError):
        build_i_frame(0, 0, b"")


def test_i_frame_max_length() -> None:
    body = b"\x00" * 249
    frame = build_i_frame(0, 0, body)
    parsed = parse_apdu(frame)
    assert parsed.asdu == body


def test_i_frame_too_long() -> None:
    with pytest.raises(EncodingError):
        build_i_frame(0, 0, b"\x00" * 250)


def test_i_frame_seq_overflow() -> None:
    with pytest.raises(EncodingError):
        build_i_frame(0x8000, 0, b"\x01")
    with pytest.raises(EncodingError):
        build_i_frame(0, 0x8000, b"\x01")


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------


def test_peek_apdu_length() -> None:
    assert peek_apdu_length(b"") is None
    assert peek_apdu_length(b"\x68") is None
    assert peek_apdu_length(b"\x68\x04") == 6
    assert peek_apdu_length(b"\x68\xfd") == 255
    with pytest.raises(EncodingError):
        peek_apdu_length(b"\x69\x04")
    with pytest.raises(EncodingError):
        peek_apdu_length(b"\x68\x03")
    with pytest.raises(EncodingError):
        peek_apdu_length(b"\x68\xfe")


def test_parse_apdu_truncated() -> None:
    frame = build_u_frame(UType.STARTDT_ACT)
    with pytest.raises(EncodingError):
        parse_apdu(frame[:-1])


def test_parse_apdu_bad_start() -> None:
    with pytest.raises(EncodingError):
        parse_apdu(b"\x69\x04\x07\x00\x00\x00")


def test_parse_apdu_length_out_of_range() -> None:
    with pytest.raises(EncodingError):
        parse_apdu(b"\x68\x03\x07\x00\x00\x00")


def test_seq_inc_wraps() -> None:
    assert seq_inc(0) == 1
    assert seq_inc(32767) == 0
    assert seq_inc(32766) == 32767


# ---------------------------------------------------------------------------
# Apdu.encode round trip
# ---------------------------------------------------------------------------


def test_apdu_encode_round_trip_i() -> None:
    a = Apdu(fmt=FrameFormat.I, send_seq=10, recv_seq=20, asdu=b"\xab\xcd")
    parsed = parse_apdu(a.encode())
    assert parsed.send_seq == 10
    assert parsed.recv_seq == 20
    assert parsed.asdu == b"\xab\xcd"


def test_apdu_encode_round_trip_s() -> None:
    a = Apdu(fmt=FrameFormat.S, recv_seq=42)
    parsed = parse_apdu(a.encode())
    assert parsed.fmt is FrameFormat.S
    assert parsed.recv_seq == 42


def test_apdu_encode_round_trip_u() -> None:
    a = Apdu(fmt=FrameFormat.U, utype=UType.TESTFR_ACT)
    parsed = parse_apdu(a.encode())
    assert parsed.fmt is FrameFormat.U
    assert parsed.utype is UType.TESTFR_ACT


def test_apdu_encode_invalid_combinations() -> None:
    with pytest.raises(EncodingError):
        Apdu(fmt=FrameFormat.I, send_seq=None, recv_seq=0, asdu=b"\x01").encode()
    with pytest.raises(EncodingError):
        Apdu(fmt=FrameFormat.S, recv_seq=None).encode()
    with pytest.raises(EncodingError):
        Apdu(fmt=FrameFormat.U, utype=None).encode()
