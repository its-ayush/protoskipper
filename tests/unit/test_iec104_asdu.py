# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the IEC 60870-5-104 ASDU codec (canonical type subset)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from protoskipper.builtin_drivers.iec104.asdu import (
    COT,
    Asdu,
    InformationObject,
    Quality,
    TypeID,
    build_clock_sync,
    build_double_command,
    build_general_interrogation,
    build_read_command,
    build_single_command,
    decode_asdu,
    decode_cp24time2a_full,
    decode_cp56time2a_full,
    decode_diq,
    decode_qds,
    decode_siq,
    encode_asdu,
    encode_cp24time2a,
    encode_cp56time2a,
    encode_diq,
    encode_qds,
    encode_siq,
)
from protoskipper.core.errors import EncodingError

# ---------------------------------------------------------------------------
# CP56Time2a / CP24Time2a
# ---------------------------------------------------------------------------


def test_cp56time2a_round_trip() -> None:
    ts = datetime(2024, 6, 15, 12, 34, 56, 789000, tzinfo=timezone.utc)
    encoded = encode_cp56time2a(ts)
    assert len(encoded) == 7
    decoded, invalid, dst = decode_cp56time2a_full(encoded)
    assert decoded == ts
    assert invalid is False
    assert dst is False


def test_cp56time2a_flags() -> None:
    ts = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    encoded = encode_cp56time2a(ts, invalid=True, dst=True)
    decoded, invalid, dst = decode_cp56time2a_full(encoded)
    assert decoded == ts
    assert invalid is True
    assert dst is True


def test_cp56time2a_year_range() -> None:
    with pytest.raises(EncodingError):
        encode_cp56time2a(datetime(1999, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(EncodingError):
        encode_cp56time2a(datetime(2128, 1, 1, tzinfo=timezone.utc))


def test_cp56time2a_naive_treated_as_utc() -> None:
    ts = datetime(2024, 1, 1, 12, 0, 0)  # naive
    encoded = encode_cp56time2a(ts)
    decoded, _, _ = decode_cp56time2a_full(encoded)
    assert decoded == ts.replace(tzinfo=timezone.utc)


def test_cp56time2a_truncated() -> None:
    with pytest.raises(EncodingError):
        decode_cp56time2a_full(b"\x00\x00\x00")


def test_cp24time2a_round_trip() -> None:
    ts = datetime(2024, 1, 1, 0, 30, 45, 250000, tzinfo=timezone.utc)
    encoded = encode_cp24time2a(ts)
    assert len(encoded) == 3
    ms, minute, invalid = decode_cp24time2a_full(encoded)
    assert ms == 45 * 1000 + 250
    assert minute == 30
    assert invalid is False


# ---------------------------------------------------------------------------
# Quality / SIQ / DIQ / QDS
# ---------------------------------------------------------------------------


def test_quality_byte_round_trip_all_flags() -> None:
    q = Quality(overflow=True, blocked=True, substituted=True, not_topical=True, invalid=True)
    b = q.to_byte()
    q2 = Quality.from_byte(b)
    assert q == q2


def test_siq_round_trip() -> None:
    for v in (False, True):
        for q in (Quality(), Quality(invalid=True), Quality(blocked=True, not_topical=True)):
            encoded = encode_siq(v, q)
            value, decoded_q = decode_siq(encoded)
            assert value is v
            assert decoded_q == q


def test_diq_round_trip() -> None:
    for dpi in range(4):
        encoded = encode_diq(dpi, Quality(substituted=True))
        v, q = decode_diq(encoded)
        assert v == dpi
        assert q.substituted is True


def test_diq_out_of_range() -> None:
    with pytest.raises(EncodingError):
        encode_diq(4)


def test_qds_round_trip() -> None:
    q = Quality(overflow=True, invalid=True)
    assert decode_qds(encode_qds(q)) == q


# ---------------------------------------------------------------------------
# ASDU round trips per supported type
# ---------------------------------------------------------------------------


def _round_trip(asdu: Asdu) -> Asdu:
    return decode_asdu(encode_asdu(asdu))


def test_asdu_m_sp_na_1() -> None:
    a = Asdu(
        type_id=TypeID.M_SP_NA_1,
        cot=COT.SPONT,
        ca=10,
        objects=[
            InformationObject(ioa=100, value=True, quality=Quality(invalid=True)),
            InformationObject(ioa=101, value=False, quality=Quality()),
        ],
    )
    out = _round_trip(a)
    assert out.type_id is TypeID.M_SP_NA_1
    assert out.cot is COT.SPONT
    assert out.ca == 10
    assert out.objects[0].ioa == 100
    assert out.objects[0].value is True
    assert out.objects[0].quality is not None
    assert out.objects[0].quality.invalid is True
    assert out.objects[1].value is False


def test_asdu_m_dp_na_1() -> None:
    a = Asdu(
        type_id=TypeID.M_DP_NA_1,
        cot=COT.SPONT,
        ca=1,
        objects=[InformationObject(ioa=200, value=2, quality=Quality(not_topical=True))],
    )
    out = _round_trip(a)
    assert out.objects[0].value == 2
    assert out.objects[0].quality is not None
    assert out.objects[0].quality.not_topical is True


def test_asdu_m_me_na_1_normalised() -> None:
    a = Asdu(
        type_id=TypeID.M_ME_NA_1,
        cot=COT.PER_CYC,
        ca=2,
        objects=[InformationObject(ioa=500, value=-1234, quality=Quality(overflow=True))],
    )
    out = _round_trip(a)
    assert out.objects[0].value == -1234


def test_asdu_m_me_nc_1_float() -> None:
    a = Asdu(
        type_id=TypeID.M_ME_NC_1,
        cot=COT.PER_CYC,
        ca=2,
        objects=[InformationObject(ioa=4001, value=230.75, quality=Quality())],
    )
    out = _round_trip(a)
    assert out.objects[0].value == pytest.approx(230.75)


def test_asdu_m_sp_tb_1_with_time() -> None:
    ts = datetime(2024, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    a = Asdu(
        type_id=TypeID.M_SP_TB_1,
        cot=COT.SPONT,
        ca=3,
        objects=[InformationObject(ioa=11, value=True, quality=Quality(), timestamp=ts)],
    )
    out = _round_trip(a)
    assert out.objects[0].value is True
    assert out.objects[0].timestamp == ts


def test_asdu_m_me_tf_1_float_with_time() -> None:
    ts = datetime(2024, 7, 1, 10, 0, 0, 500000, tzinfo=timezone.utc)
    a = Asdu(
        type_id=TypeID.M_ME_TF_1,
        cot=COT.PER_CYC,
        ca=3,
        objects=[InformationObject(ioa=4002, value=42.5, timestamp=ts, quality=Quality())],
    )
    out = _round_trip(a)
    assert out.objects[0].value == pytest.approx(42.5)
    assert out.objects[0].timestamp == ts


def test_asdu_c_sc_na_1_command() -> None:
    a = Asdu(
        type_id=TypeID.C_SC_NA_1,
        cot=COT.ACT,
        ca=4,
        objects=[InformationObject(ioa=2001, value=True)],
    )
    out = _round_trip(a)
    assert out.cot is COT.ACT
    assert out.objects[0].value is True


def test_asdu_c_dc_na_1_command() -> None:
    a = Asdu(
        type_id=TypeID.C_DC_NA_1,
        cot=COT.ACT,
        ca=4,
        objects=[InformationObject(ioa=2002, value=2)],
    )
    out = _round_trip(a)
    assert out.objects[0].value == 2


def test_asdu_c_ic_na_1_interrogation() -> None:
    a = Asdu(
        type_id=TypeID.C_IC_NA_1,
        cot=COT.ACT,
        ca=1,
        objects=[InformationObject(ioa=0, value=20)],
    )
    out = _round_trip(a)
    assert out.objects[0].value == 20


def test_asdu_c_rd_na_1_read() -> None:
    a = Asdu(
        type_id=TypeID.C_RD_NA_1,
        cot=COT.REQ,
        ca=1,
        objects=[InformationObject(ioa=4001, value=None)],
    )
    out = _round_trip(a)
    assert out.objects[0].ioa == 4001


def test_asdu_c_cs_na_1_clock_sync() -> None:
    ts = datetime(2024, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc)
    a = Asdu(
        type_id=TypeID.C_CS_NA_1,
        cot=COT.ACT,
        ca=1,
        objects=[InformationObject(ioa=0, value=ts, timestamp=ts)],
    )
    out = _round_trip(a)
    assert out.objects[0].timestamp == ts


# ---------------------------------------------------------------------------
# SQ=1 sequence form
# ---------------------------------------------------------------------------


def test_asdu_sq_form_consecutive_iao() -> None:
    a = Asdu(
        type_id=TypeID.M_ME_NC_1,
        cot=COT.INTROGEN,
        ca=1,
        sq=True,
        objects=[
            InformationObject(ioa=1000, value=1.0),
            InformationObject(ioa=1001, value=2.0),
            InformationObject(ioa=1002, value=3.0),
        ],
    )
    out = _round_trip(a)
    assert out.sq is True
    assert [o.ioa for o in out.objects] == [1000, 1001, 1002]
    assert [pytest.approx(o.value) for o in out.objects] == [1.0, 2.0, 3.0]


# ---------------------------------------------------------------------------
# COT flags (test, P/N)
# ---------------------------------------------------------------------------


def test_asdu_cot_flags_round_trip() -> None:
    a = Asdu(
        type_id=TypeID.C_SC_NA_1,
        cot=COT.ACT,
        ca=1,
        test=True,
        negative=True,
        originator=42,
        objects=[InformationObject(ioa=2001, value=True)],
    )
    out = _round_trip(a)
    assert out.test is True
    assert out.negative is True
    assert out.originator == 42


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_decode_unknown_type_id() -> None:
    raw = bytes([99, 0x01, 0x06, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00])
    with pytest.raises(EncodingError):
        decode_asdu(raw)


def test_decode_truncated_object() -> None:
    # M_ME_NC_1 needs 5 bytes of element + 3 IOA = 8 after header; we give 5.
    raw = bytes([13, 0x01, 0x03, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00])
    with pytest.raises(EncodingError):
        decode_asdu(raw)


def test_encode_empty_objects_rejected() -> None:
    a = Asdu(type_id=TypeID.M_SP_NA_1, cot=COT.SPONT, ca=1, objects=[])
    with pytest.raises(EncodingError):
        encode_asdu(a)


def test_encode_too_many_objects_rejected() -> None:
    a = Asdu(
        type_id=TypeID.M_SP_NA_1,
        cot=COT.SPONT,
        ca=1,
        objects=[InformationObject(ioa=i, value=False) for i in range(128)],
    )
    with pytest.raises(EncodingError):
        encode_asdu(a)


def test_ioa_out_of_range() -> None:
    a = Asdu(
        type_id=TypeID.M_SP_NA_1,
        cot=COT.SPONT,
        ca=1,
        objects=[InformationObject(ioa=0x1000000, value=False)],
    )
    with pytest.raises(EncodingError):
        encode_asdu(a)


# ---------------------------------------------------------------------------
# Convenience builders
# ---------------------------------------------------------------------------


def test_build_general_interrogation() -> None:
    body = build_general_interrogation(ca=1, qoi=20)
    out = decode_asdu(body)
    assert out.type_id is TypeID.C_IC_NA_1
    assert out.cot is COT.ACT
    assert out.objects[0].value == 20


def test_build_read_command() -> None:
    body = build_read_command(ca=2, ioa=4001)
    out = decode_asdu(body)
    assert out.type_id is TypeID.C_RD_NA_1
    assert out.objects[0].ioa == 4001


def test_build_single_command() -> None:
    body = build_single_command(ca=1, ioa=2001, on=True)
    out = decode_asdu(body)
    assert out.type_id is TypeID.C_SC_NA_1
    assert out.objects[0].value is True


def test_build_double_command_validation() -> None:
    with pytest.raises(EncodingError):
        build_double_command(ca=1, ioa=2002, dcs=0)
    with pytest.raises(EncodingError):
        build_double_command(ca=1, ioa=2002, dcs=3)


def test_build_clock_sync() -> None:
    ts = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    body = build_clock_sync(ca=1, ts=ts)
    out = decode_asdu(body)
    assert out.type_id is TypeID.C_CS_NA_1
    assert out.objects[0].timestamp == ts


# ---------------------------------------------------------------------------
# Wire-format spot checks (catch endianness / bit order regressions)
# ---------------------------------------------------------------------------


def test_wire_format_m_sp_na_1_one_object() -> None:
    a = Asdu(
        type_id=TypeID.M_SP_NA_1,
        cot=COT.SPONT,
        ca=1,
        objects=[InformationObject(ioa=100, value=True)],
    )
    raw = encode_asdu(a)
    # Type=1, VSQ=0x01 (SQ=0, n=1), COT=3 spont, OA=0, CA=1 LE, IOA=100 LE 24-bit, SIQ=0x01
    assert raw == bytes([0x01, 0x01, 0x03, 0x00, 0x01, 0x00, 100, 0x00, 0x00, 0x01])


def test_wire_format_clock_sync_fields() -> None:
    ts = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    raw = build_clock_sync(ca=1, ts=ts)
    # Header 6 bytes + IOA 3 bytes + 7 bytes CP56Time2a = 16 bytes
    assert len(raw) == 16
    assert raw[0] == TypeID.C_CS_NA_1
    assert raw[2] == COT.ACT
    # CP56Time2a year byte at end == 24
    assert raw[-1] == 24
