# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""ASDU codec for IEC 60870-5-104 (canonical subset).

This module implements encoding/decoding for the ASDU types most widely
used in real substations and industrial deployments. The full type-set
(1..127) is intentionally not implemented in the MVP; see
``docs/IEC104_PLAN.md`` for the deferred catalogue.

ASDU layout assumed (per IEC 60870-5-104 §7.1, default sizes):

* Type identification - 1 byte
* Variable structure qualifier (VSQ) - 1 byte: SQ(bit7) | number(bits0..6)
* Cause of transmission (COT) - 2 bytes: COT(byte0) | originator(byte1)
* Common address of ASDU (CA) - 2 bytes (LE)
* Information objects: each prefixed by IOA (3 bytes LE), elements vary
  by type id.

When SQ=1 the ASDU contains a single IOA followed by ``number`` elements
addressed at IOA, IOA+1, .... When SQ=0 each of the ``number`` elements is
preceded by its own IOA.

Field codecs (CP56Time2a, CP24Time2a, QDS, SIQ, DIQ, SCO, DCO, NVA) are
provided as standalone helpers so the master state machine and tests can
build raw frames directly.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import Any

from protoskipper.core.errors import EncodingError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TypeID(IntEnum):
    """ASDU type identifiers implemented in this MVP."""

    M_SP_NA_1 = 1  # single point info, no time tag
    M_DP_NA_1 = 3  # double point info, no time tag
    M_BO_NA_1 = 7  # bitstring 32 bit, no time tag
    M_ME_NA_1 = 9  # measured value, normalised, no time tag
    M_ME_NB_1 = 11  # measured value, scaled, no time tag
    M_ME_NC_1 = 13  # measured value, short float, no time tag
    M_IT_NA_1 = 15  # integrated totals (BCR), no time tag
    M_SP_TB_1 = 30  # single point info with CP56Time2a
    M_DP_TB_1 = 31  # double point info with CP56Time2a
    M_BO_TB_1 = 33  # bitstring 32 bit with CP56Time2a
    M_ME_TF_1 = 36  # measured value, short float, with CP56Time2a
    M_IT_TB_1 = 37  # integrated totals with CP56Time2a
    C_SC_NA_1 = 45  # single command
    C_DC_NA_1 = 46  # double command
    C_SE_NA_1 = 48  # set point command, normalised
    C_SE_NB_1 = 49  # set point command, scaled
    C_SE_NC_1 = 50  # set point command, short float
    C_BO_NA_1 = 51  # bitstring 32 bit command
    M_EI_NA_1 = 70  # end of initialisation
    C_IC_NA_1 = 100  # interrogation command
    C_CI_NA_1 = 101  # counter interrogation command
    C_RD_NA_1 = 102  # read command
    C_CS_NA_1 = 103  # clock synchronisation command
    # File transfer services (IEC 60870-5-101/104 section 7.3.5)
    F_FR_NA_1 = 120  # file ready
    F_SR_NA_1 = 121  # section ready
    F_SC_NA_1 = 122  # call directory / select / call file / call section
    F_LS_NA_1 = 123  # last section / last segment
    F_AF_NA_1 = 124  # ack file / ack section
    F_SG_NA_1 = 125  # segment
    F_DR_TA_1 = 126  # directory


class COT(IntEnum):
    """Cause of Transmission codes (subset)."""

    PER_CYC = 1
    BACK = 2
    SPONT = 3
    INIT = 4
    REQ = 5
    ACT = 6
    ACTCON = 7
    DEACT = 8
    DEACTCON = 9
    ACTTERM = 10
    INTROGEN = 20  # interrogated by station interrogation
    REQCOGEN = 37  # requested by general counter request
    UNKNOWN_TYPE = 44
    UNKNOWN_CAUSE = 45
    UNKNOWN_CA = 46
    UNKNOWN_IOA = 47


# Quality bits (for SIQ, DIQ, QDS)
QDS_OV = 0x01  # overflow
QDS_BL = 0x10  # blocked
QDS_SB = 0x20  # substituted
QDS_NT = 0x40  # not topical
QDS_IV = 0x80  # invalid

# QOI (qualifier of interrogation) values
QOI_STATION = 20
QOI_GROUP_1 = 21  # ... up to 36 for groups 1..16

# Single command qualifiers
SCO_EXEC = 0x00
SCO_SELECT = 0x80  # SE bit

# Counter interrogation command qualifier (QCC) - bits 0..5 RQT, 6..7 FRZ
QCC_RQT_GENERAL = 5  # general request counter
QCC_RQT_GROUP_1 = 1  # group 1 .. group 4 (1..4)
QCC_FRZ_READ = 0
QCC_FRZ_FREEZE_NORESET = 1
QCC_FRZ_FREEZE_RESET = 2
QCC_FRZ_RESET = 3

# BCR (Binary Counter Reading) flag bits in the trailing sequence byte
BCR_CY = 0x20  # carry
BCR_CA = 0x40  # counter adjusted
BCR_IV = 0x80  # invalid


# ---------------------------------------------------------------------------
# Information element codecs
# ---------------------------------------------------------------------------


def encode_cp56time2a(ts: datetime, *, invalid: bool = False, dst: bool = False) -> bytes:
    """Encode a UTC datetime as CP56Time2a (7 bytes).

    Year is stored as ``year - 2000`` and must therefore be in [2000, 2127].
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    year = ts.year - 2000
    if not 0 <= year <= 127:
        raise EncodingError(f"CP56Time2a year out of range: {ts.year} (must be 2000..2127)")
    ms = (ts.second * 1000 + ts.microsecond // 1000) & 0xFFFF
    minute = ts.minute & 0x3F
    if invalid:
        minute |= 0x80
    hour = ts.hour & 0x1F
    if dst:
        hour |= 0x80
    # Python: Mon=0..Sun=6; IEC: Mon=1..Sun=7. 0 is allowed (not used).
    dow = ((ts.weekday() + 1) & 0x07) << 5
    day = (ts.day & 0x1F) | dow
    month = ts.month & 0x0F
    yr_byte = year & 0x7F
    return struct.pack("<H", ms) + bytes([minute, hour, day, month, yr_byte])


def decode_cp56time2a(buf: bytes) -> datetime:
    """Decode CP56Time2a (7 bytes) into a UTC datetime.

    Drops the IV/SU/DOW flags; callers that need them should use
    :func:`decode_cp56time2a_full`.
    """
    return decode_cp56time2a_full(buf)[0]


def decode_cp56time2a_full(buf: bytes) -> tuple[datetime, bool, bool]:
    """Decode CP56Time2a, returning ``(timestamp, invalid_flag, dst_flag)``."""
    if len(buf) < 7:
        raise EncodingError(f"CP56Time2a needs 7 bytes, got {len(buf)}")
    (ms,) = struct.unpack("<H", buf[0:2])
    minute_b = buf[2]
    hour_b = buf[3]
    day_b = buf[4]
    month_b = buf[5]
    year_b = buf[6]
    invalid = bool(minute_b & 0x80)
    dst = bool(hour_b & 0x80)
    second = (ms // 1000) % 60
    micros = (ms % 1000) * 1000
    minute = minute_b & 0x3F
    hour = hour_b & 0x1F
    day = day_b & 0x1F
    month = month_b & 0x0F
    year = 2000 + (year_b & 0x7F)
    try:
        ts = datetime(year, month, day, hour, minute, second, micros, tzinfo=timezone.utc)
    except ValueError as exc:
        raise EncodingError(f"CP56Time2a decodes to invalid date: {exc}") from exc
    return ts, invalid, dst


def encode_cp24time2a(ts: datetime, *, invalid: bool = False) -> bytes:
    """Encode minute-resolution timestamp (3 bytes: ms LE + minute|IV)."""
    ms = (ts.second * 1000 + ts.microsecond // 1000) & 0xFFFF
    minute = ts.minute & 0x3F
    if invalid:
        minute |= 0x80
    return struct.pack("<H", ms) + bytes([minute])


def decode_cp24time2a_full(buf: bytes) -> tuple[int, int, bool]:
    """Decode CP24Time2a -> ``(milliseconds, minute, invalid)``.

    Note: CP24Time2a omits the date - callers must supply context.
    """
    if len(buf) < 3:
        raise EncodingError(f"CP24Time2a needs 3 bytes, got {len(buf)}")
    (ms,) = struct.unpack("<H", buf[0:2])
    minute_b = buf[2]
    return ms, minute_b & 0x3F, bool(minute_b & 0x80)


# ---------------------------------------------------------------------------
# Quality / value containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Quality:
    """Decoded quality bits common to SIQ / DIQ / QDS."""

    overflow: bool = False
    blocked: bool = False
    substituted: bool = False
    not_topical: bool = False
    invalid: bool = False

    def to_byte(self, low_bits: int = 0) -> int:
        b = low_bits & 0x0F
        if self.overflow:
            b |= QDS_OV
        if self.blocked:
            b |= QDS_BL
        if self.substituted:
            b |= QDS_SB
        if self.not_topical:
            b |= QDS_NT
        if self.invalid:
            b |= QDS_IV
        return b & 0xFF

    @classmethod
    def from_byte(cls, b: int) -> Quality:
        return cls(
            overflow=bool(b & QDS_OV),
            blocked=bool(b & QDS_BL),
            substituted=bool(b & QDS_SB),
            not_topical=bool(b & QDS_NT),
            invalid=bool(b & QDS_IV),
        )


def encode_siq(value: bool, q: Quality = Quality()) -> int:
    # SIQ: bit 0 = SPI, bits 1..3 reserved, bits 4..7 = BL/SB/NT/IV.
    # No OV bit (overflow does not apply to single point indications).
    high = q.to_byte() & 0xF0
    return (1 if value else 0) | high


def decode_siq(b: int) -> tuple[bool, Quality]:
    return bool(b & 0x01), Quality.from_byte(b & 0xF0)


def encode_diq(dpi: int, q: Quality = Quality()) -> int:
    """``dpi``: 0=intermediate, 1=off, 2=on, 3=indeterminate."""
    if not 0 <= dpi <= 3:
        raise EncodingError(f"DPI out of range: {dpi} (must be 0..3)")
    high = q.to_byte() & 0xF0
    return (dpi & 0x03) | high


def decode_diq(b: int) -> tuple[int, Quality]:
    return b & 0x03, Quality.from_byte(b & 0xF0)


def encode_qds(q: Quality = Quality()) -> int:
    return q.to_byte()


def decode_qds(b: int) -> Quality:
    return Quality.from_byte(b)


# ---------------------------------------------------------------------------
# ASDU container
# ---------------------------------------------------------------------------


@dataclass
class BinaryCounter:
    """Decoded BCR (Binary Counter Reading) - M_IT_* payload."""

    count: int  # signed 32-bit
    sequence: int = 0  # 5-bit free-running sequence number
    carry: bool = False
    adjusted: bool = False
    invalid: bool = False

    def to_bytes(self) -> bytes:
        seq = self.sequence & 0x1F
        if self.carry:
            seq |= BCR_CY
        if self.adjusted:
            seq |= BCR_CA
        if self.invalid:
            seq |= BCR_IV
        return struct.pack("<i", self.count) + bytes([seq])

    @classmethod
    def from_bytes(cls, buf: bytes) -> BinaryCounter:
        if len(buf) < 5:
            raise EncodingError(f"BCR needs 5 bytes, got {len(buf)}")
        (count,) = struct.unpack("<i", buf[0:4])
        seq = buf[4]
        return cls(
            count=count,
            sequence=seq & 0x1F,
            carry=bool(seq & BCR_CY),
            adjusted=bool(seq & BCR_CA),
            invalid=bool(seq & BCR_IV),
        )


@dataclass
class InformationObject:
    """One IOA + payload tuple inside an ASDU."""

    ioa: int
    value: Any  # decoded element (bool, float, datetime, etc.)
    quality: Quality | None = None
    timestamp: datetime | None = None
    raw_element: bytes = b""  # original element bytes (without IOA)
    select: bool = False  # SE bit on SCO/DCO/QOS for SBO commands
    qu: int = 0  # qualifier of command (5 bits) - SCO/DCO QU field, or QOS QL


@dataclass
class Asdu:
    """Decoded or under-construction ASDU."""

    type_id: TypeID
    cot: COT
    ca: int
    objects: list[InformationObject] = field(default_factory=list)
    sq: bool = False  # if True, single IOA + N elements
    test: bool = False  # T flag in COT byte
    negative: bool = False  # P/N flag in COT byte
    originator: int = 0


# ---------------------------------------------------------------------------
# Element size table (bytes per element, excluding IOA)
# ---------------------------------------------------------------------------

_ELEMENT_SIZES: dict[int, int] = {
    TypeID.M_SP_NA_1: 1,
    TypeID.M_DP_NA_1: 1,
    TypeID.M_BO_NA_1: 5,  # BSI(4) + QDS(1)
    TypeID.M_ME_NA_1: 3,  # NVA(2) + QDS(1)
    TypeID.M_ME_NB_1: 3,  # SVA(2) + QDS(1)
    TypeID.M_ME_NC_1: 5,  # float(4) + QDS(1)
    TypeID.M_IT_NA_1: 5,  # BCR(5)
    TypeID.M_SP_TB_1: 8,  # SIQ(1) + CP56(7)
    TypeID.M_DP_TB_1: 8,  # DIQ(1) + CP56(7)
    TypeID.M_BO_TB_1: 12,  # BSI(4) + QDS(1) + CP56(7)
    TypeID.M_ME_TF_1: 12,  # float(4) + QDS(1) + CP56(7)
    TypeID.M_IT_TB_1: 12,  # BCR(5) + CP56(7)
    TypeID.C_SC_NA_1: 1,  # SCO
    TypeID.C_DC_NA_1: 1,  # DCO
    TypeID.C_SE_NA_1: 3,  # NVA(2) + QOS(1)
    TypeID.C_SE_NB_1: 3,  # SVA(2) + QOS(1)
    TypeID.C_SE_NC_1: 5,  # float(4) + QOS(1)
    TypeID.C_BO_NA_1: 4,  # BSI(4) - no QDS on commands
    TypeID.M_EI_NA_1: 1,  # COI
    TypeID.C_IC_NA_1: 1,  # QOI
    TypeID.C_CI_NA_1: 1,  # QCC
    TypeID.C_RD_NA_1: 0,  # no element
    TypeID.C_CS_NA_1: 7,  # CP56Time2a
    # File transfer services (§7.3.5)
    TypeID.F_FR_NA_1: 4,  # length uint32_le
    TypeID.F_SR_NA_1: 5,  # section (1) + length uint32_le (4)
    TypeID.F_SC_NA_1: 2,  # call_type (1) + section (1)
    TypeID.F_LS_NA_1: 2,  # last_qualifier (1) + section (1)
    TypeID.F_AF_NA_1: 1,  # ack_type (1)
    TypeID.F_SG_NA_1: -1,  # variable-length: section (1) + LOS (1) + data (LOS bytes)
    TypeID.F_DR_TA_1: 13,  # name (8 bytes ASCII-padded) + length uint32_le (4) + status (1)
}


def element_size(type_id: TypeID) -> int:
    try:
        return _ELEMENT_SIZES[type_id]
    except KeyError as exc:
        raise EncodingError(f"Unsupported ASDU type id: {int(type_id)}") from exc


# ---------------------------------------------------------------------------
# Element encode / decode
# ---------------------------------------------------------------------------


def _encode_element(type_id: TypeID, obj: InformationObject) -> bytes:
    if type_id is TypeID.M_SP_NA_1:
        return bytes([encode_siq(bool(obj.value), obj.quality or Quality())])
    if type_id is TypeID.M_DP_NA_1:
        return bytes([encode_diq(int(obj.value), obj.quality or Quality())])
    if type_id is TypeID.M_BO_NA_1:
        return struct.pack("<I", int(obj.value) & 0xFFFFFFFF) + bytes(
            [encode_qds(obj.quality or Quality())]
        )
    if type_id is TypeID.M_ME_NA_1:
        nva = int(obj.value) & 0xFFFF
        return struct.pack("<h", _to_signed16(nva)) + bytes([encode_qds(obj.quality or Quality())])
    if type_id is TypeID.M_ME_NB_1:
        return struct.pack("<h", int(obj.value)) + bytes([encode_qds(obj.quality or Quality())])
    if type_id is TypeID.M_ME_NC_1:
        return struct.pack("<f", float(obj.value)) + bytes([encode_qds(obj.quality or Quality())])
    if type_id is TypeID.M_IT_NA_1:
        bcr = obj.value if isinstance(obj.value, BinaryCounter) else BinaryCounter(int(obj.value))
        return bcr.to_bytes()
    if type_id is TypeID.M_SP_TB_1:
        ts = obj.timestamp or datetime.now(tz=timezone.utc)
        return bytes([encode_siq(bool(obj.value), obj.quality or Quality())]) + encode_cp56time2a(
            ts
        )
    if type_id is TypeID.M_DP_TB_1:
        ts = obj.timestamp or datetime.now(tz=timezone.utc)
        return bytes([encode_diq(int(obj.value), obj.quality or Quality())]) + encode_cp56time2a(ts)
    if type_id is TypeID.M_BO_TB_1:
        ts = obj.timestamp or datetime.now(tz=timezone.utc)
        return (
            struct.pack("<I", int(obj.value) & 0xFFFFFFFF)
            + bytes([encode_qds(obj.quality or Quality())])
            + encode_cp56time2a(ts)
        )
    if type_id is TypeID.M_ME_TF_1:
        ts = obj.timestamp or datetime.now(tz=timezone.utc)
        return (
            struct.pack("<f", float(obj.value))
            + bytes([encode_qds(obj.quality or Quality())])
            + encode_cp56time2a(ts)
        )
    if type_id is TypeID.M_IT_TB_1:
        ts = obj.timestamp or datetime.now(tz=timezone.utc)
        bcr = obj.value if isinstance(obj.value, BinaryCounter) else BinaryCounter(int(obj.value))
        return bcr.to_bytes() + encode_cp56time2a(ts)
    if type_id is TypeID.C_SC_NA_1:
        # SCO: bit 0 = SCS, bits 2..6 = QU, bit 7 = SE
        sco = (1 if obj.value else 0) & 0x01
        sco |= (obj.qu & 0x1F) << 2
        if obj.select:
            sco |= 0x80
        return bytes([sco])
    if type_id is TypeID.C_DC_NA_1:
        # DCO: bits 0..1 = DCS, bits 2..6 = QU, bit 7 = SE
        dco = int(obj.value) & 0x03
        dco |= (obj.qu & 0x1F) << 2
        if obj.select:
            dco |= 0x80
        return bytes([dco])
    if type_id is TypeID.C_SE_NA_1:
        nva = _to_signed16(int(obj.value) & 0xFFFF)
        qos = (obj.qu & 0x7F) | (0x80 if obj.select else 0)
        return struct.pack("<h", nva) + bytes([qos])
    if type_id is TypeID.C_SE_NB_1:
        qos = (obj.qu & 0x7F) | (0x80 if obj.select else 0)
        return struct.pack("<h", int(obj.value)) + bytes([qos])
    if type_id is TypeID.C_SE_NC_1:
        qos = (obj.qu & 0x7F) | (0x80 if obj.select else 0)
        return struct.pack("<f", float(obj.value)) + bytes([qos])
    if type_id is TypeID.C_BO_NA_1:
        return struct.pack("<I", int(obj.value) & 0xFFFFFFFF)
    if type_id is TypeID.M_EI_NA_1:
        return bytes([int(obj.value) & 0xFF])
    if type_id is TypeID.C_IC_NA_1:
        return bytes([int(obj.value) & 0xFF])
    if type_id is TypeID.C_CI_NA_1:
        return bytes([int(obj.value) & 0xFF])
    if type_id is TypeID.C_RD_NA_1:
        return b""
    if type_id is TypeID.C_CS_NA_1:
        ts = obj.timestamp or (
            obj.value if isinstance(obj.value, datetime) else datetime.now(tz=timezone.utc)
        )
        return encode_cp56time2a(ts)
    # File transfer services
    if type_id is TypeID.F_FR_NA_1:
        return struct.pack("<I", int(obj.value) if obj.value is not None else 0)
    if type_id is TypeID.F_SR_NA_1:
        section = int(obj.quality) if obj.quality is not None else 1
        length = int(obj.value) if obj.value is not None else 0
        return bytes([section]) + struct.pack("<I", length)
    if type_id is TypeID.F_SC_NA_1:
        call_type = int(obj.value) if obj.value is not None else 0
        section = int(obj.quality) if obj.quality is not None else 0
        return bytes([call_type & 0xFF, section & 0xFF])
    if type_id is TypeID.F_LS_NA_1:
        last_qual = int(obj.value) if obj.value is not None else 1
        section = int(obj.quality) if obj.quality is not None else 0
        return bytes([last_qual & 0xFF, section & 0xFF])
    if type_id is TypeID.F_AF_NA_1:
        ack_type = int(obj.value) if obj.value is not None else 0
        return bytes([ack_type & 0xFF])
    if type_id is TypeID.F_SG_NA_1:
        section = int(obj.quality) if obj.quality is not None else 0
        payload = obj.value if isinstance(obj.value, (bytes, bytearray)) else b""
        seg_len = min(len(payload), 238)
        return bytes([section & 0xFF, seg_len & 0xFF]) + bytes(payload[:seg_len])
    if type_id is TypeID.F_DR_TA_1:
        name = str(obj.value) if obj.value is not None else ""
        name_bytes = name.encode("ascii", errors="replace")[:8].ljust(8, b"\x00")
        length = int(obj.quality) if obj.quality is not None else 0
        return name_bytes + struct.pack("<I", length) + bytes([0])
    raise EncodingError(f"Unsupported type id for encoding: {int(type_id)}")


def _decode_element(type_id: TypeID, buf: bytes, ioa: int) -> InformationObject:
    if type_id is TypeID.M_SP_NA_1:
        v, q = decode_siq(buf[0])
        return InformationObject(ioa=ioa, value=v, quality=q, raw_element=buf)
    if type_id is TypeID.M_DP_NA_1:
        v, q = decode_diq(buf[0])
        return InformationObject(ioa=ioa, value=v, quality=q, raw_element=buf)
    if type_id is TypeID.M_BO_NA_1:
        (bsi,) = struct.unpack("<I", buf[0:4])
        q = decode_qds(buf[4])
        return InformationObject(ioa=ioa, value=bsi, quality=q, raw_element=buf)
    if type_id is TypeID.M_ME_NA_1:
        (nva,) = struct.unpack("<h", buf[0:2])
        q = decode_qds(buf[2])
        return InformationObject(ioa=ioa, value=nva, quality=q, raw_element=buf)
    if type_id is TypeID.M_ME_NB_1:
        (sva,) = struct.unpack("<h", buf[0:2])
        q = decode_qds(buf[2])
        return InformationObject(ioa=ioa, value=sva, quality=q, raw_element=buf)
    if type_id is TypeID.M_ME_NC_1:
        (f,) = struct.unpack("<f", buf[0:4])
        q = decode_qds(buf[4])
        return InformationObject(ioa=ioa, value=f, quality=q, raw_element=buf)
    if type_id is TypeID.M_IT_NA_1:
        bcr = BinaryCounter.from_bytes(buf[0:5])
        return InformationObject(ioa=ioa, value=bcr, raw_element=buf)
    if type_id is TypeID.M_SP_TB_1:
        v, q = decode_siq(buf[0])
        ts = decode_cp56time2a(buf[1:8])
        return InformationObject(ioa=ioa, value=v, quality=q, timestamp=ts, raw_element=buf)
    if type_id is TypeID.M_DP_TB_1:
        v, q = decode_diq(buf[0])
        ts = decode_cp56time2a(buf[1:8])
        return InformationObject(ioa=ioa, value=v, quality=q, timestamp=ts, raw_element=buf)
    if type_id is TypeID.M_BO_TB_1:
        (bsi,) = struct.unpack("<I", buf[0:4])
        q = decode_qds(buf[4])
        ts = decode_cp56time2a(buf[5:12])
        return InformationObject(ioa=ioa, value=bsi, quality=q, timestamp=ts, raw_element=buf)
    if type_id is TypeID.M_ME_TF_1:
        (f,) = struct.unpack("<f", buf[0:4])
        q = decode_qds(buf[4])
        ts = decode_cp56time2a(buf[5:12])
        return InformationObject(ioa=ioa, value=f, quality=q, timestamp=ts, raw_element=buf)
    if type_id is TypeID.M_IT_TB_1:
        bcr = BinaryCounter.from_bytes(buf[0:5])
        ts = decode_cp56time2a(buf[5:12])
        return InformationObject(ioa=ioa, value=bcr, timestamp=ts, raw_element=buf)
    if type_id is TypeID.C_SC_NA_1:
        sco = buf[0]
        return InformationObject(
            ioa=ioa,
            value=bool(sco & 0x01),
            select=bool(sco & 0x80),
            qu=(sco >> 2) & 0x1F,
            raw_element=buf,
        )
    if type_id is TypeID.C_DC_NA_1:
        dco = buf[0]
        return InformationObject(
            ioa=ioa,
            value=dco & 0x03,
            select=bool(dco & 0x80),
            qu=(dco >> 2) & 0x1F,
            raw_element=buf,
        )
    if type_id is TypeID.C_SE_NA_1:
        (nva,) = struct.unpack("<h", buf[0:2])
        qos = buf[2]
        return InformationObject(
            ioa=ioa, value=nva, select=bool(qos & 0x80), qu=qos & 0x7F, raw_element=buf
        )
    if type_id is TypeID.C_SE_NB_1:
        (sva,) = struct.unpack("<h", buf[0:2])
        qos = buf[2]
        return InformationObject(
            ioa=ioa, value=sva, select=bool(qos & 0x80), qu=qos & 0x7F, raw_element=buf
        )
    if type_id is TypeID.C_SE_NC_1:
        (f,) = struct.unpack("<f", buf[0:4])
        qos = buf[4]
        return InformationObject(
            ioa=ioa, value=f, select=bool(qos & 0x80), qu=qos & 0x7F, raw_element=buf
        )
    if type_id is TypeID.C_BO_NA_1:
        (bsi,) = struct.unpack("<I", buf[0:4])
        return InformationObject(ioa=ioa, value=bsi, raw_element=buf)
    if type_id is TypeID.M_EI_NA_1:
        return InformationObject(ioa=ioa, value=buf[0], raw_element=buf)
    if type_id is TypeID.C_IC_NA_1:
        return InformationObject(ioa=ioa, value=buf[0], raw_element=buf)
    if type_id is TypeID.C_CI_NA_1:
        return InformationObject(ioa=ioa, value=buf[0], raw_element=buf)
    if type_id is TypeID.C_RD_NA_1:
        return InformationObject(ioa=ioa, value=None, raw_element=buf)
    if type_id is TypeID.C_CS_NA_1:
        ts = decode_cp56time2a(buf[0:7])
        return InformationObject(ioa=ioa, value=ts, timestamp=ts, raw_element=buf)
    # File transfer services
    if type_id is TypeID.F_FR_NA_1:
        (length,) = struct.unpack("<I", buf[0:4])
        return InformationObject(ioa=ioa, value=length, raw_element=buf)
    if type_id is TypeID.F_SR_NA_1:
        section = buf[0]
        (length,) = struct.unpack("<I", buf[1:5])
        return InformationObject(ioa=ioa, value=length, quality=section, raw_element=buf)
    if type_id is TypeID.F_SC_NA_1:
        call_type = buf[0]
        section = buf[1] if len(buf) > 1 else 0
        return InformationObject(ioa=ioa, value=call_type, quality=section, raw_element=buf)
    if type_id is TypeID.F_LS_NA_1:
        last_qual = buf[0]
        section = buf[1] if len(buf) > 1 else 0
        return InformationObject(ioa=ioa, value=last_qual, quality=section, raw_element=buf)
    if type_id is TypeID.F_AF_NA_1:
        ack_type = buf[0]
        return InformationObject(ioa=ioa, value=ack_type, raw_element=buf)
    if type_id is TypeID.F_SG_NA_1:
        section = buf[0]
        seg_len = buf[1] if len(buf) > 1 else 0
        payload = bytes(buf[2 : 2 + seg_len])
        return InformationObject(ioa=ioa, value=payload, quality=section, raw_element=buf)
    if type_id is TypeID.F_DR_TA_1:
        name = buf[0:8].rstrip(b"\x00").decode("ascii", errors="replace")
        (length,) = struct.unpack("<I", buf[8:12])
        return InformationObject(ioa=ioa, value=name, quality=length, raw_element=buf)
    raise EncodingError(f"Unsupported type id for decoding: {int(type_id)}")


def _to_signed16(u: int) -> int:
    return u - 0x10000 if u & 0x8000 else u


# ---------------------------------------------------------------------------
# ASDU encode / decode
# ---------------------------------------------------------------------------


def encode_asdu(asdu: Asdu) -> bytes:
    n = len(asdu.objects)
    if n == 0:
        raise EncodingError("ASDU must contain at least one information object")
    if n > 0x7F:
        raise EncodingError(f"VSQ number field overflow: {n} (max 127)")
    if asdu.sq and n < 1:
        raise EncodingError("SQ=1 requires at least one element")
    vsq = (0x80 if asdu.sq else 0) | (n & 0x7F)
    cot_byte = int(asdu.cot) & 0x3F
    if asdu.negative:
        cot_byte |= 0x40
    if asdu.test:
        cot_byte |= 0x80
    header = bytes(
        [
            int(asdu.type_id) & 0xFF,
            vsq,
            cot_byte,
            asdu.originator & 0xFF,
        ]
    ) + struct.pack("<H", asdu.ca & 0xFFFF)

    body = bytearray()
    type_id = asdu.type_id
    if asdu.sq:
        if type_id is TypeID.C_RD_NA_1:
            raise EncodingError("Read command does not support SQ=1 sequence form")
        body += _encode_ioa(asdu.objects[0].ioa)
        for obj in asdu.objects:
            body += _encode_element(type_id, obj)
    else:
        for obj in asdu.objects:
            body += _encode_ioa(obj.ioa)
            body += _encode_element(type_id, obj)
    return header + bytes(body)


def decode_asdu(buf: bytes) -> Asdu:
    if len(buf) < 6:
        raise EncodingError(f"ASDU header truncated: {len(buf)} bytes")
    type_raw = buf[0]
    try:
        type_id = TypeID(type_raw)
    except ValueError as exc:
        raise EncodingError(f"Unsupported ASDU type id: {type_raw}") from exc
    vsq = buf[1]
    sq = bool(vsq & 0x80)
    n = vsq & 0x7F
    cot_raw = buf[2]
    cot_value = cot_raw & 0x3F
    negative = bool(cot_raw & 0x40)
    test = bool(cot_raw & 0x80)
    try:
        cot = COT(cot_value)
    except ValueError:
        cot = COT(cot_value) if cot_value in COT._value2member_map_ else COT.SPONT  # type: ignore[arg-type]
    originator = buf[3]
    (ca,) = struct.unpack("<H", buf[4:6])

    objects: list[InformationObject] = []
    pos = 6
    elem_size = element_size(type_id)
    # elem_size == -1 → variable-length element (e.g. F_SG_NA_1: section + LOS + data)
    if sq:
        if pos + 3 > len(buf):
            raise EncodingError("Truncated ASDU: missing IOA in SQ form")
        ioa = _decode_ioa(buf[pos : pos + 3])
        pos += 3
        for i in range(n):
            if elem_size == -1:
                # Variable-length: read section (1) + LOS (1), then LOS data bytes
                if pos + 2 > len(buf):
                    raise EncodingError(f"Truncated variable-length element {i} of {n}")
                los = buf[pos + 1]
                actual_size = 2 + los
            else:
                actual_size = elem_size
            if pos + actual_size > len(buf):
                raise EncodingError(
                    f"Truncated ASDU: element {i} of {n} (need {actual_size} bytes)"
                )
            element = bytes(buf[pos : pos + actual_size])
            pos += actual_size
            objects.append(_decode_element(type_id, element, ioa + i))
    else:
        for i in range(n):
            if pos + 3 > len(buf):
                raise EncodingError(f"Truncated ASDU: object {i} of {n} (need IOA 3 bytes)")
            ioa = _decode_ioa(buf[pos : pos + 3])
            pos += 3
            if elem_size == -1:
                # Variable-length: read section (1) + LOS (1), then LOS data bytes
                if pos + 2 > len(buf):
                    raise EncodingError(f"Truncated variable-length element {i} of {n}")
                los = buf[pos + 1]
                actual_size = 2 + los
            else:
                actual_size = elem_size
            if pos + actual_size > len(buf):
                raise EncodingError(
                    f"Truncated ASDU: object {i} of {n} (need {3 + actual_size} bytes)"
                )
            element = bytes(buf[pos : pos + actual_size])
            pos += actual_size
            objects.append(_decode_element(type_id, element, ioa))

    return Asdu(
        type_id=type_id,
        cot=cot,
        ca=ca,
        objects=objects,
        sq=sq,
        test=test,
        negative=negative,
        originator=originator,
    )


def _encode_ioa(ioa: int) -> bytes:
    if not 0 <= ioa <= 0xFFFFFF:
        raise EncodingError(f"IOA out of 24-bit range: {ioa}")
    return bytes([ioa & 0xFF, (ioa >> 8) & 0xFF, (ioa >> 16) & 0xFF])


def _decode_ioa(buf: bytes) -> int:
    return buf[0] | (buf[1] << 8) | (buf[2] << 16)


# ---------------------------------------------------------------------------
# Convenience builders for common master commands
# ---------------------------------------------------------------------------


def build_general_interrogation(ca: int, qoi: int = QOI_STATION) -> bytes:
    """Build the body of a C_IC_NA_1 station interrogation ASDU."""
    asdu = Asdu(
        type_id=TypeID.C_IC_NA_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=0, value=qoi)],
    )
    return encode_asdu(asdu)


def build_clock_sync(ca: int, ts: datetime | None = None) -> bytes:
    """Build the body of a C_CS_NA_1 clock sync ASDU."""
    ts = ts or datetime.now(tz=timezone.utc)
    asdu = Asdu(
        type_id=TypeID.C_CS_NA_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=0, value=ts, timestamp=ts)],
    )
    return encode_asdu(asdu)


def build_read_command(ca: int, ioa: int) -> bytes:
    """Build the body of a C_RD_NA_1 read command ASDU."""
    asdu = Asdu(
        type_id=TypeID.C_RD_NA_1,
        cot=COT.REQ,
        ca=ca,
        objects=[InformationObject(ioa=ioa, value=None)],
    )
    return encode_asdu(asdu)


def build_single_command(
    ca: int, ioa: int, on: bool, *, select: bool = False, qu: int = 0
) -> bytes:
    """Build the body of a C_SC_NA_1 single command.

    ``select=True`` sets the SE bit (Select-Before-Operate); ``qu`` packs into
    the QU field of SCO (5 bits).
    """
    asdu = Asdu(
        type_id=TypeID.C_SC_NA_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=ioa, value=bool(on), select=select, qu=qu)],
    )
    return encode_asdu(asdu)


def build_double_command(
    ca: int, ioa: int, dcs: int, *, select: bool = False, qu: int = 0
) -> bytes:
    """Build the body of a C_DC_NA_1 double command.

    ``dcs``: 1 = OFF, 2 = ON. (0 and 3 are not permitted.)
    ``select=True`` sets the SE bit (Select-Before-Operate).
    """
    if dcs not in (1, 2):
        raise EncodingError(f"DCS must be 1 (OFF) or 2 (ON), got {dcs}")
    asdu = Asdu(
        type_id=TypeID.C_DC_NA_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=ioa, value=dcs, select=select, qu=qu)],
    )
    return encode_asdu(asdu)


def build_set_point_normalised(
    ca: int, ioa: int, value: int, *, select: bool = False, ql: int = 0
) -> bytes:
    """Build the body of a C_SE_NA_1 set-point command (normalised, signed 16-bit)."""
    asdu = Asdu(
        type_id=TypeID.C_SE_NA_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=ioa, value=value, select=select, qu=ql)],
    )
    return encode_asdu(asdu)


def build_set_point_scaled(
    ca: int, ioa: int, value: int, *, select: bool = False, ql: int = 0
) -> bytes:
    """Build the body of a C_SE_NB_1 set-point command (scaled, signed 16-bit)."""
    asdu = Asdu(
        type_id=TypeID.C_SE_NB_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=ioa, value=value, select=select, qu=ql)],
    )
    return encode_asdu(asdu)


def build_set_point_float(
    ca: int, ioa: int, value: float, *, select: bool = False, ql: int = 0
) -> bytes:
    """Build the body of a C_SE_NC_1 set-point command (IEEE 754 short float)."""
    asdu = Asdu(
        type_id=TypeID.C_SE_NC_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=ioa, value=value, select=select, qu=ql)],
    )
    return encode_asdu(asdu)


def build_bitstring_command(ca: int, ioa: int, value: int) -> bytes:
    """Build the body of a C_BO_NA_1 32-bit bitstring command."""
    asdu = Asdu(
        type_id=TypeID.C_BO_NA_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=ioa, value=value)],
    )
    return encode_asdu(asdu)


def build_counter_interrogation(
    ca: int, rqt: int = QCC_RQT_GENERAL, frz: int = QCC_FRZ_READ
) -> bytes:
    """Build the body of a C_CI_NA_1 counter interrogation ASDU.

    ``rqt``: request type, 1..4 = group 1..4, 5 = general.
    ``frz``: freeze action, 0=read, 1=freeze, 2=freeze+reset, 3=reset.
    """
    if not 1 <= rqt <= 5:
        raise EncodingError(f"QCC RQT must be 1..5, got {rqt}")
    if not 0 <= frz <= 3:
        raise EncodingError(f"QCC FRZ must be 0..3, got {frz}")
    qcc = (rqt & 0x3F) | ((frz & 0x03) << 6)
    asdu = Asdu(
        type_id=TypeID.C_CI_NA_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=0, value=qcc)],
    )
    return encode_asdu(asdu)
