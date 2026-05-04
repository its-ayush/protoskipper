# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""PCAP dissector tests (P4.E).

Synthesises a tiny libpcap file in-test (Ethernet + IPv4 + TCP wrapping
several IEC 104 APDUs) and verifies the dissector reassembles them.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from protoskipper.builtin_drivers.iec104.apci import (
    UType,
    build_i_frame,
    build_s_frame,
    build_u_frame,
)
from protoskipper.builtin_drivers.iec104.asdu import (
    COT,
    Asdu,
    InformationObject,
    Quality,
    TypeID,
    encode_asdu,
)
from protoskipper.builtin_drivers.iec104.pcap import (
    iter_iec104_frames,
    summarize_pcap,
)
from protoskipper.core.errors import ProtoSkipperError

# ---------------------------------------------------------------------------
# pcap synthesis helpers
# ---------------------------------------------------------------------------


def _ipv4_checksum(header: bytes) -> int:
    s = 0
    for i in range(0, len(header), 2):
        s += int.from_bytes(header[i : i + 2], "big")
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def _build_tcp_segment(
    src_ip: str,
    dst_ip: str,
    src_port: int,
    dst_port: int,
    seq: int,
    payload: bytes,
) -> bytes:
    # Minimal TCP header: data offset 5, ACK flag set, no options.
    tcp_hdr = struct.pack(
        ">HHIIBBHHH",
        src_port,
        dst_port,
        seq,
        0,  # ack number
        (5 << 4),
        0x10,  # ACK
        0xFFFF,  # window
        0,  # checksum (we don't bother validating)
        0,  # urgent pointer
    )
    tcp_segment = tcp_hdr + payload

    ip_total_len = 20 + len(tcp_segment)
    src = bytes(int(x) for x in src_ip.split("."))
    dst = bytes(int(x) for x in dst_ip.split("."))
    ip_hdr_no_csum = struct.pack(
        ">BBHHHBBH4s4s",
        0x45,  # version 4, IHL 5
        0,
        ip_total_len,
        0x0001,  # ident
        0x4000,  # flags=DF
        64,
        6,  # TCP
        0,
        src,
        dst,
    )
    csum = _ipv4_checksum(ip_hdr_no_csum)
    ip_hdr = ip_hdr_no_csum[:10] + struct.pack(">H", csum) + ip_hdr_no_csum[12:]

    eth = (
        b"\x00\x11\x22\x33\x44\x55"  # dst mac
        b"\x66\x77\x88\x99\xaa\xbb"  # src mac
        b"\x08\x00"  # ethertype IPv4
    )
    return eth + ip_hdr + tcp_segment


def _write_pcap(
    path: Path,
    records: list[tuple[float, bytes]],
) -> None:
    with path.open("wb") as fh:
        # global header (microsecond, little-endian)
        fh.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for ts, data in records:
            ts_sec = int(ts)
            ts_usec = int((ts - ts_sec) * 1_000_000)
            fh.write(struct.pack("<IIII", ts_sec, ts_usec, len(data), len(data)))
            fh.write(data)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dissector_reassembles_basic_session(tmp_path: Path) -> None:
    body = encode_asdu(
        Asdu(
            type_id=TypeID.M_ME_NC_1,
            cot=COT.SPONT,
            ca=1,
            objects=[
                InformationObject(ioa=4001, value=230.5, quality=Quality()),
            ],
        )
    )
    apdus_master = [
        build_u_frame(UType.STARTDT_ACT),
        build_s_frame(0),
    ]
    apdus_slave = [
        build_u_frame(UType.STARTDT_CON),
        build_i_frame(0, 0, body),
    ]
    pcap_path = tmp_path / "session.pcap"
    seq_m = 1000
    seq_s = 2000
    records: list[tuple[float, bytes]] = []
    t = 1_700_000_000.0
    # master -> slave
    for apdu in apdus_master:
        records.append(
            (
                t,
                _build_tcp_segment("10.0.0.1", "10.0.0.2", 50_000, 2404, seq_m, apdu),
            )
        )
        seq_m += len(apdu)
        t += 0.001
    # slave -> master
    for apdu in apdus_slave:
        records.append(
            (
                t,
                _build_tcp_segment("10.0.0.2", "10.0.0.1", 2404, 50_000, seq_s, apdu),
            )
        )
        seq_s += len(apdu)
        t += 0.001
    _write_pcap(pcap_path, records)

    frames = list(iter_iec104_frames(pcap_path))
    assert len(frames) == 4
    # First master frame -> STARTDT_ACT (U)
    assert frames[0].apdu.fmt.name == "U"
    # Slave's I-frame should carry M_ME_NC_1 with the float we sent.
    iframes = [f for f in frames if f.apdu.fmt.name == "I"]
    assert len(iframes) == 1
    asdu = iframes[0].asdu
    assert asdu is not None
    assert asdu.type_id is TypeID.M_ME_NC_1
    assert asdu.objects[0].ioa == 4001
    assert asdu.objects[0].value == pytest.approx(230.5)


def test_dissector_handles_split_segments(tmp_path: Path) -> None:
    body = encode_asdu(
        Asdu(
            type_id=TypeID.C_IC_NA_1,
            cot=COT.ACT,
            ca=1,
            objects=[InformationObject(ioa=0, value=20)],
        )
    )
    apdu = build_i_frame(0, 0, body)
    half = len(apdu) // 2
    seg1 = apdu[:half]
    seg2 = apdu[half:]
    pcap_path = tmp_path / "split.pcap"
    records = [
        (
            1.0,
            _build_tcp_segment("10.0.0.1", "10.0.0.2", 50000, 2404, 1, seg1),
        ),
        (
            1.001,
            _build_tcp_segment("10.0.0.1", "10.0.0.2", 50000, 2404, 1 + half, seg2),
        ),
    ]
    _write_pcap(pcap_path, records)
    frames = list(iter_iec104_frames(pcap_path))
    assert len(frames) == 1
    assert frames[0].apdu.fmt.name == "I"
    assert frames[0].asdu is not None
    assert frames[0].asdu.type_id is TypeID.C_IC_NA_1


def test_dissector_summarises_counts(tmp_path: Path) -> None:
    body = encode_asdu(
        Asdu(
            type_id=TypeID.M_SP_NA_1,
            cot=COT.SPONT,
            ca=1,
            objects=[InformationObject(ioa=1001, value=True, quality=Quality())],
        )
    )
    pcap_path = tmp_path / "counts.pcap"
    records = [
        (
            1.0,
            _build_tcp_segment(
                "10.0.0.1", "10.0.0.2", 50000, 2404, 1, build_u_frame(UType.STARTDT_ACT)
            ),
        ),
        (
            2.0,
            _build_tcp_segment(
                "10.0.0.2", "10.0.0.1", 2404, 50000, 1, build_u_frame(UType.STARTDT_CON)
            ),
        ),
        (
            3.0,
            _build_tcp_segment("10.0.0.2", "10.0.0.1", 2404, 50000, 7, build_i_frame(0, 0, body)),
        ),
    ]
    _write_pcap(pcap_path, records)
    counts = summarize_pcap(pcap_path)
    assert counts["U"] == 2
    assert counts["I"] == 1
    assert counts["asdu:M_SP_NA_1"] == 1


def test_dissector_rejects_pcapng(tmp_path: Path) -> None:
    pcapng_path = tmp_path / "empty.pcapng"
    # pcapng SHB magic
    pcapng_path.write_bytes(b"\x0a\x0d\x0d\x0a" + b"\x00" * 24)
    with pytest.raises(ProtoSkipperError):
        list(iter_iec104_frames(pcapng_path))
