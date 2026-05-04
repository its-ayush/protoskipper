# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""PCAP dissection scaffold for BACnet traffic analysis (P7.G).

Opens a ``.pcap`` / ``.pcapng`` capture file and dissects BVLC / NPDU / APDU
layers.  Requires the optional ``dpkt`` package (``pip install dpkt``).  If
``dpkt`` is not installed the reader raises :class:`PcapUnavailable`.

The dissector is intentionally read-only — it never opens a socket.

Usage::

    from protoskipper.builtin_drivers.bacnet.pcap import PcapReader

    with PcapReader("capture.pcapng") as reader:
        for frame in reader.frames():
            print(frame.src, frame.service, frame.apdu_type)

    # Filtered view
    for frame in reader.filter(service="readProperty"):
        print(frame)
"""

from __future__ import annotations

import struct
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["BACnetFrame", "PcapReader", "PcapUnavailable"]

# BACnet/IP UDP port
BACNET_PORT = 47808  # 0xBAC0

# BVLC function codes (ASHRAE Annex J)
BVLC_FUNCTIONS: dict[int, str] = {
    0x00: "BVLL-Result",
    0x01: "Write-Broadcast-Distribution-Table",
    0x02: "Read-Broadcast-Distribution-Table",
    0x03: "Read-Broadcast-Distribution-Table-Ack",
    0x04: "Forwarded-NPDU",
    0x05: "Register-Foreign-Device",
    0x06: "Read-Foreign-Device-Table",
    0x07: "Read-Foreign-Device-Table-Ack",
    0x08: "Delete-Foreign-Device-Table-Entry",
    0x09: "Distribute-Broadcast-To-Network",
    0x0A: "Original-Unicast-NPDU",
    0x0B: "Original-Broadcast-NPDU",
    0x0C: "Secure-BVLL",
}

# APDU type names (ASHRAE clause 20)
APDU_TYPES: dict[int, str] = {
    0: "Confirmed-Request",
    1: "Unconfirmed-Request",
    2: "SimpleACK",
    3: "ComplexACK",
    4: "SegmentACK",
    5: "Error",
    6: "Reject",
    7: "Abort",
}

# Confirmed service choice names (truncated; full table in ASHRAE Table 21-1)
CONFIRMED_SERVICES: dict[int, str] = {
    0: "acknowledgeAlarm",
    1: "confirmedCOVNotification",
    2: "confirmedEventNotification",
    3: "getAlarmSummary",
    4: "getEnrollmentSummary",
    5: "subscribeCOV",
    6: "atomicReadFile",
    7: "atomicWriteFile",
    8: "addListElement",
    9: "removeListElement",
    10: "createObject",
    11: "deleteObject",
    12: "readProperty",
    14: "readPropertyMultiple",
    15: "writeProperty",
    16: "writePropertyMultiple",
    17: "deviceCommunicationControl",
    18: "confirmedPrivateTransfer",
    19: "confirmedTextMessage",
    20: "reinitializeDevice",
    21: "vtOpen",
    22: "vtClose",
    23: "vtData",
    26: "subscribeCOVProperty",
    27: "getEventInformation",
    28: "writeGroup",
    29: "subscribeCOVPropertyMultiple",
}

UNCONFIRMED_SERVICES: dict[int, str] = {
    0: "iAm",
    1: "iHave",
    2: "unconfirmedCOVNotification",
    3: "unconfirmedEventNotification",
    4: "unconfirmedPrivateTransfer",
    5: "unconfirmedTextMessage",
    6: "timeSynchronization",
    7: "whoHas",
    8: "whoIs",
    9: "utcTimeSynchronization",
    10: "writeGroup",
    11: "unconfirmedCOVNotificationMultiple",
}


class PcapUnavailable(RuntimeError):
    """Raised when the ``dpkt`` package is not installed."""


@dataclass
class BACnetFrame:
    """Dissected representation of one BACnet/IP frame."""

    timestamp: float = 0.0
    src: str = ""
    dst: str = ""
    bvlc_function: str = ""
    bvlc_function_code: int = -1
    npdu_version: int = -1
    npdu_flags: int = 0
    hop_count: int = -1
    apdu_type: str = ""
    apdu_type_code: int = -1
    service: str = ""
    invoke_id: int = -1
    raw: bytes = field(default_factory=bytes)

    def __repr__(self) -> str:
        return (
            f"<BACnetFrame t={self.timestamp:.3f} {self.src}→{self.dst} "
            f"bvlc={self.bvlc_function!r} svc={self.service!r}>"
        )


class PcapReader:
    """Read and dissect BACnet/IP frames from a ``.pcap`` or ``.pcapng`` file.

    Parameters
    ----------
    path:
        Path to the capture file.
    strict:
        If ``True``, raise on malformed frames.  If ``False`` (default),
        skip them with a warning.
    """

    def __init__(self, path: str | Path, *, strict: bool = False) -> None:
        self._path = Path(path)
        self._strict = strict
        self._dpkt: Any = self._import_dpkt()

    @staticmethod
    def _import_dpkt() -> Any:
        try:
            import dpkt  # type: ignore[import-untyped]

            return dpkt
        except ImportError as exc:
            raise PcapUnavailable(
                "PCAP dissection requires the 'dpkt' package: pip install dpkt"
            ) from exc

    def __enter__(self) -> PcapReader:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def frames(self) -> Generator[BACnetFrame, None, None]:
        """Iterate over all dissected BACnet/IP frames in the capture."""
        dpkt = self._dpkt
        with open(self._path, "rb") as f:
            try:
                capture = dpkt.pcapng.Reader(f)
            except Exception:
                f.seek(0)
                capture = dpkt.pcap.Reader(f)

            for ts, raw_packet in capture:
                try:
                    frame = self._dissect(ts, raw_packet)
                    if frame is not None:
                        yield frame
                except Exception:
                    if self._strict:
                        raise

    def filter(
        self,
        *,
        service: str | None = None,
        bvlc_function: str | None = None,
        src: str | None = None,
        dst: str | None = None,
    ) -> Generator[BACnetFrame, None, None]:
        """Filtered iterator over :meth:`frames`.

        Parameters
        ----------
        service:
            Only yield frames with this service name (e.g. ``"readProperty"``).
        bvlc_function:
            Only yield frames with this BVLC function (e.g. ``"Original-Unicast-NPDU"``).
        src, dst:
            Filter by source or destination IP.
        """
        for frame in self.frames():
            if service and frame.service != service:
                continue
            if bvlc_function and frame.bvlc_function != bvlc_function:
                continue
            if src and frame.src != src:
                continue
            if dst and frame.dst != dst:
                continue
            yield frame

    def count(self) -> int:
        """Return the total number of BACnet/IP frames in the capture."""
        return sum(1 for _ in self.frames())

    # ------------------------------------------------------------------
    # Dissection logic
    # ------------------------------------------------------------------

    def _dissect(self, ts: float, raw: bytes) -> BACnetFrame | None:
        dpkt = self._dpkt
        eth = dpkt.ethernet.Ethernet(raw)
        if not isinstance(eth.data, dpkt.ip.IP):
            return None
        ip = eth.data
        if not isinstance(ip.data, dpkt.udp.UDP):
            return None
        udp = ip.data
        if udp.sport != BACNET_PORT and udp.dport != BACNET_PORT:
            return None

        payload = bytes(udp.data)
        frame = BACnetFrame(
            timestamp=ts,
            src=self._fmt_ip(bytes(ip.src)),
            dst=self._fmt_ip(bytes(ip.dst)),
            raw=payload,
        )
        self._parse_bvlc(payload, frame)
        return frame

    @staticmethod
    def _fmt_ip(addr: bytes) -> str:
        return ".".join(str(b) for b in addr)

    def _parse_bvlc(self, data: bytes, frame: BACnetFrame) -> None:
        """Parse BVLC header and delegate to NPDU parser."""
        if len(data) < 4:
            return
        bvlc_type, func, _length = struct.unpack(">BBH", data[:4])
        if bvlc_type != 0x81:
            return  # not BACnet/IP

        frame.bvlc_function_code = func
        frame.bvlc_function = BVLC_FUNCTIONS.get(func, f"0x{func:02X}")

        # Skip BVLC header (4 bytes); for Forwarded-NPDU skip extra 6 bytes
        npdu_offset = 4
        if func == 0x04:  # Forwarded-NPDU
            npdu_offset += 6

        if len(data) > npdu_offset:
            self._parse_npdu(data[npdu_offset:], frame)

    def _parse_npdu(self, data: bytes, frame: BACnetFrame) -> None:
        """Parse NPDU and delegate to APDU parser."""
        if len(data) < 2:
            return
        frame.npdu_version = data[0]
        frame.npdu_flags = data[1]

        # Calculate APDU start offset in NPDU payload
        offset = 2
        if frame.npdu_flags & 0x20:  # DNET present
            offset += 3
            if offset < len(data):
                dlen = data[offset - 1]
                offset += dlen  # DADR
        if frame.npdu_flags & 0x08:  # SNET present
            offset += 3
            if offset < len(data):
                slen = data[offset - 1]
                offset += slen  # SADR

        if frame.npdu_flags & 0x20:  # hop count present after DNET
            offset += 1  # hop count
            if offset <= len(data):
                frame.hop_count = data[offset - 1]

        # Network layer message — no APDU
        if frame.npdu_flags & 0x80:
            return

        if len(data) > offset:
            self._parse_apdu(data[offset:], frame)

    def _parse_apdu(self, data: bytes, frame: BACnetFrame) -> None:
        """Parse APDU type and service."""
        if not data:
            return
        pdu_type = (data[0] >> 4) & 0x0F
        frame.apdu_type_code = pdu_type
        frame.apdu_type = APDU_TYPES.get(pdu_type, f"type-{pdu_type}")

        if pdu_type == 0 and len(data) >= 4:  # Confirmed-Request
            frame.invoke_id = data[2]
            service = data[3]
            frame.service = CONFIRMED_SERVICES.get(service, f"confirmed-{service}")
        elif pdu_type == 1 and len(data) >= 2:  # Unconfirmed-Request
            service = data[1]
            frame.service = UNCONFIRMED_SERVICES.get(service, f"unconfirmed-{service}")
        elif pdu_type in {2, 3} and len(data) >= 3:  # ACK
            frame.invoke_id = data[1]
            service = data[2]
            frame.service = CONFIRMED_SERVICES.get(service, f"ack-service-{service}")
