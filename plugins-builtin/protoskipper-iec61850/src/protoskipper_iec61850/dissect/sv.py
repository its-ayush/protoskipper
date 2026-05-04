# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""SV Ethernet-frame re-dissector — P8.H.1.

Wraps :func:`protoskipper_iec61850.sv.decoder.decode_frame` and adds the
:class:`SvDissection` result type expected by the PCAP filter engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoskipper_iec61850.sv.decoder import SvAsdu


@dataclass
class SvDissection:
    """Decoded SV frame for PCAP dissection.

    Attributes
    ----------
    src_mac, dst_mac:
        Source/destination MAC as colon-delimited hex strings.
    vlan_id:
        802.1Q VLAN ID or ``None`` if untagged.
    app_id:
        APPID field from the SV common header.
    no_asdu:
        Number of ASDUs in the frame.
    sv_id:
        ``svID`` of the first ASDU (empty string if no ASDUs).
    asdus:
        List of decoded ASDUs.
    raw_apdu:
        Raw APDU bytes after the Ethernet header.
    """

    src_mac: str
    dst_mac: str
    vlan_id: int | None
    app_id: int
    no_asdu: int
    sv_id: str
    asdus: list[SvAsdu] = field(default_factory=list)
    raw_apdu: bytes = field(default_factory=bytes)


def decode_sv_frame(frame: bytes) -> SvDissection | None:
    """Decode a raw Ethernet frame into an :class:`SvDissection`.

    Returns ``None`` if the frame is not a valid SV frame.
    """
    from protoskipper_iec61850.sv.decoder import decode_frame

    result = decode_frame(frame)
    if result is None:
        return None

    return SvDissection(
        src_mac=result.src_mac,
        dst_mac=result.dst_mac,
        vlan_id=result.vlan_id,
        app_id=result.app_id,
        no_asdu=result.no_asdu,
        sv_id=result.asdus[0].sv_id if result.asdus else "",
        asdus=list(result.asdus),
        raw_apdu=result.raw_apdu,
    )
