# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Pure-Python PCAP / PCAPNG reader — P8.H.1.

Reads ``.pcap`` (libpcap legacy) and ``.pcapng`` (Block-based) files
without any external dependency.

Supported link types
--------------------
* ``DLT_EN10MB`` (1) — Ethernet frames (standard for IEC 61850 captures).

Usage::

    from protoskipper_iec61850.dissect.pcap import PcapReader, PcapRecord

    with PcapReader("capture.pcap") as rdr:
        for record in rdr:
            print(record.ts_us, len(record.data))
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class PcapRecord:
    """A single packet record from a PCAP or PCAPNG file.

    Attributes
    ----------
    ts_us:
        Timestamp in microseconds since Unix epoch.
    data:
        Raw captured bytes (may be truncated at *orig_len* if snaplen exceeded).
    orig_len:
        Original packet length before any capture truncation.
    iface_index:
        Interface index (PCAPNG only, always 0 for legacy PCAP).
    link_type:
        PCAP link-type code for the originating interface.
    """

    ts_us: int
    data: bytes
    orig_len: int
    iface_index: int = 0
    link_type: int = 1  # DLT_EN10MB default


# ---------------------------------------------------------------------------
# Legacy PCAP reader (.pcap)
# ---------------------------------------------------------------------------

_PCAP_MAGIC_LE: int = 0xA1B2C3D4
_PCAP_MAGIC_LE_NS: int = 0xA1B23C4D  # nanosecond variant
_PCAP_MAGIC_BE: int = 0xD4C3B2A1
_PCAP_MAGIC_BE_NS: int = 0x4D3CB2A1


class _PcapLegacyReader:
    """Reads libpcap legacy .pcap files."""

    def __init__(self, path: Path) -> None:
        self._f = path.open("rb")
        self._endian: str = "<"
        self._ns: bool = False
        self._link_type: int = 1
        self._read_global_header()

    def _read_global_header(self) -> None:
        header = self._f.read(24)
        if len(header) < 24:
            msg = "Truncated PCAP global header"
            raise ValueError(msg)
        magic = struct.unpack_from("<I", header, 0)[0]
        if magic in (_PCAP_MAGIC_LE, _PCAP_MAGIC_LE_NS):
            self._endian = "<"
            self._ns = magic == _PCAP_MAGIC_LE_NS
        elif magic in (_PCAP_MAGIC_BE, _PCAP_MAGIC_BE_NS):
            self._endian = ">"
            self._ns = magic == _PCAP_MAGIC_BE_NS
        else:
            msg = f"Not a valid PCAP file (magic=0x{magic:08x})"
            raise ValueError(msg)
        e = self._endian
        # version_major, version_minor, thiszone, sigfigs, snaplen, network
        self._link_type = struct.unpack_from(f"{e}I", header, 20)[0]

    def records(self) -> Iterator[PcapRecord]:
        e = self._endian
        while True:
            rec_hdr = self._f.read(16)
            if not rec_hdr:
                break
            if len(rec_hdr) < 16:
                break
            ts_sec, ts_usec, incl_len, orig_len = struct.unpack(f"{e}IIII", rec_hdr)
            data = self._f.read(incl_len)
            frac = ts_usec // 1000 if self._ns else ts_usec
            ts_us = ts_sec * 1_000_000 + frac
            yield PcapRecord(
                ts_us=ts_us,
                data=data,
                orig_len=orig_len,
                iface_index=0,
                link_type=self._link_type,
            )

    def close(self) -> None:
        self._f.close()


# ---------------------------------------------------------------------------
# PCAPNG reader (.pcapng)
# ---------------------------------------------------------------------------

_PCAPNG_SHB_TYPE: int = 0x0A0D0D0A
_PCAPNG_IDB_TYPE: int = 0x00000001
_PCAPNG_EPB_TYPE: int = 0x00000006
_PCAPNG_SPB_TYPE: int = 0x00000003
_PCAPNG_OPB_TYPE: int = 0x00000002

_PCAPNG_BYTE_ORDER_MAGIC: int = 0x1A2B3C4D


class _PcapNgReader:
    """Reads PCAPNG files (RFC 8126 / Wireshark spec)."""

    def __init__(self, path: Path) -> None:
        self._f = path.open("rb")
        self._endian: str = "<"
        self._ifaces: list[int] = []  # link_type per interface
        self._ts_resol: list[int] = []  # microseconds per ts unit (default 10^-6 = 1)
        self._read_shb()

    def _read_shb(self) -> None:
        """Read and validate the Section Header Block."""
        block_type = struct.unpack_from("<I", self._f.read(4))[0]
        if block_type != _PCAPNG_SHB_TYPE:
            msg = "Not a PCAPNG file (missing SHB)"
            raise ValueError(msg)
        block_len = struct.unpack_from("<I", self._f.read(4))[0]
        body = self._f.read(block_len - 12)  # 4 type + 4 len + 4 end_len
        self._f.read(4)  # trailing block_len

        bom = struct.unpack_from("<I", body, 0)[0]
        if bom == _PCAPNG_BYTE_ORDER_MAGIC:
            self._endian = "<"
        else:
            self._endian = ">"

    def _read_block_raw(self) -> tuple[int, bytes] | None:
        """Read next block; return (block_type, body_bytes) or None at EOF."""
        hdr = self._f.read(8)
        if len(hdr) < 8:
            return None
        e = self._endian
        block_type, block_len = struct.unpack(f"{e}II", hdr)
        body_len = block_len - 12  # type + len + end_len
        body = self._f.read(max(0, body_len))
        self._f.read(4)  # trailing block len
        return block_type, body

    def records(self) -> Iterator[PcapRecord]:
        e = self._endian
        while True:
            block = self._read_block_raw()
            if block is None:
                break
            block_type, body = block

            if block_type == _PCAPNG_IDB_TYPE:
                # Interface Description Block: link_type (2) + reserved (2) + snaplen (4)
                link_type = struct.unpack_from(f"{e}H", body, 0)[0] if len(body) >= 2 else 1
                self._ifaces.append(link_type)
                # ts_resol option not parsed — assume 10^-6
                self._ts_resol.append(1)

            elif block_type == _PCAPNG_EPB_TYPE:
                # Enhanced Packet Block
                if len(body) < 20:
                    continue
                iface_id, ts_high, ts_low, cap_len, orig_len_val = struct.unpack_from(
                    f"{e}IIIII", body, 0
                )
                ts_raw = (ts_high << 32) | ts_low
                resol = self._ts_resol[iface_id] if iface_id < len(self._ts_resol) else 1
                ts_us = ts_raw * resol
                data = body[20 : 20 + cap_len]
                link_type = self._ifaces[iface_id] if iface_id < len(self._ifaces) else 1
                yield PcapRecord(
                    ts_us=ts_us,
                    data=data,
                    orig_len=orig_len_val,
                    iface_index=iface_id,
                    link_type=link_type,
                )

            elif block_type == _PCAPNG_SPB_TYPE:
                # Simple Packet Block (no interface or timestamp — use 0)
                if len(body) < 4:
                    continue
                orig_len_val = struct.unpack_from(f"{e}I", body, 0)[0]
                link_type = self._ifaces[0] if self._ifaces else 1
                data = body[4:]
                yield PcapRecord(
                    ts_us=0,
                    data=data,
                    orig_len=orig_len_val,
                    iface_index=0,
                    link_type=link_type,
                )

            # SHB, OPB, and unknown blocks are silently skipped.

    def close(self) -> None:
        self._f.close()


# ---------------------------------------------------------------------------
# Public PcapReader
# ---------------------------------------------------------------------------


class PcapReader:
    """Context-manager PCAP/PCAPNG reader.

    Supports both legacy ``.pcap`` and ``.pcapng`` formats.  Auto-detects
    the format from the file magic.

    Usage::

        with PcapReader("capture.pcap") as rdr:
            for rec in rdr:
                ...
    """

    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            msg = f"File not found: {p}"
            raise FileNotFoundError(msg)
        # Peek at magic to decide which reader to use
        with p.open("rb") as f:
            magic4 = f.read(4)
        magic = struct.unpack_from("<I", magic4)[0] if len(magic4) >= 4 else 0
        if magic == _PCAPNG_SHB_TYPE:
            self._reader: _PcapLegacyReader | _PcapNgReader = _PcapNgReader(p)
        else:
            self._reader = _PcapLegacyReader(p)

    def __enter__(self) -> PcapReader:
        return self

    def __exit__(self, *args: object) -> None:
        self._reader.close()

    def __iter__(self) -> Iterator[PcapRecord]:
        return self._reader.records()
