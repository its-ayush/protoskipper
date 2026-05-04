# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the BACnet points-list parser.

Tests native CSV, EDE-3, and various edge cases (duplicate detection,
validation, empty input, state_text parsing, numeric EDE-2 objid).
"""

from __future__ import annotations

import json
import textwrap

import pytest

from protoskipper.builtin_drivers.bacnet.points import (
    PointDef,
    load_point_list,
    validate_point_list,
)
from protoskipper.core.driver import Access

# ---------------------------------------------------------------------------
# Native CSV
# ---------------------------------------------------------------------------


class TestNativeCsv:
    HEADER = "objid,object_name,description,units,device_id,cov_increment,access"

    def test_basic_parse(self) -> None:
        lines = [
            self.HEADER,
            "analog-value:1,SATempSensor,Supply Air Temp,degC,1234,0.5,rw",
            "binary-output:2,FanStart,Fan Start/Stop,,1234,,ro",
            "multi-state-value:3,Mode,Operating Mode,,1234,,ro",
        ]
        pts = load_point_list(lines)
        assert len(pts) == 3

        p0 = pts[0]
        assert p0.objid == "analog-value:1"
        assert p0.object_name == "SATempSensor"
        assert p0.description == "Supply Air Temp"
        assert p0.units == "degC"
        assert p0.device_id == 1234
        assert p0.cov_increment == pytest.approx(0.5)
        assert p0.access == Access.READ_WRITE

        p1 = pts[1]
        assert p1.objid == "binary-output:2"
        assert p1.access == Access.READ_ONLY
        assert p1.cov_increment is None

    def test_state_text_semicolon(self) -> None:
        lines = [
            "objid,object_name,state_text",
            "binary-value:1,StartStop,Off;On",
        ]
        pts = load_point_list(lines)
        assert pts[0].state_text == ("Off", "On")

    def test_blank_and_comment_lines_skipped(self) -> None:
        lines = [
            "# This is a comment",
            "objid,object_name",
            "",
            "analog-input:5,OutdoorAirTemp",
            "",
            "# Another comment",
        ]
        pts = load_point_list(lines)
        assert len(pts) == 1
        assert pts[0].objid == "analog-input:5"

    def test_no_device_id(self) -> None:
        lines = ["objid,object_name", "analog-value:7,TestPoint"]
        pts = load_point_list(lines)
        assert pts[0].device_id is None

    def test_cov_increment_none_when_empty(self) -> None:
        lines = ["objid,cov_increment", "analog-value:1,"]
        pts = load_point_list(lines)
        assert pts[0].cov_increment is None

    def test_object_type_and_instance_properties(self) -> None:
        lines = ["objid", "trend-log:42"]
        pts = load_point_list(lines)
        assert pts[0].object_type == "trend-log"
        assert pts[0].instance == 42

    def test_short_alias_in_objid(self) -> None:
        lines = ["objid,object_name", "AV:1,SpeedSetpoint"]
        pts = load_point_list(lines)
        assert pts[0].objid == "analog-value:1"

    def test_empty_input(self) -> None:
        pts = load_point_list([])
        assert pts == []

    def test_only_header_no_data(self) -> None:
        pts = load_point_list(["objid,object_name"])
        assert pts == []


# ---------------------------------------------------------------------------
# EDE-3 single-file format
# ---------------------------------------------------------------------------


class TestEde3:
    def test_ede3_basic(self) -> None:
        lines = [
            "# ede-version:3",
            "# project-name:TestBuilding",
            "keyname,object-identifier,instance-number,description,units",
            "SATempSensor,AV,1,Supply Air Temp,degC",
            "FanStart,BO,2,Fan Start/Stop,",
            "OperatingMode,MSV,3,Operating Mode,",
        ]
        pts = load_point_list(lines)
        assert len(pts) == 3
        assert pts[0].objid == "analog-value:1"
        assert pts[0].object_name == "SATempSensor"
        assert pts[1].objid == "binary-output:2"
        assert pts[2].objid == "multi-state-value:3"

    def test_ede3_with_cov(self) -> None:
        lines = [
            "# ede-version: 3",
            "keyname,object-identifier,instance-number,cov-increment,units",
            "RoomTemp,AI,10,0.25,degC",
        ]
        pts = load_point_list(lines)
        assert pts[0].cov_increment == pytest.approx(0.25)
        assert pts[0].units == "degC"

    def test_ede3_comment_lines_skipped(self) -> None:
        lines = [
            "# ede-version:3",
            "# author: test",
            "# date: 2026-01-01",
            "keyname,object-identifier,instance-number",
            "# this comment is mid-file",
            "Pump,BI,5",
        ]
        pts = load_point_list(lines)
        assert len(pts) == 1
        assert pts[0].objid == "binary-input:5"


# ---------------------------------------------------------------------------
# JSON format
# ---------------------------------------------------------------------------


class TestJsonFormat:
    def test_json_basic(self, tmp_path) -> None:
        data = [
            {"objid": "analog-value:1", "object_name": "SATempSensor", "units": "degC"},
            {"objid": "binary-output:2", "object_name": "FanStart"},
        ]
        json_file = tmp_path / "points.json"
        json_file.write_text(json.dumps(data))
        pts = load_point_list(json_file)
        assert len(pts) == 2
        assert pts[0].objid == "analog-value:1"
        assert pts[0].units == "degC"

    def test_json_not_a_list_raises(self, tmp_path) -> None:
        json_file = tmp_path / "bad.json"
        json_file.write_text('{"key": "value"}')
        from protoskipper.core.errors import EncodingError

        with pytest.raises(EncodingError, match="expected a JSON array"):
            load_point_list(json_file)


# ---------------------------------------------------------------------------
# File path loading
# ---------------------------------------------------------------------------


class TestFilePath:
    def test_csv_from_path(self, tmp_path) -> None:
        f = tmp_path / "points.csv"
        f.write_text("objid,object_name,device_id\nanalog-input:3,OutdoorTemp,5678\n")
        pts = load_point_list(f)
        assert len(pts) == 1
        assert pts[0].device_id == 5678

    def test_ede3_from_path(self, tmp_path) -> None:
        f = tmp_path / "export.ede"
        f.write_text(
            textwrap.dedent("""
                # ede-version:3
                keyname,object-identifier,instance-number,description
                RoomTemp,AV,1,Room Temperature
                Occupancy,BV,2,Occupancy Status
            """).lstrip()
        )
        pts = load_point_list(f)
        assert len(pts) == 2

    def test_ede2_bundle_dir(self, tmp_path) -> None:
        (tmp_path / "object_list.csv").write_text("objid,object_name\nanalog-value:1,TestAV\n")
        pts = load_point_list(tmp_path)
        assert len(pts) == 1
        assert pts[0].objid == "analog-value:1"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_no_issues_for_valid_list(self) -> None:
        pts = load_point_list(["objid,object_name", "analog-value:1,AV1", "analog-value:2,AV2"])
        issues = validate_point_list(pts)
        assert issues == []

    def test_duplicate_objid_warning(self) -> None:
        pts = load_point_list(["objid", "analog-value:1", "analog-value:1"])
        issues = validate_point_list(pts)
        dups = [i for i in issues if i.rule == "duplicate-objid"]
        assert len(dups) == 1
        assert dups[0].severity == "warning"

    def test_bad_objid_format(self) -> None:

        pts = [PointDef(device_id=None, objid="nocolon")]
        issues = validate_point_list(pts)
        fmt_issues = [i for i in issues if i.rule == "objid-format"]
        assert len(fmt_issues) == 1

    def test_instance_out_of_range(self) -> None:

        pts = [PointDef(device_id=None, objid="analog-value:9999999")]
        issues = validate_point_list(pts)
        range_issues = [i for i in issues if i.rule == "instance-range"]
        assert len(range_issues) == 1
        assert range_issues[0].severity == "warning"

    def test_empty_objid_error(self) -> None:

        pts = [PointDef(device_id=None, objid="")]
        issues = validate_point_list(pts)
        assert any(i.rule == "non-empty-objid" for i in issues)
