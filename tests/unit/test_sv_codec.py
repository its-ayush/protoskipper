# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for IEC 61850-9-2 SV decoder, encoder, and COMTRADE — P8.D.1/D.2/D.4/D.5."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from protoskipper_iec61850.sv.comtrade import ChannelSpec, ComtradeExporter, write_comtrade_ascii
from protoskipper_iec61850.sv.comtrade_reader import ComtradeReader
from protoskipper_iec61850.sv.decoder import (
    ETHERTYPE_SV,
    QUALITY_TEST,
    QUALITY_VALIDITY_GOOD,
    SvAsdu,
    SvChannel,
    _decode_ber_length,
    _decode_refrtm,
    _decode_sequence_of_data,
    _strip_ethernet_header,
    decode_frame,
)
from protoskipper_iec61850.sv.encoder import (
    SvFrameSpec,
    _encode_ber_length,
    encode_apdu,
    encode_frame,
)

# ---------------------------------------------------------------------------
# Helper: build a minimal valid SV Ethernet frame in bytes
# ---------------------------------------------------------------------------

_SRC_MAC = bytes.fromhex("aabbccddeeff")
_DST_MAC = bytes.fromhex("010ccd040000")


def _make_asdu(
    sv_id: str = "SVTEST001",
    smp_cnt: int = 0,
    conf_rev: int = 1,
    n_channels: int = 8,
) -> SvAsdu:
    channels = [SvChannel(index=i, value_raw=i * 100, quality_raw=0) for i in range(n_channels)]
    return SvAsdu(
        sv_id=sv_id,
        smp_cnt=smp_cnt,
        conf_rev=conf_rev,
        smp_synch=2,
        smp_rate=4000,
        smp_mod=None,
        refr_tm_ms=None,
        channels=channels,
    )


def _make_frame(
    vlan: bool = False,
    n_asdus: int = 1,
    sv_id: str = "SVTEST001",
) -> bytes:
    asdus = [_make_asdu(sv_id=sv_id, smp_cnt=i) for i in range(n_asdus)]
    spec = SvFrameSpec(
        asdus=asdus,
        app_id=0x4001,
        src_mac=_SRC_MAC,
        dst_mac=_DST_MAC,
        vlan_id=100 if vlan else None,
        vlan_priority=4 if vlan else 0,
    )
    return encode_frame(spec)


# ===========================================================================
# TestBerDecoder
# ===========================================================================


class TestBerDecoder:
    def test_short_form(self):
        assert _decode_ber_length(b"\x10", 0) == (16, 1)

    def test_long_form_1byte(self):
        assert _decode_ber_length(b"\x81\x80", 0) == (128, 2)

    def test_long_form_2bytes(self):
        assert _decode_ber_length(b"\x82\x01\x00", 0) == (256, 3)

    def test_truncated_raises(self):
        with pytest.raises(ValueError, match="truncated"):
            _decode_ber_length(b"", 0)

    def test_unsupported_form_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            _decode_ber_length(b"\x85" + b"\x00" * 5, 0)


# ===========================================================================
# TestBerEncoder
# ===========================================================================


class TestBerEncoder:
    def test_short_form(self):
        assert _encode_ber_length(127) == b"\x7f"

    def test_long_form_1byte(self):
        assert _encode_ber_length(128) == b"\x81\x80"

    def test_long_form_2bytes(self):
        assert _encode_ber_length(300) == b"\x82\x01\x2c"

    def test_roundtrip(self):
        for n in (0, 1, 127, 128, 255, 256, 1000, 65535):
            encoded = _encode_ber_length(n)
            length, _offset = _decode_ber_length(encoded, 0)
            assert length == n, f"roundtrip failed for {n}"

    def test_oversized_raises(self):
        with pytest.raises(ValueError, match="65535"):
            _encode_ber_length(65536)


# ===========================================================================
# TestDecodeSequenceOfData
# ===========================================================================


class TestDecodeSequenceOfData:
    def test_eight_channels(self):
        data = b""
        for i in range(8):
            data += struct.pack(">i", i * 1000)  # signed value
            data += struct.pack(">I", i * 10)  # quality
        channels = _decode_sequence_of_data(data)
        assert len(channels) == 8
        assert channels[0].value_raw == 0
        assert channels[3].value_raw == 3000
        assert channels[7].quality_raw == 70

    def test_negative_value(self):
        data = struct.pack(">i", -999) + struct.pack(">I", 0)
        channels = _decode_sequence_of_data(data)
        assert channels[0].value_raw == -999

    def test_empty_data(self):
        assert _decode_sequence_of_data(b"") == []

    def test_quality_validity(self):
        data = struct.pack(">i", 0) + struct.pack(">I", QUALITY_TEST)
        ch = _decode_sequence_of_data(data)[0]
        assert ch.test is True
        assert ch.validity == QUALITY_VALIDITY_GOOD


# ===========================================================================
# TestDecodeRefrtm
# ===========================================================================


class TestDecodeRefrtm:
    def test_zero_epoch(self):
        # IEC 61850 UtcTime: 4-byte seconds + 3-byte fraction + 1-byte time quality = 8 bytes
        data = struct.pack(">I", 0) + b"\x00\x00\x00\x0a"
        result = _decode_refrtm(data)
        assert result is not None
        assert result == pytest.approx(0.0, abs=1.0)

    def test_known_timestamp(self):
        # Unix epoch second 1_000_000, no fractional part
        data = struct.pack(">I", 1_000_000) + b"\x00\x00\x00\x0a"
        result = _decode_refrtm(data)
        assert result is not None
        assert result == pytest.approx(1_000_000 * 1000.0, rel=1e-6)

    def test_wrong_length_returns_none(self):
        assert _decode_refrtm(b"\x00" * 7) is None


# ===========================================================================
# TestStripEthernetHeader
# ===========================================================================


class TestStripEthernetHeader:
    def test_untagged(self):
        frame = _DST_MAC + _SRC_MAC + struct.pack(">H", ETHERTYPE_SV) + b"\x00" * 8
        eth = _strip_ethernet_header(frame)
        assert eth is not None
        assert eth.dst == _DST_MAC
        assert eth.src == _SRC_MAC
        assert eth.ethertype == ETHERTYPE_SV
        assert eth.vlan_id is None
        assert eth.payload_offset == 14

    def test_vlan_tagged(self):
        tci = (4 << 13) | 200  # priority=4, vid=200
        frame = (
            _DST_MAC
            + _SRC_MAC
            + struct.pack(">HH", 0x8100, tci)
            + struct.pack(">H", ETHERTYPE_SV)
            + b"\x00" * 8
        )
        eth = _strip_ethernet_header(frame)
        assert eth is not None
        assert eth.vlan_id == 200
        assert eth.vlan_priority == 4
        assert eth.payload_offset == 18

    def test_too_short_returns_none(self):
        assert _strip_ethernet_header(b"\x00" * 10) is None

    def test_wrong_ethertype_passes_through(self):
        frame = _DST_MAC + _SRC_MAC + struct.pack(">H", 0x0800) + b"\x00" * 8
        eth = _strip_ethernet_header(frame)
        assert eth is not None
        assert eth.ethertype == 0x0800


# ===========================================================================
# TestEncodeDecodeRoundtrip
# ===========================================================================


class TestEncodeDecodeRoundtrip:
    def test_single_asdu_untagged(self):
        raw = _make_frame(vlan=False)
        decoded = decode_frame(raw)
        assert decoded is not None
        assert decoded.app_id == 0x4001
        assert decoded.vlan_id is None
        assert len(decoded.asdus) == 1
        asdu = decoded.asdus[0]
        assert asdu.sv_id == "SVTEST001"
        assert asdu.smp_synch == 2
        assert asdu.smp_rate == 4000
        assert len(asdu.channels) == 8
        assert asdu.channels[3].value_raw == 300

    def test_single_asdu_vlan(self):
        raw = _make_frame(vlan=True)
        decoded = decode_frame(raw)
        assert decoded is not None
        assert decoded.vlan_id == 100
        assert decoded.vlan_priority == 4

    def test_multi_asdu_80_2(self):
        """80-2 style: 4 ASDUs per frame."""
        raw = _make_frame(n_asdus=4)
        decoded = decode_frame(raw)
        assert decoded is not None
        assert decoded.no_asdu == 4
        assert len(decoded.asdus) == 4
        for i, asdu in enumerate(decoded.asdus):
            assert asdu.smp_cnt == i

    def test_wrong_ethertype_returns_none(self):
        raw = _make_frame()
        # patch EtherType to 0x0800 (IPv4)
        patched = raw[:12] + struct.pack(">H", 0x0800) + raw[14:]
        assert decode_frame(patched) is None

    def test_too_short_returns_none(self):
        assert decode_frame(b"\x00" * 10) is None

    def test_source_mac_preserved(self):
        raw = _make_frame()
        decoded = decode_frame(raw)
        assert decoded is not None
        assert decoded.src_mac == _SRC_MAC

    def test_refrtm_roundtrip(self):
        asdu = _make_asdu()
        asdu.refr_tm_ms = 1_700_000_000_123.0
        spec = SvFrameSpec(asdus=[asdu], app_id=0x4001, src_mac=_SRC_MAC, dst_mac=_DST_MAC)
        raw = encode_frame(spec)
        decoded = decode_frame(raw)
        assert decoded is not None
        decoded_ms = decoded.asdus[0].refr_tm_ms
        assert decoded_ms is not None
        # Precision: UtcTime has ~60 ns resolution; allow 1 ms tolerance
        assert abs(decoded_ms - 1_700_000_000_123.0) < 1.0

    def test_negative_channel_values(self):
        asdu = _make_asdu()
        asdu.channels[0] = SvChannel(index=0, value_raw=-32767, quality_raw=0)
        spec = SvFrameSpec(asdus=[asdu], app_id=0x4001, src_mac=_SRC_MAC, dst_mac=_DST_MAC)
        raw = encode_frame(spec)
        decoded = decode_frame(raw)
        assert decoded is not None
        assert decoded.asdus[0].channels[0].value_raw == -32767

    def test_empty_channels(self):
        asdu = _make_asdu(n_channels=0)
        spec = SvFrameSpec(asdus=[asdu], app_id=0x4001, src_mac=_SRC_MAC, dst_mac=_DST_MAC)
        raw = encode_frame(spec)
        decoded = decode_frame(raw)
        assert decoded is not None
        assert decoded.asdus[0].channels == []


# ===========================================================================
# TestEncodeApdu
# ===========================================================================


class TestEncodeApdu:
    def test_apdu_starts_with_appid(self):
        asdus = [_make_asdu()]
        apdu = encode_apdu(asdus, app_id=0x4002)
        assert struct.unpack_from(">H", apdu, 0)[0] == 0x4002

    def test_apdu_length_field(self):
        asdus = [_make_asdu()]
        apdu = encode_apdu(asdus)
        length = struct.unpack_from(">H", apdu, 2)[0]
        assert length == len(apdu)

    def test_reserved_bytes_zero(self):
        asdus = [_make_asdu()]
        apdu = encode_apdu(asdus)
        assert apdu[4:8] == b"\x00\x00\x00\x00"


# ===========================================================================
# TestComtradeExporter
# ===========================================================================


class TestComtradeExporter:
    def _make_exporter(self, n_ch: int = 4) -> ComtradeExporter:
        channels = [
            ChannelSpec(f"ch{i}", unit="A", primary=100.0, secondary=1.0) for i in range(n_ch)
        ]
        return ComtradeExporter(
            station_name="TESTSTATION",
            rec_dev_id="DEV01",
            frequency=50.0,
            channels=channels,
        )

    def test_add_sample_increments_count(self):
        exp = self._make_exporter(4)
        exp.add_sample(0, [1, 2, 3, 4])
        exp.add_sample(250, [5, 6, 7, 8])
        assert exp.sample_count == 2

    def test_wrong_channel_count_raises(self):
        exp = self._make_exporter(4)
        with pytest.raises(ValueError, match="Expected 4"):
            exp.add_sample(0, [1, 2, 3])

    def test_write_binary32(self, tmp_path):
        exp = self._make_exporter(4)
        for i in range(10):
            exp.add_sample(i * 250, [i * 10, i * 20, i * 30, i * 40])
        exp.write(tmp_path / "capture")
        assert (tmp_path / "capture.cfg").exists()
        assert (tmp_path / "capture.dat").exists()

    def test_cfg_contains_station_name(self, tmp_path):
        exp = self._make_exporter(4)
        exp.add_sample(0, [0, 0, 0, 0])
        exp.write(tmp_path / "cap")
        cfg = (tmp_path / "cap.cfg").read_text()
        assert "TESTSTATION" in cfg

    def test_cfg_format_field(self, tmp_path):
        exp = self._make_exporter(2)
        exp.add_sample(0, [0, 0])
        exp.write(tmp_path / "cap")
        cfg = (tmp_path / "cap.cfg").read_text()
        assert "BINARY32" in cfg

    def test_write_ascii(self, tmp_path):
        exp = self._make_exporter(2)
        for i in range(5):
            exp.add_sample(i * 1000, [i, i * 2])
        write_comtrade_ascii(exp, tmp_path / "asc")
        cfg = (tmp_path / "asc.cfg").read_text()
        assert "ASCII" in cfg
        dat = (tmp_path / "asc.dat").read_text()
        assert "1,0," in dat


# ===========================================================================
# TestComtradeReader
# ===========================================================================


class TestComtradeReader:
    def _write_and_read(self, tmp_path: Path, ascii: bool = False) -> ComtradeReader:
        exp = ComtradeExporter(
            station_name="READTEST",
            rec_dev_id="REC01",
            frequency=50.0,
            channels=[
                ChannelSpec("iA", unit="A", scale=0.001, primary=100.0, secondary=1.0),
                ChannelSpec("uA", unit="V", scale=0.01, primary=110000.0, secondary=110.0),
            ],
        )
        for i in range(8):
            exp.add_sample(i * 2500, [i * 1000, i * 500])

        base = tmp_path / "test"
        if ascii:
            write_comtrade_ascii(exp, base)
        else:
            exp.write(base)
        return ComtradeReader.from_cfg(base.with_suffix(".cfg"))

    def test_station_name(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        assert reader.station_name == "READTEST"

    def test_frequency(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        assert reader.frequency == pytest.approx(50.0)

    def test_channel_count(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        assert len(reader.channels) == 2

    def test_channel_names(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        assert reader.channels[0].name == "iA"
        assert reader.channels[1].name == "uA"

    def test_sample_count(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        samples = reader.all_samples()
        assert len(samples) == 8

    def test_raw_values_match(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        samples = reader.all_samples()
        # Sample 0: values [0, 0]; sample 3: values [3000, 1500]
        assert samples[3].values_raw == [3000, 1500]

    def test_scaled_values(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        samples = reader.all_samples()
        # scale=0.001 for iA; raw=3000 → 3.0
        assert samples[3].values[0] == pytest.approx(3.0, abs=1e-9)

    def test_timestamp_us(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        samples = reader.all_samples()
        assert samples[0].timestamp_us == 0
        assert samples[1].timestamp_us == 2500

    def test_ascii_roundtrip(self, tmp_path):
        reader = self._write_and_read(tmp_path, ascii=True)
        samples = reader.all_samples()
        assert len(samples) == 8
        assert samples[4].values_raw == [4000, 2000]

    def test_sample_numbers_1based(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        samples = reader.all_samples()
        assert samples[0].n == 1
        assert samples[7].n == 8

    def test_revision_2013(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        assert reader.revision == "2013"

    def test_dat_format_binary32(self, tmp_path):
        reader = self._write_and_read(tmp_path)
        assert reader.dat_format == "BINARY32"

    def test_dat_format_ascii(self, tmp_path):
        reader = self._write_and_read(tmp_path, ascii=True)
        assert reader.dat_format == "ASCII"
