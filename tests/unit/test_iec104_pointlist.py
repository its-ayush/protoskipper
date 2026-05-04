# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the IEC 104 point-list CSV loader."""

from __future__ import annotations

import pytest

from protoskipper.builtin_drivers.iec104.asdu import TypeID
from protoskipper.builtin_drivers.iec104.pointlist import (
    PointDef,
    load_point_list,
    parse_access,
    parse_type,
)
from protoskipper.core.driver import Access
from protoskipper.core.errors import EncodingError


def test_parse_type_by_mnemonic() -> None:
    assert parse_type("M_ME_NC_1") is TypeID.M_ME_NC_1
    assert parse_type("c_sc_na_1") is TypeID.C_SC_NA_1


def test_parse_type_by_number() -> None:
    assert parse_type("13") is TypeID.M_ME_NC_1


def test_parse_type_unknown() -> None:
    with pytest.raises(EncodingError):
        parse_type("FOO")


def test_parse_access() -> None:
    assert parse_access("ro") is Access.READ_ONLY
    assert parse_access("RW") is Access.READ_WRITE
    assert parse_access("write-only") is Access.WRITE_ONLY


def test_parse_access_unknown() -> None:
    with pytest.raises(EncodingError):
        parse_access("xyz")


def test_load_minimal_csv() -> None:
    csv_text = "ioa,type,label\n100,M_SP_NA_1,Pump_Run\n4001,M_ME_NC_1,Voltage_Phase_A\n"
    pts = load_point_list(csv_text)
    assert len(pts) == 2
    assert pts[0] == PointDef(
        ioa=100,
        type_id=TypeID.M_SP_NA_1,
        label="Pump_Run",
    )
    assert pts[1].label == "Voltage_Phase_A"
    assert pts[1].type_id is TypeID.M_ME_NC_1


def test_load_full_csv() -> None:
    csv_text = (
        "ioa,type,label,unit,access,description,ca\n"
        "2001,C_SC_NA_1,Breaker_Open_Cmd,,rw,Open command,1\n"
        "4001,M_ME_NC_1,Voltage_A,V,ro,Phase-A voltage,1\n"
    )
    pts = load_point_list(csv_text)
    assert pts[0].access is Access.READ_WRITE
    assert pts[0].ca == 1
    assert pts[1].unit == "V"
    assert pts[1].description == "Phase-A voltage"


def test_load_skips_blank_and_comment_lines() -> None:
    csv_text = "ioa,type,label\n\n# comment\n100,M_SP_NA_1,Pump\n"
    pts = load_point_list(csv_text)
    assert len(pts) == 1


def test_load_missing_required_column() -> None:
    csv_text = "ioa,label\n100,Pump\n"
    with pytest.raises(EncodingError):
        load_point_list(csv_text)


def test_load_duplicate_ioa() -> None:
    csv_text = "ioa,type,label\n100,M_SP_NA_1,A\n100,M_SP_NA_1,B\n"
    with pytest.raises(EncodingError):
        load_point_list(csv_text)


def test_load_empty_label_rejected() -> None:
    csv_text = "ioa,type,label\n100,M_SP_NA_1,\n"
    with pytest.raises(EncodingError):
        load_point_list(csv_text)


def test_load_empty_csv_rejected() -> None:
    with pytest.raises(EncodingError):
        load_point_list("")


def test_load_from_path(tmp_path) -> None:
    csv_path = tmp_path / "points.csv"
    csv_path.write_text("ioa,type,label\n100,M_SP_NA_1,Pump\n", encoding="utf-8")
    pts = load_point_list(csv_path)
    assert len(pts) == 1
    assert pts[0].label == "Pump"
