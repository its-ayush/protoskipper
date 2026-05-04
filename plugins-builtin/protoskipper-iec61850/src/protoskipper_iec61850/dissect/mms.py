# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""MMS Ethernet-frame dissector — P8.H.1.

Decodes a raw TCP payload (or Ethernet frame) containing an ISO/IEC 8073
(COTP) + ISO 8327-1 (Session) + ISO 8823-1 (Presentation) + MMS PDU stack.

This is a best-effort dissector aimed at extracting the MMS service type
and invoke ID from captured traffic.  It supports the common *Confirmed
Request / Confirmed Response / Unconfirmed* PDU types.

Wire format layers (on top of TCP port 102):
    TPKT (RFC 1006) → COTP DT → OSI Session → OSI Presentation → MMS BER

This dissector is intentionally shallow — it does NOT reconstruct
segmented MMS PDUs across multiple packets.  It skips OSI headers via
fixed-offset heuristics and looks for the MMS BER tag directly.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# MMS PDU type constants (from ISO 9506-1 / 61850-7-2)
# ---------------------------------------------------------------------------
MMS_CONFIRMED_REQUEST: int = 0xA0
MMS_CONFIRMED_RESPONSE: int = 0xA1
MMS_CONFIRMED_ERROR: int = 0xA2
MMS_UNCONFIRMED: int = 0xA3
MMS_REJECT: int = 0xA4
MMS_CANCEL_REQUEST: int = 0xA5
MMS_CANCEL_RESPONSE: int = 0xA6
MMS_CANCEL_ERROR: int = 0xA7
MMS_INITIATE_REQUEST: int = 0xA8
MMS_INITIATE_RESPONSE: int = 0xA9
MMS_CONCLUDE_REQUEST: int = 0xAA
MMS_CONCLUDE_RESPONSE: int = 0xAB
MMS_CONCLUDE_ERROR: int = 0xAC

# Well-known MMS confirmed service tags (context class, primitive/constructed)
_MMS_SERVICES: dict[int, str] = {
    0xA0: "Read",
    0xA1: "Write",
    0xA2: "GetNameList",
    0xA3: "GetVariableAccessAttributes",
    0xA4: "DefineNamedVariable",
    0xA5: "DefineScatteredAccess",
    0xA6: "GetScatteredAccessAttributes",
    0xA7: "DeleteVariableAccess",
    0xA8: "DefineNamedVariableList",
    0xA9: "GetNamedVariableListAttributes",
    0xAA: "DeleteNamedVariableList",
    0xAB: "ObtainFile",
    0xAC: "FileOpen",
    # Unconfirmed / informationReport
    0x00: "InformationReport",
}

# Minimal OSI overhead we skip heuristically
_TPKT_COTP_OVERHEAD: int = 7  # TPKT(4) + COTP DT(3)


@dataclass
class MmsDissection:
    """Decoded MMS PDU.

    Attributes
    ----------
    pdu_type:
        Top-level PDU tag (e.g. ``MMS_CONFIRMED_REQUEST``).
    pdu_type_name:
        Human-readable PDU type name.
    invoke_id:
        Invoke ID for confirmed requests/responses; ``-1`` if not present.
    service_name:
        Name of the MMS service (e.g. ``"Read"``) or empty string.
    raw_mms:
        Raw bytes of the MMS PDU (from the outermost MMS BER tag onward).
    errors:
        Parsing errors / warnings encountered, if any.
    """

    pdu_type: int
    pdu_type_name: str
    invoke_id: int
    service_name: str
    raw_mms: bytes = field(default_factory=bytes)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _decode_ber_length(data: bytes, offset: int) -> tuple[int, int]:
    """Return (length, new_offset) after reading a BER-encoded length."""
    first = data[offset]
    offset += 1
    if first & 0x80 == 0:
        return first, offset
    n = first & 0x7F
    if n == 0:
        return 0, offset  # indefinite form — not handled, treat as 0
    length = int.from_bytes(data[offset : offset + n], "big")
    return length, offset + n


def _find_mms_tag(data: bytes) -> int:
    """Heuristically locate the offset of the first MMS top-level PDU tag.

    Scans for a byte matching one of the known MMS outer tags after
    skipping the expected TPKT + COTP DT header bytes.

    Returns the offset, or ``-1`` if not found.
    """
    mms_tags = {
        MMS_CONFIRMED_REQUEST,
        MMS_CONFIRMED_RESPONSE,
        MMS_CONFIRMED_ERROR,
        MMS_UNCONFIRMED,
        MMS_REJECT,
        MMS_INITIATE_REQUEST,
        MMS_INITIATE_RESPONSE,
        MMS_CONCLUDE_REQUEST,
        MMS_CONCLUDE_RESPONSE,
    }

    # Skip TPKT(4) + COTP DT(3) = 7 bytes, then scan OSI Session + Presentation
    start = min(_TPKT_COTP_OVERHEAD, len(data))
    # Scan up to 64 bytes for the MMS tag
    for i in range(start, min(start + 64, len(data))):
        if data[i] in mms_tags:
            return i
    return -1


def _decode_invoke_id(content: bytes) -> int:
    """Extract invoke ID from confirmed request/response content bytes.

    The invoke ID is the first element of the SEQUENCE content of a
    Confirmed-RequestPDU / Confirmed-ResponsePDU, encoded as a plain
    INTEGER (tag 0x02).
    """
    if len(content) < 2:
        return -1
    if content[0] != 0x02:  # INTEGER tag
        return -1
    length = content[1]
    if len(content) < 2 + length:
        return -1
    return int.from_bytes(content[2 : 2 + length], "big", signed=False)


def _pdu_type_name(tag: int) -> str:
    names = {
        MMS_CONFIRMED_REQUEST: "ConfirmedRequest",
        MMS_CONFIRMED_RESPONSE: "ConfirmedResponse",
        MMS_CONFIRMED_ERROR: "ConfirmedError",
        MMS_UNCONFIRMED: "Unconfirmed",
        MMS_REJECT: "Reject",
        MMS_INITIATE_REQUEST: "InitiateRequest",
        MMS_INITIATE_RESPONSE: "InitiateResponse",
        MMS_CONCLUDE_REQUEST: "ConcludeRequest",
        MMS_CONCLUDE_RESPONSE: "ConcludeResponse",
    }
    return names.get(tag, f"Unknown(0x{tag:02x})")


def _decode_service_name(pdu_type: int, content: bytes) -> str:
    """Extract service name from confirmed PDU content."""
    if pdu_type not in (MMS_CONFIRMED_REQUEST, MMS_CONFIRMED_RESPONSE):
        return ""
    # Skip invoke ID field: 0x02 <len> <id...>
    offset = 0
    if len(content) < 2:
        return ""
    if content[0] == 0x02:
        id_len = content[1]
        offset = 2 + id_len
    if offset >= len(content):
        return ""
    service_tag = content[offset]
    return _MMS_SERVICES.get(service_tag, f"0x{service_tag:02x}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def decode_mms_frame(payload: bytes) -> MmsDissection | None:
    """Decode an MMS PDU from a TCP *payload* (post-TCP, raw bytes).

    Accepts either a raw TCP segment payload (containing TPKT + COTP +
    OSI layers) or bare MMS BER bytes.  Returns ``None`` if no MMS PDU
    can be identified.

    Parameters
    ----------
    payload:
        Raw bytes from the TCP payload (no Ethernet / IP / TCP headers).
    """
    if len(payload) < 4:
        return None

    # Check for TPKT magic (version=3)
    if payload[0] == 0x03:
        pkt_len = struct.unpack_from(">H", payload, 2)[0]
        payload = payload[:pkt_len]

    mms_offset = _find_mms_tag(payload)
    if mms_offset < 0:
        return None

    raw_mms = payload[mms_offset:]
    pdu_type = raw_mms[0]

    try:
        _length, content_start = _decode_ber_length(raw_mms, 1)
        content = raw_mms[content_start:]
    except (IndexError, ValueError):
        return MmsDissection(
            pdu_type=pdu_type,
            pdu_type_name=_pdu_type_name(pdu_type),
            invoke_id=-1,
            service_name="",
            raw_mms=raw_mms,
            errors=["BER length decode failed"],
        )

    invoke_id = _decode_invoke_id(content)
    service_name = _decode_service_name(pdu_type, content)

    return MmsDissection(
        pdu_type=pdu_type,
        pdu_type_name=_pdu_type_name(pdu_type),
        invoke_id=invoke_id,
        service_name=service_name,
        raw_mms=raw_mms,
    )
