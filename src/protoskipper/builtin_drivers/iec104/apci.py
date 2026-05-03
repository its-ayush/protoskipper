# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""APCI (Application Protocol Control Information) codec for IEC 60870-5-104.

An APCI is the 6-byte fixed header that introduces every APDU on the wire::

    +------+--------+---------+---------+---------+---------+
    | 0x68 | length | ctrl[0] | ctrl[1] | ctrl[2] | ctrl[3] |
    +------+--------+---------+---------+---------+---------+

``length`` is the count of bytes following it (i.e. ctrl + ASDU); valid
range is 4..253. The control field encodes one of three frame formats:

* **I-format** (information transfer) - ctrl[0] LSB = 0. Carries an ASDU.
  N(S) is encoded in ctrl[0..1] (15 bits, ctrl[0] LSB always 0); N(R) is
  encoded in ctrl[2..3] (15 bits, ctrl[2] LSB always 0).

* **S-format** (supervisory) - ctrl[0] = 0x01, ctrl[1] = 0x00. Carries
  only N(R) in ctrl[2..3]. No ASDU. Used to acknowledge received I-frames.

* **U-format** (unnumbered control) - ctrl[0] bit0=1 and bit1=1. The
  function (STARTDT/STOPDT/TESTFR x act|con) is encoded in the high bits
  of ctrl[0]; ctrl[1..3] are zero.

Sequence numbers wrap at 32768 (15-bit field). The k/w window state is
not tracked here - that lives in :mod:`master`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from protoskipper.core.errors import EncodingError

START_BYTE = 0x68
APCI_LEN = 6  # full APCI: start + length + 4 control bytes
MIN_APDU_LEN = 4  # length field minimum (S/U frames carry only ctrl)
MAX_APDU_LEN = 253  # length field maximum per IEC 60870-5-104

SEQ_MAX = 0x8000  # sequence numbers are 15-bit, wrap at 32768
SEQ_MASK = 0x7FFF


class FrameFormat(Enum):
    I = "I"  # noqa: E741 - matches spec terminology
    S = "S"
    U = "U"


class UType(Enum):
    """U-format function encodings (high bits of ctrl[0])."""

    STARTDT_ACT = 0x07  # 0000 0111
    STARTDT_CON = 0x0B  # 0000 1011
    STOPDT_ACT = 0x13  # 0001 0011
    STOPDT_CON = 0x23  # 0010 0011
    TESTFR_ACT = 0x43  # 0100 0011
    TESTFR_CON = 0x83  # 1000 0011


@dataclass(frozen=True)
class Apdu:
    """One parsed Application Protocol Data Unit.

    Exactly one of ``asdu`` (I-format) or ``utype`` (U-format) is set; for
    S-format both are ``None`` and only ``recv_seq`` is meaningful.
    """

    fmt: FrameFormat
    send_seq: int | None = None  # N(S), only for I-frames
    recv_seq: int | None = None  # N(R), for I and S frames
    utype: UType | None = None  # only for U-frames
    asdu: bytes = b""  # ASDU payload, only for I-frames

    def encode(self) -> bytes:
        return build_apdu(self)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _check_seq(name: str, value: int) -> None:
    if not 0 <= value < SEQ_MAX:
        raise EncodingError(f"{name} out of range: {value} (must be 0..{SEQ_MAX - 1})")


def build_i_frame(send_seq: int, recv_seq: int, asdu: bytes) -> bytes:
    """Build an I-format APDU framing ``asdu``."""
    _check_seq("N(S)", send_seq)
    _check_seq("N(R)", recv_seq)
    if not asdu:
        raise EncodingError("I-frame requires a non-empty ASDU")
    if len(asdu) > MAX_APDU_LEN - 4:
        raise EncodingError(
            f"ASDU too long: {len(asdu)} bytes (max {MAX_APDU_LEN - 4} for one APDU)"
        )
    ns_lo = (send_seq & 0x7F) << 1
    ns_hi = (send_seq >> 7) & 0xFF
    nr_lo = (recv_seq & 0x7F) << 1
    nr_hi = (recv_seq >> 7) & 0xFF
    length = 4 + len(asdu)
    return bytes([START_BYTE, length, ns_lo, ns_hi, nr_lo, nr_hi]) + asdu


def build_s_frame(recv_seq: int) -> bytes:
    _check_seq("N(R)", recv_seq)
    nr_lo = (recv_seq & 0x7F) << 1
    nr_hi = (recv_seq >> 7) & 0xFF
    return bytes([START_BYTE, MIN_APDU_LEN, 0x01, 0x00, nr_lo, nr_hi])


def build_u_frame(utype: UType) -> bytes:
    return bytes([START_BYTE, MIN_APDU_LEN, utype.value, 0x00, 0x00, 0x00])


def build_apdu(apdu: Apdu) -> bytes:
    if apdu.fmt is FrameFormat.I:
        if apdu.send_seq is None or apdu.recv_seq is None:
            raise EncodingError("I-frame requires both N(S) and N(R)")
        return build_i_frame(apdu.send_seq, apdu.recv_seq, apdu.asdu)
    if apdu.fmt is FrameFormat.S:
        if apdu.recv_seq is None:
            raise EncodingError("S-frame requires N(R)")
        return build_s_frame(apdu.recv_seq)
    if apdu.fmt is FrameFormat.U:
        if apdu.utype is None:
            raise EncodingError("U-frame requires a UType")
        return build_u_frame(apdu.utype)
    raise EncodingError(f"Unknown frame format: {apdu.fmt!r}")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def parse_apdu(buf: bytes) -> Apdu:
    """Parse exactly one full APDU. Raises EncodingError on malformed input.

    The caller is responsible for framing: use :func:`peek_apdu_length` on
    the receive buffer, wait until at least that many bytes are available,
    then slice and pass them here.
    """
    if len(buf) < APCI_LEN:
        raise EncodingError(f"APDU too short: {len(buf)} bytes (need >= {APCI_LEN})")
    if buf[0] != START_BYTE:
        raise EncodingError(f"Bad start byte: 0x{buf[0]:02X} (expected 0x68)")
    length = buf[1]
    if not MIN_APDU_LEN <= length <= MAX_APDU_LEN:
        raise EncodingError(
            f"APDU length out of range: {length} (must be {MIN_APDU_LEN}..{MAX_APDU_LEN})"
        )
    total = length + 2  # +start +length
    if len(buf) < total:
        raise EncodingError(f"Truncated APDU: have {len(buf)} bytes, need {total}")

    c0, c1, c2, c3 = buf[2], buf[3], buf[4], buf[5]
    asdu = bytes(buf[6:total])

    # Format detection per IEC 60870-5-104 ed. 2 §5.1.
    if c0 & 0x01 == 0:
        # I-format.
        send_seq = ((c1 << 7) | (c0 >> 1)) & SEQ_MASK
        recv_seq = ((c3 << 7) | (c2 >> 1)) & SEQ_MASK
        if not asdu:
            raise EncodingError("I-frame must have a non-empty ASDU")
        return Apdu(
            fmt=FrameFormat.I,
            send_seq=send_seq,
            recv_seq=recv_seq,
            asdu=asdu,
        )
    if c0 & 0x03 == 0x01:
        # S-format.
        if c1 != 0x00:
            raise EncodingError(f"S-frame ctrl[1] must be 0, got 0x{c1:02X}")
        if asdu:
            raise EncodingError("S-frame must not carry ASDU bytes")
        recv_seq = ((c3 << 7) | (c2 >> 1)) & SEQ_MASK
        return Apdu(fmt=FrameFormat.S, recv_seq=recv_seq)
    if c0 & 0x03 == 0x03:
        # U-format.
        if c1 != 0x00 or c2 != 0x00 or c3 != 0x00:
            raise EncodingError(
                f"U-frame ctrl[1..3] must be zero, got 0x{c1:02X} 0x{c2:02X} 0x{c3:02X}"
            )
        if asdu:
            raise EncodingError("U-frame must not carry ASDU bytes")
        try:
            utype = UType(c0)
        except ValueError as exc:
            raise EncodingError(f"Unknown U-format function code: 0x{c0:02X}") from exc
        return Apdu(fmt=FrameFormat.U, utype=utype)
    raise EncodingError(f"Unrecognisable APCI control byte: 0x{c0:02X}")


def peek_apdu_length(buf: bytes) -> int | None:
    """Return total APDU byte count if the header is fully present, else ``None``.

    Used by the receive loop to know how many bytes to wait for.
    """
    if len(buf) < 2:
        return None
    if buf[0] != START_BYTE:
        raise EncodingError(f"Bad start byte: 0x{buf[0]:02X} (expected 0x68)")
    length = buf[1]
    if not MIN_APDU_LEN <= length <= MAX_APDU_LEN:
        raise EncodingError(f"APDU length out of range: {length}")
    return length + 2


def seq_inc(seq: int) -> int:
    """Increment a 15-bit sequence number with wrap-around."""
    return (seq + 1) & SEQ_MASK
