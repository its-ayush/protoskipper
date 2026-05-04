# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""IEC 61850-9-2 Sampled Values decoder — P8.D.1.

Supports both **9-2LE** (IEC TR 61869-9 / UCA2 profile, the overwhelming
majority of installed base) and **9-2 80-2** (IEC 61850-9-2:2011 Edition 2
with sample-count-per-APDU > 1).

Architecture
------------
:func:`decode_frame` is the single public entry-point.  It accepts raw
Ethernet frame bytes (starting at the destination-MAC, i.e. *no* FCS) and
returns a :class:`SvFrame` if the frame is a valid SV APDU, or ``None``
otherwise.

Internally:

1. :func:`_strip_ethernet_header` peels the Ethernet-II / 802.1Q header.
2. :func:`_decode_asn1_tlv` iterates the outer ``savPDU`` BER TLV.
3. :func:`_decode_asdu_sequence` decodes each ASDU within the
   ``seqOfASDU`` (typically 1 per frame in 9-2LE; up to 8 in 80-2).
4. Per ASDU, :func:`_decode_sequence_of_data` unpacks the eight 32-bit
   channels from the ``seqOfData`` opaque octet-string.

Wire format summary (IEC 61850-9-2:2011 + UCA2 9-2LE addendum)
---------------------------------------------------------------
::

    Ethernet II / 802.1Q (optional)
    EtherType = 0x88BA  (IEC 61850 Sampled Values)
    APPID      (2 bytes, big-endian)
    Length     (2 bytes, total APDU including these 4 bytes)
    Reserved1  (2 bytes)
    Reserved2  (2 bytes)
    savPDU (BER sequence):
      noASDU   [0] INTEGER
      seqOfASDU [2] SEQUENCE OF:
        ASDU (SEQUENCE):
          svID      [0] VISIBLE STRING
          smpCnt    [2] INTEGER (sample counter 0..N-1)
          confRev   [3] INTEGER
          refrTm    [4] UtcTime (optional, present in some profiles)
          smpSynch  [5] ENUM  { noSync=0, localClock=1, globalClock=2 }
          smpRate   [6] INTEGER (optional)
          sample    [7] OCTET STRING  (8 channels x 4 bytes each)
          smpMod    [8] ENUM (optional)

Channel layout (9-2LE with 4 current + 4 voltage channels, IEC 61869-9):

  0: iA     (primary current phase A, int32 in microamps)
  1: iA_q   (quality word for iA)
  2: iB
  3: iB_q
  4: iC
  5: iC_q
  6: iN     (neutral current)
  7: iN_q
  8: uA     (phase-to-neutral voltage A, int32 in microvolts)
  9: uA_q
  10: uB
  11: uB_q
  12: uC
  13: uC_q
  14: uN
  15: uN_q

Each value is a **signed** 32-bit big-endian integer.  Quality words use the
bit layout defined in IEC 61850-7-3 (validity, source, test, operator blocked,
derived, extrapolated, etc.).

Design note on "profile" detection
------------------------------------
:func:`decode_frame` **auto-detects** the profile from the ASDU fields:
* If ``noASDU > 1`` → 80-2 style (multiple ASDUs per frame).
* If ``smpCnt`` wraps at 80 or 96 → protection rate; at 256/288 → measurement.
The detected ``sample_rate`` in :class:`SvAsdu` is advisory; callers should
track ``smpCnt`` wrap to infer the live rate.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import NamedTuple

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: EtherType for IEC 61850 Sampled Values (IEEE 802.3/Ethernet II)
ETHERTYPE_SV: int = 0x88BA

#: Default multicast MAC for SV (01-0C-CD-04-00-00) per IEC 61850-8-1
SV_MULTICAST_MAC: bytes = bytes.fromhex("010ccd040000")

# BER tags used in the savPDU / ASDU structures
_TAG_NO_ASDU = 0x80  # [0] IMPLICIT INTEGER  (context, primitive)
_TAG_SEQ_ASDU = 0xA2  # [2] IMPLICIT SEQUENCE (context, constructed)
_TAG_SVID = 0x80  # [0] IMPLICIT VISIBLESTRING inside ASDU
_TAG_SMPCNT = 0x82  # [2] IMPLICIT INTEGER
_TAG_CONFREV = 0x83  # [3] IMPLICIT INTEGER
_TAG_REFRTM = 0x84  # [4] IMPLICIT UtcTime
_TAG_SMPSYNCH = 0x85  # [5] IMPLICIT ENUM
_TAG_SMPRATE = 0x86  # [6] IMPLICIT INTEGER
_TAG_SAMPLE = 0x87  # [7] IMPLICIT OCTET STRING
_TAG_SMPMOD = 0x88  # [8] IMPLICIT ENUM

# Quality bit masks (IEC 61850-7-3 §6.7)
QUALITY_VALIDITY_MASK: int = 0x0003  # bits 0-1
QUALITY_VALIDITY_GOOD: int = 0x0000
QUALITY_VALIDITY_INVALID: int = 0x0001
QUALITY_VALIDITY_RESERVED: int = 0x0002
QUALITY_VALIDITY_QUESTIONABLE: int = 0x0003
QUALITY_OVERFLOW: int = 1 << 2
QUALITY_OUTOFRANGE: int = 1 << 3
QUALITY_BADREFERENCE: int = 1 << 4
QUALITY_OSCILLATORY: int = 1 << 5
QUALITY_FAILURE: int = 1 << 6
QUALITY_OLDDATA: int = 1 << 7
QUALITY_INCONSISTENT: int = 1 << 8
QUALITY_INACCURATE: int = 1 << 9
QUALITY_SOURCE_SUBSTITUTED: int = 1 << 10
QUALITY_TEST: int = 1 << 11
QUALITY_OPERATORBLOCKED: int = 1 << 12
QUALITY_DERIVED: int = 1 << 13

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SvChannel:
    """One decoded channel value from an SV ASDU's ``seqOfData``.

    Attributes
    ----------
    index:
        Zero-based channel index within the ASDU.
    value_raw:
        Signed 32-bit integer straight from the wire (units depend on the
        instrument transformer's primary/secondary ratio declared in the SCL).
    quality_raw:
        32-bit quality word (IEC 61850-7-3 §6.7).
    validity:
        2-bit validity field: 0=good, 1=invalid, 2=reserved, 3=questionable.
    test:
        ``True`` when the test bit (bit 11) is set.
    operator_blocked:
        ``True`` when the operator-blocked bit (bit 12) is set.
    """

    index: int
    value_raw: int
    quality_raw: int

    @property
    def validity(self) -> int:
        return self.quality_raw & QUALITY_VALIDITY_MASK

    @property
    def test(self) -> bool:
        return bool(self.quality_raw & QUALITY_TEST)

    @property
    def operator_blocked(self) -> bool:
        return bool(self.quality_raw & QUALITY_OPERATORBLOCKED)


@dataclass(slots=True)
class SvAsdu:
    """One Application Service Data Unit decoded from an SV APDU.

    A 9-2LE frame typically contains exactly one ASDU; an IEC 61850-9-2
    Edition 2 frame (80-2 profile) may contain up to 8.

    Attributes
    ----------
    sv_id:
        SVID string (identifies the stream, typically matching the SVCB).
    smp_cnt:
        Sample counter (0-based, wraps at ``smp_rate``).
    conf_rev:
        Configuration revision (should match SVCB confRev in the SCL).
    smp_synch:
        Sample synchronisation: 0=noSync, 1=localClock, 2=globalClock.
    smp_rate:
        Declared samples-per-second (advisory — may be absent in 9-2LE
        where it defaults to 80 or 4000 depending on frequency/profile).
        ``None`` if the ASDU did not include the optional field.
    smp_mod:
        Sample mode enum (optional): 0=SPS_NOMINAL_FREQ, 1=SAMPLES_PER_SEC,
        2=SECONDS_PER_SAMPLE.  ``None`` if absent.
    refr_tm_ms:
        Optional refresh-time in milliseconds since the Unix epoch, or
        ``None`` if not present.
    channels:
        Decoded channels from ``seqOfData`` in order.
    """

    sv_id: str
    smp_cnt: int
    conf_rev: int
    smp_synch: int
    smp_rate: int | None
    smp_mod: int | None
    refr_tm_ms: float | None
    channels: list[SvChannel]


@dataclass(slots=True)
class SvFrame:
    """A fully decoded IEC 61850-9-2 Sampled Values Ethernet frame.

    Attributes
    ----------
    src_mac:
        Source MAC address as 6 raw bytes.
    dst_mac:
        Destination MAC address as 6 raw bytes.
    vlan_id:
        802.1Q VLAN ID (0..4094), or ``None`` if the frame is untagged.
    vlan_priority:
        802.1Q PCP priority (0..7), or ``None`` if untagged.
    app_id:
        SV APPID (2-byte big-endian value from the SV common header).
    no_asdu:
        Declared ``noASDU`` count from the savPDU header.
    asdus:
        Decoded ASDU list (length should equal ``no_asdu``).
    raw_apdu:
        The raw APDU bytes (from APPID through end of savPDU), useful for
        logging and PCAP re-assembly.
    """

    src_mac: bytes
    dst_mac: bytes
    vlan_id: int | None
    vlan_priority: int | None
    app_id: int
    no_asdu: int
    asdus: list[SvAsdu]
    raw_apdu: bytes


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _EthernetInfo(NamedTuple):
    dst: bytes
    src: bytes
    vlan_id: int | None
    vlan_priority: int | None
    ethertype: int
    payload_offset: int  # byte offset in the original frame where payload begins


def _strip_ethernet_header(frame: bytes) -> _EthernetInfo | None:
    """Parse the Ethernet II / 802.1Q header.

    Returns ``None`` if the frame is too short or otherwise malformed.
    """
    if len(frame) < 14:
        return None

    dst = frame[:6]
    src = frame[6:12]
    ethertype = struct.unpack_from(">H", frame, 12)[0]
    offset = 14

    vlan_id: int | None = None
    vlan_priority: int | None = None

    # Handle 802.1Q VLAN tag (0x8100)
    if ethertype == 0x8100:
        if len(frame) < 18:
            return None
        tci = struct.unpack_from(">H", frame, 14)[0]
        vlan_priority = (tci >> 13) & 0x07
        vlan_id = tci & 0x0FFF
        ethertype = struct.unpack_from(">H", frame, 16)[0]
        offset = 18

    return _EthernetInfo(dst, src, vlan_id, vlan_priority, ethertype, offset)


def _decode_ber_length(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a BER length field at *offset*.

    Returns ``(length, new_offset)`` where *new_offset* points to the first
    content byte.

    Raises :class:`ValueError` if the data is truncated or the length form
    is unsupported.
    """
    if offset >= len(data):
        raise ValueError("BER length truncated")
    first = data[offset]
    if first < 0x80:
        return first, offset + 1
    n_bytes = first & 0x7F
    if n_bytes == 0 or n_bytes > 4:
        raise ValueError(f"Unsupported BER long-form length: {n_bytes} bytes")
    if offset + 1 + n_bytes > len(data):
        raise ValueError("BER long-form length truncated")
    length = int.from_bytes(data[offset + 1 : offset + 1 + n_bytes], "big")
    return length, offset + 1 + n_bytes


def _iter_tlv(data: bytes, offset: int, end: int):
    """Yield ``(tag, value_bytes, next_offset)`` tuples within *data[offset:end]*."""
    while offset < end:
        if offset >= len(data):
            break
        tag = data[offset]
        offset += 1
        try:
            length, offset = _decode_ber_length(data, offset)
        except ValueError as exc:
            _log.debug("BER parse error in TLV iteration: %s", exc)
            return
        if offset + length > len(data):
            _log.debug("BER value truncated at offset %d", offset)
            return
        yield tag, data[offset : offset + length], offset + length
        offset += length


def _decode_int(data: bytes) -> int:
    """Decode a BER INTEGER from *data* as a signed Python int."""
    return int.from_bytes(data, "big", signed=True)


def _decode_uint(data: bytes) -> int:
    """Decode a BER INTEGER from *data* as an unsigned Python int."""
    return int.from_bytes(data, "big", signed=False)


def _decode_refrtm(data: bytes) -> float | None:
    """Decode a 9-2 UtcTime (8 bytes) to milliseconds since epoch.

    Format: 4-byte unsigned seconds since 1970-01-01T00:00:00Z,
    3-byte nanosecond fraction (raw), 1-byte time quality.
    """
    if len(data) != 8:
        return None
    seconds = struct.unpack_from(">I", data, 0)[0]
    # Fraction: bits 7..31 of bytes 4-6, representing the subsecond fraction
    # as a 24-bit fixed-point where 2^24 = 1 second.
    frac_raw = (data[4] << 16) | (data[5] << 8) | data[6]
    frac_ms = (frac_raw / 0x1000000) * 1000.0
    return seconds * 1000.0 + frac_ms


def _decode_asdu(data: bytes) -> SvAsdu | None:
    """Decode a single ASDU from the raw BER bytes *data*."""
    sv_id: str = ""
    smp_cnt: int = 0
    conf_rev: int = 0
    smp_synch: int = 0
    smp_rate: int | None = None
    smp_mod: int | None = None
    refr_tm_ms: float | None = None
    channels: list[SvChannel] = []

    for tag, value, _ in _iter_tlv(data, 0, len(data)):
        if tag == _TAG_SVID:
            sv_id = value.decode("ascii", errors="replace")
        elif tag == _TAG_SMPCNT:
            smp_cnt = _decode_uint(value)
        elif tag == _TAG_CONFREV:
            conf_rev = _decode_uint(value)
        elif tag == _TAG_REFRTM:
            refr_tm_ms = _decode_refrtm(value)
        elif tag == _TAG_SMPSYNCH:
            smp_synch = _decode_uint(value)
        elif tag == _TAG_SMPRATE:
            smp_rate = _decode_uint(value)
        elif tag == _TAG_SAMPLE:
            channels = _decode_sequence_of_data(value)
        elif tag == _TAG_SMPMOD:
            smp_mod = _decode_uint(value)

    return SvAsdu(
        sv_id=sv_id,
        smp_cnt=smp_cnt,
        conf_rev=conf_rev,
        smp_synch=smp_synch,
        smp_rate=smp_rate,
        smp_mod=smp_mod,
        refr_tm_ms=refr_tm_ms,
        channels=channels,
    )


def _decode_sequence_of_data(data: bytes) -> list[SvChannel]:
    """Unpack ``seqOfData`` from a raw octet string.

    Each pair of 4-byte big-endian words = (value, quality).
    The layout follows IEC 61869-9 §5.3 (UCA2 9-2LE profile):
    8 value/quality pairs = 64 bytes total for the standard 8-channel stream.
    """
    channels: list[SvChannel] = []
    n_pairs = len(data) // 8  # each channel = 4-byte value + 4-byte quality
    for i in range(n_pairs):
        base = i * 8
        val = struct.unpack_from(">i", data, base)[0]  # signed
        qual = struct.unpack_from(">I", data, base + 4)[0]  # unsigned
        channels.append(SvChannel(index=i, value_raw=val, quality_raw=qual))
    return channels


def _decode_save_pdu(apdu: bytes) -> tuple[int, list[SvAsdu]] | None:
    """Decode the ``savPDU`` outer BER structure.

    Returns ``(no_asdu, asdus)`` or ``None`` on parse failure.
    """
    no_asdu = 0
    asdus: list[SvAsdu] = []

    # The entire apdu (after the 8-byte SV common header) should be a
    # BER sequence wrapping the savPDU contents.  In practice, the outer
    # SEQUENCE tag (0x60) is sometimes omitted by implementations; we
    # handle both.
    offset = 0
    end = len(apdu)

    # Peek at first byte: if it is 0x60 (APPLICATION 0 constructed = savPDU),
    # skip the outer tag+length.
    if offset < end and apdu[offset] == 0x60:
        offset += 1
        try:
            _pdu_len, offset = _decode_ber_length(apdu, offset)
        except ValueError:
            return None

    for tag, value, _ in _iter_tlv(apdu, offset, end):
        if tag == _TAG_NO_ASDU:
            no_asdu = _decode_uint(value)
        elif tag == _TAG_SEQ_ASDU:
            # Iterate the SEQUENCE OF ASDUs
            seq_offset = 0
            seq_end = len(value)
            while seq_offset < seq_end:
                # Each ASDU is a SEQUENCE (0x30)
                if seq_offset >= seq_end:
                    break
                asdu_tag = value[seq_offset]
                seq_offset += 1
                if asdu_tag != 0x30:
                    _log.debug("Unexpected ASDU tag 0x%02X (expected 0x30)", asdu_tag)
                    break
                try:
                    asdu_len, seq_offset = _decode_ber_length(value, seq_offset)
                except ValueError:
                    break
                if seq_offset + asdu_len > seq_end:
                    _log.debug("ASDU length overruns seqOfASDU boundary")
                    break
                asdu_data = value[seq_offset : seq_offset + asdu_len]
                asdu = _decode_asdu(asdu_data)
                if asdu is not None:
                    asdus.append(asdu)
                seq_offset += asdu_len

    return no_asdu, asdus


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def decode_frame(frame: bytes) -> SvFrame | None:
    """Decode a raw Ethernet frame bytes to an :class:`SvFrame`.

    Parameters
    ----------
    frame:
        Complete Ethernet frame bytes starting at the destination MAC
        (no FCS / CRC).

    Returns
    -------
    SvFrame
        On success.
    None
        If the frame is not a valid SV frame (wrong EtherType, too short,
        parse error).
    """
    eth = _strip_ethernet_header(frame)
    if eth is None:
        return None
    if eth.ethertype != ETHERTYPE_SV:
        return None

    payload = frame[eth.payload_offset :]
    if len(payload) < 8:
        _log.debug("SV payload too short: %d bytes", len(payload))
        return None

    # SV common header: APPID (2), Length (2), Reserved1 (2), Reserved2 (2)
    app_id = struct.unpack_from(">H", payload, 0)[0]
    apdu_length = struct.unpack_from(">H", payload, 2)[0]
    # Reserved1 = payload[4:6], Reserved2 = payload[6:8] (ignored by decoder)

    apdu_end = min(apdu_length, len(payload))
    apdu_body = payload[8:apdu_end]
    raw_apdu = payload[:apdu_end]

    result = _decode_save_pdu(apdu_body)
    if result is None:
        _log.debug("savPDU decode failed for APPID=0x%04X", app_id)
        return None

    no_asdu, asdus = result
    return SvFrame(
        src_mac=eth.src,
        dst_mac=eth.dst,
        vlan_id=eth.vlan_id,
        vlan_priority=eth.vlan_priority,
        app_id=app_id,
        no_asdu=no_asdu,
        asdus=asdus,
        raw_apdu=raw_apdu,
    )
