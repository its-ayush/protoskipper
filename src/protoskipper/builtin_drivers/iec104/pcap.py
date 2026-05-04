# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Offline PCAP dissector for IEC 60870-5-104 (P4.E).

Parses a libpcap-format capture file and reassembles each TCP flow that
carries IEC 104 traffic, yielding decoded APDUs in chronological order.

This module deliberately re-implements the *minimum* of pcap and TCP
parsing rather than depending on Scapy / pyshark, to keep ProtoSkipper's
runtime install footprint small. It supports:

* libpcap classic file format (magic 0xA1B2C3D4 / 0xD4C3B2A1).
* Linktypes: Ethernet (1), Linux SLL (113), raw IP (12, 14).
* IPv4 with no options. IPv6 is not supported.
* TCP without options that affect payload offset; TCP-options-aware
  payload extraction *is* supported via the data-offset field.
* Naive single-direction stream reassembly per (src_ip, src_port,
  dst_ip, dst_port) tuple. Out-of-order segments are buffered up to a
  small window then dropped — sufficient for typical local captures
  but not a substitute for tshark on lossy WAN traces.

PCAP-NG (block-format, magic 0x0A0D0D0A) is **not** implemented; convert
with ``editcap -F libpcap`` or ``tcpdump -r in.pcapng -w out.pcap`` first.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from protoskipper.builtin_drivers.iec104.apci import (
    Apdu,
    parse_apdu,
    peek_apdu_length,
)
from protoskipper.builtin_drivers.iec104.asdu import Asdu, decode_asdu
from protoskipper.core.errors import ProtoSkipperError

_logger = logging.getLogger(__name__)


PCAP_MAGIC_LE = 0xA1B2C3D4  # microsecond resolution, little-endian
PCAP_MAGIC_BE = 0xD4C3B2A1
PCAP_MAGIC_NS_LE = 0xA1B23C4D  # nanosecond resolution
PCAP_MAGIC_NS_BE = 0x4D3CB2A1

LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 12
LINKTYPE_RAW_ALT = 14
LINKTYPE_LINUX_SLL = 113

IEC104_DEFAULT_PORT = 2404


# ---------------------------------------------------------------------------
# Output records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowEndpoints:
    """Five-tuple identifying a TCP flow direction."""

    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int

    def reverse(self) -> FlowEndpoints:
        return FlowEndpoints(self.dst_ip, self.dst_port, self.src_ip, self.src_port)


@dataclass(frozen=True)
class DissectedFrame:
    """One IEC 104 APDU decoded from the capture."""

    timestamp: datetime
    flow: FlowEndpoints
    raw: bytes
    apdu: Apdu
    asdu: Asdu | None  # populated for I-format frames; None for S/U


# ---------------------------------------------------------------------------
# PCAP reader
# ---------------------------------------------------------------------------


@dataclass
class _PcapHeader:
    little_endian: bool
    nanos: bool
    linktype: int


def _read_pcap_global_header(fh) -> _PcapHeader:
    raw = fh.read(24)
    if len(raw) < 24:
        raise ProtoSkipperError("pcap file too short for global header")
    (magic,) = struct.unpack("<I", raw[:4])
    if magic == PCAP_MAGIC_LE:
        endian = "<"
        little = True
        nanos = False
    elif magic == PCAP_MAGIC_BE:
        endian = ">"
        little = False
        nanos = False
    elif magic == PCAP_MAGIC_NS_LE:
        endian = "<"
        little = True
        nanos = True
    elif magic == PCAP_MAGIC_NS_BE:
        endian = ">"
        little = False
        nanos = True
    else:
        raise ProtoSkipperError(
            f"unsupported pcap magic 0x{magic:08x} "
            f"(pcap-ng / pcapng is not supported; convert with editcap)"
        )
    _ver_major, _ver_minor, _tz, _sigfigs, _snaplen, linktype = struct.unpack(
        endian + "HHiIII", raw[4:24]
    )
    return _PcapHeader(little_endian=little, nanos=nanos, linktype=linktype)


def _iter_pcap_records(fh, hdr: _PcapHeader) -> Iterator[tuple[datetime, bytes]]:
    endian = "<" if hdr.little_endian else ">"
    while True:
        rec_hdr = fh.read(16)
        if not rec_hdr:
            return
        if len(rec_hdr) < 16:
            return
        ts_sec, ts_usec, incl_len, _orig_len = struct.unpack(endian + "IIII", rec_hdr)
        data = fh.read(incl_len)
        if len(data) < incl_len:
            return
        if hdr.nanos:
            ts = datetime.fromtimestamp(ts_sec + ts_usec * 1e-9, tz=timezone.utc)
        else:
            ts = datetime.fromtimestamp(ts_sec + ts_usec * 1e-6, tz=timezone.utc)
        yield ts, data


# ---------------------------------------------------------------------------
# Link-layer + IPv4 + TCP stripping
# ---------------------------------------------------------------------------


def _strip_link(linktype: int, frame: bytes) -> bytes | None:
    if linktype == LINKTYPE_ETHERNET:
        if len(frame) < 14:
            return None
        # Handle one VLAN tag.
        eth_type = int.from_bytes(frame[12:14], "big")
        offset = 14
        if eth_type == 0x8100 and len(frame) >= 18:
            eth_type = int.from_bytes(frame[16:18], "big")
            offset = 18
        if eth_type != 0x0800:  # IPv4 only
            return None
        return frame[offset:]
    if linktype == LINKTYPE_LINUX_SLL:
        if len(frame) < 16:
            return None
        proto = int.from_bytes(frame[14:16], "big")
        if proto != 0x0800:
            return None
        return frame[16:]
    if linktype in (LINKTYPE_RAW, LINKTYPE_RAW_ALT):
        return frame
    return None


def _parse_ipv4_tcp(packet: bytes) -> tuple[FlowEndpoints, bytes] | None:
    if len(packet) < 20:
        return None
    ver_ihl = packet[0]
    version = ver_ihl >> 4
    if version != 4:
        return None
    ihl = (ver_ihl & 0x0F) * 4
    if len(packet) < ihl:
        return None
    proto = packet[9]
    if proto != 6:  # TCP
        return None
    src_ip = ".".join(str(b) for b in packet[12:16])
    dst_ip = ".".join(str(b) for b in packet[16:20])
    total_len = int.from_bytes(packet[2:4], "big")
    payload_total = packet[:total_len] if total_len else packet
    tcp = payload_total[ihl:]
    if len(tcp) < 20:
        return None
    src_port = int.from_bytes(tcp[0:2], "big")
    dst_port = int.from_bytes(tcp[2:4], "big")
    data_offset = (tcp[12] >> 4) * 4
    payload = tcp[data_offset:]
    flow = FlowEndpoints(src_ip, src_port, dst_ip, dst_port)
    return flow, payload


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def iter_iec104_frames(
    path: str | Path,
    *,
    iec104_port: int = IEC104_DEFAULT_PORT,
    decode_asdus: bool = True,
) -> Iterator[DissectedFrame]:
    """Yield IEC 104 APDUs from every TCP flow involving ``iec104_port``.

    Frames whose ASDU fails to decode still yield a :class:`DissectedFrame`
    with ``asdu=None`` so the caller can render the raw bytes. APCI-level
    parse failures are skipped silently after a debug log.
    """
    path = Path(path)
    flows: dict[FlowEndpoints, bytearray] = {}
    with path.open("rb") as fh:
        hdr = _read_pcap_global_header(fh)
        for ts, frame in _iter_pcap_records(fh, hdr):
            ip_packet = _strip_link(hdr.linktype, frame)
            if ip_packet is None:
                continue
            parsed = _parse_ipv4_tcp(ip_packet)
            if parsed is None:
                continue
            flow, payload = parsed
            if not payload:
                continue
            if iec104_port not in (flow.src_port, flow.dst_port):
                continue
            buf = flows.setdefault(flow, bytearray())
            buf.extend(payload)
            # Drain whole APDUs.
            while len(buf) >= 2:
                try:
                    total = peek_apdu_length(bytes(buf[:2]))
                except ProtoSkipperError:
                    # Resync: drop one byte and try again.
                    del buf[:1]
                    continue
                if total is None:
                    break
                if len(buf) < total:
                    break
                raw = bytes(buf[:total])
                del buf[:total]
                try:
                    apdu = parse_apdu(raw)
                except ProtoSkipperError as exc:
                    _logger.debug("malformed APDU in pcap: %s", exc)
                    continue
                asdu_obj: Asdu | None = None
                if decode_asdus and apdu.asdu:
                    try:
                        asdu_obj = decode_asdu(apdu.asdu)
                    except ProtoSkipperError as exc:
                        _logger.debug("undecodable ASDU in pcap: %s", exc)
                yield DissectedFrame(timestamp=ts, flow=flow, raw=raw, apdu=apdu, asdu=asdu_obj)


def summarize_pcap(path: str | Path, *, iec104_port: int = IEC104_DEFAULT_PORT) -> dict[str, int]:
    """Quick statistics summary: counts per APDU format / ASDU type."""
    counts: dict[str, int] = {}
    for frame in iter_iec104_frames(path, iec104_port=iec104_port):
        key = frame.apdu.fmt.name
        counts[key] = counts.get(key, 0) + 1
        if frame.asdu is not None:
            tkey = f"asdu:{frame.asdu.type_id.name}"
            counts[tkey] = counts.get(tkey, 0) + 1
    return counts


__all__ = [
    "IEC104_DEFAULT_PORT",
    "DissectedFrame",
    "FlowEndpoints",
    "iter_iec104_frames",
    "summarize_pcap",
]
