# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""GOOSE Ethernet-frame dissector — P8.H.1.

Decodes a raw Ethernet frame containing an IEC 61850-8-1 GOOSE PDU.

Wire format
-----------
Ethernet header (14 bytes) → optional 802.1Q VLAN tag (4 bytes) →
EtherType 0x88B8 → APPID (2 bytes) + Length (2 bytes) + Reserved1 (2 bytes) +
Reserved2 (2 bytes) → IECGoosePdu APPLICATION 1 (0x61) BER-TLV.

The IECGoosePdu SEQUENCE fields are decoded in declaration order per
IEC 61850-8-1:2011 Clause 8.1.4.3 (Table 26).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

# EtherType for GOOSE
ETHERTYPE_GOOSE: int = 0x88B8

# BER tags for GOOSE PDU fields (context-specific, primitive/constructed)
_TAG_GOOSE_PDU: int = 0x61  # APPLICATION 1, constructed
_TAG_GCB_REF: int = 0x80  # [0] gocbRef
_TAG_TIME_ALLOWED: int = 0x81  # [1] timeAllowedtoLive
_TAG_DAT_SET: int = 0x82  # [2] datSet
_TAG_GO_ID: int = 0x83  # [3] goID
_TAG_T: int = 0x84  # [4] t (UtcTime 8 bytes)
_TAG_ST_NUM: int = 0x85  # [5] stNum
_TAG_SQ_NUM: int = 0x86  # [6] sqNum
_TAG_TEST: int = 0x87  # [7] test
_TAG_CONF_REV: int = 0x88  # [8] confRev
_TAG_NDS_COMM: int = 0x89  # [9] ndsCom
_TAG_NUM_DATASET_ENTRIES: int = 0x8A  # [10] numDatSetEntries
_TAG_ALL_DATA: int = 0xAB  # [11] allData SEQUENCE (context 11, constructed)

# MMS-style data tags inside allData
_TAG_MMS_BOOLEAN: int = 0x83
_TAG_MMS_INT: int = 0x85
_TAG_MMS_UINT: int = 0x86
_TAG_MMS_FLOAT: int = 0x87
_TAG_MMS_OCTET_STRING: int = 0x89
_TAG_MMS_VISIBLE_STRING: int = 0x8A
_TAG_MMS_UTC_TIME: int = 0x91
_TAG_MMS_BIT_STRING: int = 0x84
_TAG_MMS_STRUCT: int = 0xA2


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class GooseDissection:
    """Decoded GOOSE PDU from a raw Ethernet frame.

    Attributes
    ----------
    src_mac, dst_mac:
        Source/destination MAC addresses as colon-delimited hex strings.
    vlan_id:
        802.1Q VLAN ID or ``None`` if untagged.
    app_id:
        APPID field from the GOOSE common header.
    go_cb_ref:
        GoCB reference string.
    go_id:
        GOOSE ID string.
    dat_set:
        Dataset reference string.
    t_ms:
        Timestamp in milliseconds since Unix epoch.
    st_num:
        State number.
    sq_num:
        Sequence number.
    conf_rev:
        Configuration revision.
    time_allowed_ms:
        ``timeAllowedtoLive`` in milliseconds.
    test:
        ``True`` when the test bit is set.
    nds_comm:
        ``True`` when needsCommissioning is set.
    num_dataset_entries:
        Declared number of dataset entries.
    all_data:
        Decoded dataset values (Python native: bool / int / float / bytes / list).
    raw_apdu:
        Raw bytes of the entire GOOSE APDU (after Ethernet header).
    """

    src_mac: str
    dst_mac: str
    vlan_id: int | None
    app_id: int
    go_cb_ref: str
    go_id: str
    dat_set: str
    t_ms: int
    st_num: int
    sq_num: int
    conf_rev: int
    time_allowed_ms: int
    test: bool
    nds_comm: bool
    num_dataset_entries: int
    all_data: list[Any] = field(default_factory=list)
    raw_apdu: bytes = field(default_factory=bytes)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _decode_ber_length(data: bytes, offset: int) -> tuple[int, int]:
    """Return (length, new_offset)."""
    first = data[offset]
    offset += 1
    if first & 0x80 == 0:
        return first, offset
    n_bytes = first & 0x7F
    length = int.from_bytes(data[offset : offset + n_bytes], "big")
    return length, offset + n_bytes


def _decode_unsigned(data: bytes) -> int:
    return int.from_bytes(data, "big")


def _decode_utctime_ms(data: bytes) -> int:
    """Decode 8-byte IEC 61850 UtcTime to milliseconds."""
    if len(data) < 8:
        return 0
    seconds = struct.unpack_from(">I", data, 0)[0]
    frac_raw = int.from_bytes(data[4:7], "big")
    frac_ms = round(frac_raw / (1 << 24) * 1000)
    return seconds * 1000 + frac_ms


def _decode_mms_value(data: bytes, offset: int, end: int) -> tuple[Any, int]:
    """Decode a single MMS-encoded data value; return (value, new_offset)."""
    tag = data[offset]
    offset += 1
    length, offset = _decode_ber_length(data, offset)
    value_bytes = data[offset : offset + length]
    next_offset = offset + length

    if tag == _TAG_MMS_BOOLEAN:
        value: Any = bool(value_bytes[0]) if value_bytes else False
    elif tag == _TAG_MMS_INT:
        value = int.from_bytes(value_bytes, "big", signed=True)
    elif tag == _TAG_MMS_UINT:
        value = int.from_bytes(value_bytes, "big", signed=False)
    elif tag == _TAG_MMS_FLOAT:
        # first byte is exponent width, then IEEE 754
        value = struct.unpack_from(">f", value_bytes, 1)[0] if len(value_bytes) >= 5 else 0.0
    elif tag == _TAG_MMS_OCTET_STRING:
        value = bytes(value_bytes)
    elif tag == _TAG_MMS_VISIBLE_STRING:
        value = value_bytes.decode("ascii", errors="replace")
    elif tag == _TAG_MMS_UTC_TIME:
        value = _decode_utctime_ms(value_bytes)
    elif tag == _TAG_MMS_BIT_STRING:
        # first byte = unused bits count, rest are data
        value = bytes(value_bytes)
    elif tag == _TAG_MMS_STRUCT:
        # structure: recursively decode children
        children: list[Any] = []
        inner_off = 0
        while inner_off < len(value_bytes):
            child, inner_off = _decode_mms_value(value_bytes, inner_off, len(value_bytes))
            children.append(child)
        value = children
    else:
        value = bytes(value_bytes)  # unknown — return raw

    return value, next_offset


def _decode_all_data(data: bytes) -> list[Any]:
    """Decode the allData SEQUENCE content."""
    result: list[Any] = []
    offset = 0
    while offset < len(data):
        try:
            val, offset = _decode_mms_value(data, offset, len(data))
            result.append(val)
        except (IndexError, struct.error):
            break
    return result


def _decode_goose_pdu(apdu: bytes) -> dict[str, Any] | None:
    """Parse GOOSE PDU (APPLICATION 1) from *apdu* bytes."""
    if len(apdu) < 2:
        return None
    if apdu[0] != _TAG_GOOSE_PDU:
        return None
    pdu_len, offset = _decode_ber_length(apdu, 1)
    end = offset + pdu_len

    fields: dict[str, Any] = {
        "go_cb_ref": "",
        "go_id": "",
        "dat_set": "",
        "t_ms": 0,
        "st_num": 0,
        "sq_num": 0,
        "conf_rev": 0,
        "time_allowed_ms": 0,
        "test": False,
        "nds_comm": False,
        "num_dataset_entries": 0,
        "all_data": [],
    }

    while offset < end:
        tag = apdu[offset]
        offset += 1
        length, offset = _decode_ber_length(apdu, offset)
        value_bytes = apdu[offset : offset + length]
        offset += length

        if tag == _TAG_GCB_REF:
            fields["go_cb_ref"] = value_bytes.decode("ascii", errors="replace")
        elif tag == _TAG_TIME_ALLOWED:
            fields["time_allowed_ms"] = _decode_unsigned(value_bytes)
        elif tag == _TAG_DAT_SET:
            fields["dat_set"] = value_bytes.decode("ascii", errors="replace")
        elif tag == _TAG_GO_ID:
            fields["go_id"] = value_bytes.decode("ascii", errors="replace")
        elif tag == _TAG_T:
            fields["t_ms"] = _decode_utctime_ms(value_bytes)
        elif tag == _TAG_ST_NUM:
            fields["st_num"] = _decode_unsigned(value_bytes)
        elif tag == _TAG_SQ_NUM:
            fields["sq_num"] = _decode_unsigned(value_bytes)
        elif tag == _TAG_TEST:
            fields["test"] = bool(value_bytes[0]) if value_bytes else False
        elif tag == _TAG_CONF_REV:
            fields["conf_rev"] = _decode_unsigned(value_bytes)
        elif tag == _TAG_NDS_COMM:
            fields["nds_comm"] = bool(value_bytes[0]) if value_bytes else False
        elif tag == _TAG_NUM_DATASET_ENTRIES:
            fields["num_dataset_entries"] = _decode_unsigned(value_bytes)
        elif tag == _TAG_ALL_DATA:
            fields["all_data"] = _decode_all_data(value_bytes)

    return fields


def _strip_ethernet_header(
    frame: bytes,
) -> tuple[str, str, int | None, int, bytes]:
    """Return (src_mac, dst_mac, vlan_id, ethertype, payload)."""
    if len(frame) < 14:
        msg = "Frame too short for Ethernet header"
        raise ValueError(msg)

    dst_mac = _mac_str(frame[:6])
    src_mac = _mac_str(frame[6:12])
    ethertype = struct.unpack_from(">H", frame, 12)[0]
    offset = 14
    vlan_id: int | None = None

    if ethertype == 0x8100:  # 802.1Q
        if len(frame) < 18:
            msg = "Frame too short for 802.1Q tag"
            raise ValueError(msg)
        tci = struct.unpack_from(">H", frame, 14)[0]
        vlan_id = tci & 0x0FFF
        ethertype = struct.unpack_from(">H", frame, 16)[0]
        offset = 18

    return src_mac, dst_mac, vlan_id, ethertype, frame[offset:]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def decode_goose_frame(frame: bytes) -> GooseDissection | None:
    """Decode a raw Ethernet frame into a :class:`GooseDissection`.

    Returns ``None`` if the frame is not a valid GOOSE frame or cannot
    be decoded.

    Parameters
    ----------
    frame:
        Raw bytes of the complete Ethernet frame (including Ethernet header).
    """
    try:
        src_mac, dst_mac, vlan_id, ethertype, payload = _strip_ethernet_header(frame)
    except ValueError:
        return None

    if ethertype != ETHERTYPE_GOOSE:
        return None

    # Common header: APPID (2) + Length (2) + Reserved1 (2) + Reserved2 (2) = 8 bytes
    if len(payload) < 8:
        return None

    app_id = struct.unpack_from(">H", payload, 0)[0]
    apdu_start = 8
    raw_apdu = payload[apdu_start:]

    fields = _decode_goose_pdu(raw_apdu)
    if fields is None:
        return None

    return GooseDissection(
        src_mac=src_mac,
        dst_mac=dst_mac,
        vlan_id=vlan_id,
        app_id=app_id,
        go_cb_ref=fields["go_cb_ref"],
        go_id=fields["go_id"],
        dat_set=fields["dat_set"],
        t_ms=fields["t_ms"],
        st_num=fields["st_num"],
        sq_num=fields["sq_num"],
        conf_rev=fields["conf_rev"],
        time_allowed_ms=fields["time_allowed_ms"],
        test=fields["test"],
        nds_comm=fields["nds_comm"],
        num_dataset_entries=fields["num_dataset_entries"],
        all_data=fields["all_data"],
        raw_apdu=raw_apdu,
    )
