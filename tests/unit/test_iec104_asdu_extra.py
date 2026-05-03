# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Tests for the additional IEC 104 ASDU types (M_BO, M_IT, C_SE_*, C_BO, C_CI)
and Select-Before-Operate support on existing command types.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from protoskipper.builtin_drivers.iec104.asdu import (
    COT,
    Asdu,
    BinaryCounter,
    InformationObject,
    Quality,
    TypeID,
    build_bitstring_command,
    build_counter_interrogation,
    build_double_command,
    build_set_point_float,
    build_set_point_normalised,
    build_set_point_scaled,
    build_single_command,
    decode_asdu,
    encode_asdu,
)
from protoskipper.core.errors import EncodingError

# ---------------------------------------------------------------------------
# BinaryCounter (BCR)
# ---------------------------------------------------------------------------


def test_bcr_round_trip_default():
    bcr = BinaryCounter(count=12345)
    assert BinaryCounter.from_bytes(bcr.to_bytes()) == bcr


def test_bcr_negative_count():
    bcr = BinaryCounter(count=-1, sequence=7)
    decoded = BinaryCounter.from_bytes(bcr.to_bytes())
    assert decoded.count == -1
    assert decoded.sequence == 7


def test_bcr_all_flags():
    bcr = BinaryCounter(count=0, sequence=31, carry=True, adjusted=True, invalid=True)
    decoded = BinaryCounter.from_bytes(bcr.to_bytes())
    assert decoded == bcr


def test_bcr_truncated():
    with pytest.raises(EncodingError):
        BinaryCounter.from_bytes(b"\x01\x02")


# ---------------------------------------------------------------------------
# M_BO_NA_1 / M_BO_TB_1 — bitstring 32 bit
# ---------------------------------------------------------------------------


def test_m_bo_na_1_round_trip():
    a = Asdu(
        type_id=TypeID.M_BO_NA_1,
        cot=COT.SPONT,
        ca=1,
        objects=[InformationObject(ioa=500, value=0xDEADBEEF, quality=Quality(invalid=True))],
    )
    decoded = decode_asdu(encode_asdu(a))
    assert decoded.type_id is TypeID.M_BO_NA_1
    assert decoded.objects[0].value == 0xDEADBEEF
    assert decoded.objects[0].quality is not None
    assert decoded.objects[0].quality.invalid is True


def test_m_bo_tb_1_round_trip():
    ts = datetime(2026, 5, 3, 12, 30, 45, 250000, tzinfo=timezone.utc)
    a = Asdu(
        type_id=TypeID.M_BO_TB_1,
        cot=COT.SPONT,
        ca=1,
        objects=[InformationObject(ioa=501, value=0xCAFEBABE, timestamp=ts)],
    )
    decoded = decode_asdu(encode_asdu(a))
    assert decoded.objects[0].value == 0xCAFEBABE
    assert decoded.objects[0].timestamp == ts


# ---------------------------------------------------------------------------
# M_IT_NA_1 / M_IT_TB_1 — integrated totals
# ---------------------------------------------------------------------------


def test_m_it_na_1_round_trip():
    bcr = BinaryCounter(count=1_000_000, sequence=3, carry=True)
    a = Asdu(
        type_id=TypeID.M_IT_NA_1,
        cot=COT.SPONT,
        ca=1,
        objects=[InformationObject(ioa=600, value=bcr)],
    )
    decoded = decode_asdu(encode_asdu(a))
    assert decoded.objects[0].value == bcr


def test_m_it_tb_1_round_trip():
    bcr = BinaryCounter(count=-12345, sequence=10, invalid=True)
    ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    a = Asdu(
        type_id=TypeID.M_IT_TB_1,
        cot=COT.SPONT,
        ca=1,
        objects=[InformationObject(ioa=601, value=bcr, timestamp=ts)],
    )
    decoded = decode_asdu(encode_asdu(a))
    assert decoded.objects[0].value == bcr
    assert decoded.objects[0].timestamp == ts


def test_m_it_accepts_int_value():
    """If caller passes a bare int, the encoder should wrap it in a BCR."""
    a = Asdu(
        type_id=TypeID.M_IT_NA_1,
        cot=COT.SPONT,
        ca=1,
        objects=[InformationObject(ioa=602, value=42)],
    )
    decoded = decode_asdu(encode_asdu(a))
    assert decoded.objects[0].value.count == 42


# ---------------------------------------------------------------------------
# C_SE_NA_1 / C_SE_NB_1 / C_SE_NC_1 — set point commands
# ---------------------------------------------------------------------------


def test_c_se_na_1_round_trip():
    body = build_set_point_normalised(ca=1, ioa=4001, value=-1234, ql=2)
    decoded = decode_asdu(body)
    assert decoded.type_id is TypeID.C_SE_NA_1
    assert decoded.objects[0].value == -1234
    assert decoded.objects[0].qu == 2
    assert decoded.objects[0].select is False


def test_c_se_nb_1_round_trip_with_select():
    body = build_set_point_scaled(ca=1, ioa=4002, value=20000, select=True)
    decoded = decode_asdu(body)
    assert decoded.type_id is TypeID.C_SE_NB_1
    assert decoded.objects[0].value == 20000
    assert decoded.objects[0].select is True


def test_c_se_nc_1_round_trip():
    body = build_set_point_float(ca=2, ioa=4003, value=3.14159, ql=5)
    decoded = decode_asdu(body)
    assert decoded.type_id is TypeID.C_SE_NC_1
    assert decoded.ca == 2
    assert decoded.objects[0].value == pytest.approx(3.14159, rel=1e-6)
    assert decoded.objects[0].qu == 5


# ---------------------------------------------------------------------------
# C_BO_NA_1 — bitstring command
# ---------------------------------------------------------------------------


def test_c_bo_na_1_round_trip():
    body = build_bitstring_command(ca=1, ioa=5000, value=0x12345678)
    decoded = decode_asdu(body)
    assert decoded.type_id is TypeID.C_BO_NA_1
    assert decoded.objects[0].value == 0x12345678


# ---------------------------------------------------------------------------
# C_CI_NA_1 — counter interrogation
# ---------------------------------------------------------------------------


def test_c_ci_na_1_default_general():
    body = build_counter_interrogation(ca=1)
    decoded = decode_asdu(body)
    assert decoded.type_id is TypeID.C_CI_NA_1
    # rqt=5 (general), frz=0 -> qcc = 5
    assert decoded.objects[0].value == 5
    assert decoded.objects[0].ioa == 0


def test_c_ci_na_1_freeze_with_reset():
    body = build_counter_interrogation(ca=1, rqt=2, frz=2)
    decoded = decode_asdu(body)
    # qcc = 2 | (2 << 6) = 0x82
    assert decoded.objects[0].value == 0x82


@pytest.mark.parametrize("rqt", [0, 6, -1, 99])
def test_c_ci_rejects_invalid_rqt(rqt):
    with pytest.raises(EncodingError):
        build_counter_interrogation(ca=1, rqt=rqt)


@pytest.mark.parametrize("frz", [-1, 4, 99])
def test_c_ci_rejects_invalid_frz(frz):
    with pytest.raises(EncodingError):
        build_counter_interrogation(ca=1, frz=frz)


# ---------------------------------------------------------------------------
# SBO support on existing command types
# ---------------------------------------------------------------------------


def test_single_command_select_bit_set():
    body = build_single_command(ca=1, ioa=100, on=True, select=True)
    decoded = decode_asdu(body)
    assert decoded.objects[0].value is True
    assert decoded.objects[0].select is True


def test_single_command_execute_clears_select():
    body = build_single_command(ca=1, ioa=100, on=True, select=False)
    decoded = decode_asdu(body)
    assert decoded.objects[0].select is False


def test_double_command_with_qu():
    body = build_double_command(ca=1, ioa=200, dcs=2, select=True, qu=3)
    decoded = decode_asdu(body)
    assert decoded.objects[0].value == 2
    assert decoded.objects[0].select is True
    assert decoded.objects[0].qu == 3


# ---------------------------------------------------------------------------
# Wire-format spot checks (catch byte-order regressions)
# ---------------------------------------------------------------------------


def test_c_se_nc_1_wire_format():
    """C_SE_NC_1 with value 1.0 at IOA=10, CA=1, ql=0, no select.

    Expected layout:
      type=50, vsq=01 (sq=0, n=1), cot=06, oa=00, ca=0001 (LE),
      IOA=10 00 00, float32(1.0)=00 00 80 3F, qos=00.
    """
    body = build_set_point_float(ca=1, ioa=10, value=1.0)
    expected = bytes(
        [0x32, 0x01, 0x06, 0x00, 0x01, 0x00, 0x0A, 0x00, 0x00, 0x00, 0x00, 0x80, 0x3F, 0x00]
    )
    assert body == expected


def test_c_bo_na_1_wire_format():
    """C_BO_NA_1 with value 0xAABBCCDD at IOA=20, CA=1.

    Expected: type=51, vsq=01, cot=06, oa=00, ca=0001, ioa=14 00 00, bsi=DD CC BB AA.
    """
    body = build_bitstring_command(ca=1, ioa=20, value=0xAABBCCDD)
    expected = bytes([0x33, 0x01, 0x06, 0x00, 0x01, 0x00, 0x14, 0x00, 0x00, 0xDD, 0xCC, 0xBB, 0xAA])
    assert body == expected
