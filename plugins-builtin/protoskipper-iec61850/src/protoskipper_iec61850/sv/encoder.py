# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""IEC 61850-9-2 Sampled Values — raw APDU encoder — P8.D.2.

This module is the inverse of :mod:`protoskipper_iec61850.sv.decoder`.
It builds a complete Ethernet frame (or just the APDU bytes) from a list of
:class:`~protoskipper_iec61850.sv.decoder.SvAsdu` objects.

Public API
----------
* :func:`encode_frame` — builds a full Ethernet frame including the 14- (or
  18-) byte Ethernet / 802.1Q header and the 8-byte SV common header.
* :func:`encode_apdu` — builds only the APDU (APPID + Length + Reserved x2 +
  savPDU), useful for unit testing.

Wire format note
----------------
The encoder always emits the outer ``savPDU`` APPLICATION 0 tag (0x60) to
maximise interoperability with strict IEC 61850 parsers (libiec61850,
Wireshark, OMICRON). The ASDU inner SEQUENCE tag is 0x30 (UNIVERSAL 16
constructed).

BER encoding is *definite short/long form*: lengths < 128 use a single byte;
≥ 128 use the long form (2 bytes for lengths up to 65535, which covers any
normal SV frame that fits inside a single Ethernet frame).
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass

from protoskipper_iec61850.sv.decoder import (
    _TAG_CONFREV,
    _TAG_NO_ASDU,
    _TAG_REFRTM,
    _TAG_SAMPLE,
    _TAG_SEQ_ASDU,
    _TAG_SMPCNT,
    _TAG_SMPMOD,
    _TAG_SMPRATE,
    _TAG_SMPSYNCH,
    _TAG_SVID,
    ETHERTYPE_SV,
    SvAsdu,
    SvChannel,
)

# ---------------------------------------------------------------------------
# Public builder dataclass
# ---------------------------------------------------------------------------


@dataclass
class SvFrameSpec:
    """Parameters for a Sampled Values Ethernet frame.

    Attributes
    ----------
    asdus:
        One or more ASDUs to include (9-2LE = 1; 80-2 = up to 8).
    app_id:
        SV APPID (2-byte unsigned integer, typically 0x4000..0x7FFF for SV).
    dst_mac:
        Destination MAC (6 bytes).  Defaults to SV multicast
        ``01-0C-CD-04-00-00``.
    src_mac:
        Source MAC (6 bytes).  No default — caller must supply.
    vlan_id:
        802.1Q VLAN ID to use (0..4094), or ``None`` for untagged.
    vlan_priority:
        802.1Q PCP priority (0..7, default 4) — only relevant when
        *vlan_id* is not ``None``.
    """

    asdus: list[SvAsdu]
    app_id: int
    src_mac: bytes
    dst_mac: bytes = b"\x01\x0c\xcd\x04\x00\x00"
    vlan_id: int | None = None
    vlan_priority: int = 4


# ---------------------------------------------------------------------------
# Internal BER helpers
# ---------------------------------------------------------------------------


def _encode_ber_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    if length <= 0xFF:
        return bytes([0x81, length])
    if length <= 0xFFFF:
        return bytes([0x82, (length >> 8) & 0xFF, length & 0xFF])
    raise ValueError(f"BER length {length} exceeds 65535 (won't fit in one frame)")


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _encode_ber_length(len(value)) + value


def _encode_uint(value: int, min_bytes: int = 1) -> bytes:
    """Encode a non-negative integer in BER INTEGER form (big-endian, unsigned)."""
    if value < 0:
        raise ValueError("Use _encode_int for signed integers")
    n = max(min_bytes, (value.bit_length() + 7) // 8 or 1)
    return value.to_bytes(n, "big")


def _encode_int(value: int) -> bytes:
    """Encode a signed integer in BER INTEGER form (minimum bytes)."""
    if value == 0:
        return b"\x00"
    n = (value.bit_length() + 8) // 8  # +1 sign bit, rounded up
    return value.to_bytes(n, "big", signed=True)


def _encode_refrtm(ms: float) -> bytes:
    """Encode milliseconds-since-epoch as an 8-byte IEC 61850 UtcTime.

    Format: 4-byte unsigned seconds + 3-byte fractional (24-bit fixed-point
    where 2^24 = 1 second) + 1-byte time quality = 8 bytes total.
    """
    seconds = int(ms // 1000)
    frac_ms = ms - seconds * 1000.0
    frac_raw = int((frac_ms / 1000.0) * 0x1000000) & 0xFFFFFF
    tq = 0x0A  # time quality: 10-bit accuracy (advisory)
    return struct.pack(">I", seconds) + frac_raw.to_bytes(3, "big") + bytes([tq])


# ---------------------------------------------------------------------------
# ASDU / savPDU encoding
# ---------------------------------------------------------------------------


def _encode_sequence_of_data(channels: Sequence[SvChannel]) -> bytes:
    """Pack channel list into a raw ``seqOfData`` octet string."""
    buf = bytearray()
    for ch in channels:
        buf += struct.pack(">i", ch.value_raw)  # signed 32-bit
        buf += struct.pack(">I", ch.quality_raw)  # unsigned 32-bit
    return bytes(buf)


def _encode_asdu(asdu: SvAsdu) -> bytes:
    """Encode a single :class:`SvAsdu` to BER bytes (no outer SEQUENCE tag)."""
    fields = bytearray()

    # svID [0]
    sv_id_bytes = asdu.sv_id.encode("ascii")
    fields += _tlv(_TAG_SVID, sv_id_bytes)

    # smpCnt [2]
    fields += _tlv(_TAG_SMPCNT, _encode_uint(asdu.smp_cnt, 2))

    # confRev [3]
    fields += _tlv(_TAG_CONFREV, _encode_uint(asdu.conf_rev, 4))

    # refrTm [4] (optional)
    if asdu.refr_tm_ms is not None:
        fields += _tlv(_TAG_REFRTM, _encode_refrtm(asdu.refr_tm_ms))

    # smpSynch [5]
    fields += _tlv(_TAG_SMPSYNCH, _encode_uint(asdu.smp_synch, 1))

    # smpRate [6] (optional)
    if asdu.smp_rate is not None:
        fields += _tlv(_TAG_SMPRATE, _encode_uint(asdu.smp_rate, 2))

    # sample [7]
    sample_bytes = _encode_sequence_of_data(asdu.channels)
    fields += _tlv(_TAG_SAMPLE, sample_bytes)

    # smpMod [8] (optional)
    if asdu.smp_mod is not None:
        fields += _tlv(_TAG_SMPMOD, _encode_uint(asdu.smp_mod, 1))

    # Wrap in SEQUENCE (tag 0x30)
    return _tlv(0x30, bytes(fields))


def _encode_save_pdu(asdus: list[SvAsdu]) -> bytes:
    """Build the complete ``savPDU`` BER structure (with outer 0x60 tag)."""
    inner = bytearray()

    # noASDU [0]
    inner += _tlv(_TAG_NO_ASDU, _encode_uint(len(asdus), 1))

    # seqOfASDU [2] — SEQUENCE OF ASDU
    asdu_bytes = b"".join(_encode_asdu(a) for a in asdus)
    inner += _tlv(_TAG_SEQ_ASDU, asdu_bytes)

    # Wrap in savPDU APPLICATION 0 (0x60)
    return _tlv(0x60, bytes(inner))


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def encode_apdu(asdus: list[SvAsdu], app_id: int = 0x4001) -> bytes:
    """Encode an SV APDU (APPID header + savPDU).

    Parameters
    ----------
    asdus:
        List of :class:`~protoskipper_iec61850.sv.decoder.SvAsdu` to include.
    app_id:
        16-bit APPID (default 0x4001).

    Returns
    -------
    bytes
        Raw bytes starting with APPID (2), Length (2), Reserved1 (2),
        Reserved2 (2), then the ``savPDU``.
    """
    pdu = _encode_save_pdu(asdus)
    total_length = 8 + len(pdu)  # 8-byte common header
    header = struct.pack(">HH", app_id, total_length) + b"\x00\x00\x00\x00"
    return header + pdu


def encode_frame(spec: SvFrameSpec) -> bytes:
    """Build a complete Ethernet frame for the given :class:`SvFrameSpec`.

    Parameters
    ----------
    spec:
        Frame parameters (ASDUs, MACs, optional VLAN).

    Returns
    -------
    bytes
        Complete Ethernet II frame (no FCS).
    """
    apdu = encode_apdu(spec.asdus, spec.app_id)

    # Build Ethernet / 802.1Q header
    eth = bytearray()
    eth += spec.dst_mac
    eth += spec.src_mac

    if spec.vlan_id is not None:
        pcp_dei_vid = ((spec.vlan_priority & 0x07) << 13) | (spec.vlan_id & 0x0FFF)
        eth += struct.pack(">HH", 0x8100, pcp_dei_vid)

    eth += struct.pack(">H", ETHERTYPE_SV)
    return bytes(eth) + apdu
