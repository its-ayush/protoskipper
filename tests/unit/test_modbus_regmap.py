# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the Modbus CSV register-map importer (P1.A.2).

Tests cover: happy path, all data types, optional columns, skip-with-warning
policy for malformed rows, fatal errors, duplicate handling, encoding edge
cases.  No pymodbus or Qt dependency.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from protoskipper.builtin_drivers.modbus.regmap import load_csv, load_text
from protoskipper.core.driver import Access, DeviceRef
from protoskipper.core.errors import EncodingError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SENTINEL = "# protoskipper-modbus-map v1"
_HEADER = "object_id,data_type,access,label,unit,scale,offset,description"
_BYTE_ORDER_HEADER = _HEADER + ",byte_order,word_order"
_BITFIELD_HEADER = _BYTE_ORDER_HEADER + ",bit"


def _csv(*data_rows: str) -> str:
    """Assemble a minimal valid CSV with the given data rows."""
    return "\n".join([_SENTINEL, _HEADER, *data_rows]) + "\n"


_DEVICE = DeviceRef(protocol="modbus.tcp", address="127.0.0.1:502/unit=1")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_single_uint16_returns_one_object(self) -> None:
        text = _csv("holding:0,uint16,rw,Set Point,degC,0.1,0.0,Temperature set point")
        objs = load_text(text, device=_DEVICE)
        assert len(objs) == 1
        o = objs[0]
        assert o.object_id == "holding:0"
        assert o.data_type == "uint16"
        assert o.access == Access.READ_WRITE
        assert o.label == "Set Point"
        assert o.unit == "degC"
        assert o.metadata["scale"] == pytest.approx(0.1)
        assert o.metadata["offset"] == pytest.approx(0.0)

    def test_float32_with_explicit_byte_word_order(self) -> None:
        text = (
            f"{_SENTINEL}\n{_BYTE_ORDER_HEADER}\n"
            "holding:2:2,float32,ro,Active Energy,kWh,1.0,0.0,Accumulated energy,big,little\n"
        )
        objs = load_text(text, device=_DEVICE)
        assert len(objs) == 1
        o = objs[0]
        assert o.object_id == "holding:2:2"
        assert o.data_type == "float32"
        assert o.metadata["byte_order"] == "big"
        assert o.metadata["word_order"] == "little"

    def test_bitfield_row_sets_bit_in_metadata(self) -> None:
        full = (
            f"{_SENTINEL}\n{_BITFIELD_HEADER}\n"
            "holding:10,boolean,ro,Overvoltage,,1.0,0.0,VAC > 264 V,,,0\n"
        )
        objs = load_text(full, device=_DEVICE)
        assert len(objs) == 1
        o = objs[0]
        assert o.data_type == "boolean"
        assert o.metadata["bit"] == 0

    def test_multiple_bitfield_rows_from_same_register(self) -> None:
        rows = [
            "holding:10,boolean,ro,OV,,1.0,0.0,,,,0",
            "holding:10,boolean,ro,UV,,1.0,0.0,,,,1",
            "holding:10,boolean,ro,OC,,1.0,0.0,,,,2",
        ]
        text = f"{_SENTINEL}\n{_BITFIELD_HEADER}\n" + "\n".join(rows) + "\n"
        # Duplicate object_ids: last writer wins — 1 object remains (holding:10 written 3 times).
        objs = load_text(text, device=_DEVICE)
        # All three have the same object_id; last one (bit=2) survives.
        assert len(objs) == 1
        assert objs[0].metadata["bit"] == 2

    def test_order_preserved(self) -> None:
        text = _csv(
            "holding:0,uint16,ro,Reg0,,1.0,0.0,",
            "holding:1,uint16,ro,Reg1,,1.0,0.0,",
            "holding:2,uint16,ro,Reg2,,1.0,0.0,",
        )
        objs = load_text(text, device=_DEVICE)
        assert [o.object_id for o in objs] == ["holding:0", "holding:1", "holding:2"]

    def test_device_attached_to_all_objects(self) -> None:
        text = _csv("holding:0,uint16,ro,,,1.0,0.0,")
        objs = load_text(text, device=_DEVICE)
        assert objs[0].device is _DEVICE

    def test_no_device_uses_sentinel(self) -> None:
        from protoskipper.builtin_drivers.modbus.regmap import _SENTINEL_DEVICE

        text = _csv("holding:0,uint16,ro,,,1.0,0.0,")
        objs = load_text(text)
        assert objs[0].device is _SENTINEL_DEVICE

    def test_empty_label_becomes_none(self) -> None:
        text = _csv("holding:0,uint16,ro,,,1.0,0.0,")
        assert load_text(text, device=_DEVICE)[0].label is None

    def test_empty_unit_becomes_none(self) -> None:
        text = _csv("holding:0,uint16,ro,,,1.0,0.0,")
        assert load_text(text, device=_DEVICE)[0].unit is None

    def test_hex_address_parsed(self) -> None:
        text = _csv("holding:0x0A,uint16,ro,Reg10,,1.0,0.0,")
        objs = load_text(text, device=_DEVICE)
        assert objs[0].object_id == "holding:0x0A"

    def test_column_names_are_case_insensitive(self) -> None:
        header = "Object_ID,Data_Type,Access,Label,Unit,Scale,Offset,Description"
        text = f"{_SENTINEL}\n{header}\nholding:0,uint16,rw,Tag,V,1.0,0.0,\n"
        objs = load_text(text, device=_DEVICE)
        assert len(objs) == 1
        assert objs[0].object_id == "holding:0"

    def test_comment_rows_skipped(self) -> None:
        text = _csv(
            "# this is a comment",
            "holding:0,uint16,ro,,,1.0,0.0,",
        )
        objs = load_text(text, device=_DEVICE)
        assert len(objs) == 1

    def test_blank_rows_skipped(self) -> None:
        text = _csv(
            "holding:0,uint16,ro,,,1.0,0.0,",
            "",
            "holding:1,uint16,ro,,,1.0,0.0,",
        )
        objs = load_text(text, device=_DEVICE)
        assert len(objs) == 2

    def test_bom_stripped(self) -> None:
        """Excel-exported UTF-8 CSVs may start with a BOM."""
        import tempfile

        raw = ("\ufeff" + _csv("holding:0,uint16,ro,Tag,V,1.0,0.0,")).encode("utf-8")
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(raw)
            tmp_path = Path(f.name)
        try:
            objs = load_csv(tmp_path, device=_DEVICE)
            assert len(objs) == 1
        finally:
            tmp_path.unlink()

    def test_all_data_types_accepted(self) -> None:
        data_types = [
            "boolean",
            "uint16",
            "int16",
            "uint32",
            "int32",
            "float32",
            "uint64",
            "int64",
            "float64",
            "ascii",
            "utf16",
        ]
        rows = [f"holding:{i},{dt},ro,,,1.0,0.0," for i, dt in enumerate(data_types)]
        text = _csv(*rows)
        objs = load_text(text, device=_DEVICE)
        assert len(objs) == len(data_types)
        assert [o.data_type for o in objs] == data_types

    def test_default_byte_and_word_order(self) -> None:
        text = _csv("holding:0,uint16,ro,,,1.0,0.0,")
        o = load_text(text, device=_DEVICE)[0]
        assert o.metadata["byte_order"] == "big"
        assert o.metadata["word_order"] == "big"

    def test_description_stored_in_metadata(self) -> None:
        text = _csv("holding:0,uint16,ro,,,1.0,0.0,My description")
        o = load_text(text, device=_DEVICE)[0]
        assert o.metadata["description"] == "My description"

    def test_empty_description_not_in_metadata(self) -> None:
        text = _csv("holding:0,uint16,ro,,,1.0,0.0,")
        o = load_text(text, device=_DEVICE)[0]
        assert "description" not in o.metadata

    def test_load_csv_from_file(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "map.csv"
        csv_file.write_text(_csv("holding:0,uint16,ro,Reg,,1.0,0.0,"), encoding="utf-8")
        objs = load_csv(csv_file, device=_DEVICE)
        assert len(objs) == 1


# ---------------------------------------------------------------------------
# Duplicate handling
# ---------------------------------------------------------------------------


class TestDuplicates:
    def test_later_row_wins_on_duplicate_object_id(self) -> None:
        text = _csv(
            "holding:0,uint16,ro,First,,1.0,0.0,",
            "holding:0,int16,rw,Second,,2.0,1.0,",
        )
        objs = load_text(text, device=_DEVICE)
        assert len(objs) == 1
        assert objs[0].label == "Second"
        assert objs[0].data_type == "int16"


# ---------------------------------------------------------------------------
# Skip-with-warning policy (malformed rows)
# ---------------------------------------------------------------------------


class TestSkipWithWarning:
    def test_unknown_data_type_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        text = _csv(
            "holding:0,bad_type,ro,,,1.0,0.0,",
            "holding:1,uint16,ro,,,1.0,0.0,",
        )
        objs = load_text(text, device=_DEVICE)
        assert len(objs) == 1
        assert objs[0].object_id == "holding:1"
        assert "unknown data_type" in caplog.text.lower()

    def test_bad_access_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        text = _csv("holding:0,uint16,bad,,,1.0,0.0,")
        objs = load_text(text, device=_DEVICE)
        assert objs == []
        assert "access" in caplog.text.lower()

    def test_non_numeric_scale_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        text = _csv("holding:0,uint16,ro,,,abc,0.0,")
        objs = load_text(text, device=_DEVICE)
        assert objs == []
        assert "scale" in caplog.text.lower()

    def test_non_numeric_offset_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        text = _csv("holding:0,uint16,ro,,,1.0,xyz,")
        objs = load_text(text, device=_DEVICE)
        assert objs == []
        assert "offset" in caplog.text.lower()

    def test_bit_out_of_range_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        text = f"{_SENTINEL}\n{_BITFIELD_HEADER}\nholding:0,boolean,ro,,,1.0,0.0,,,,99\n"
        objs = load_text(text, device=_DEVICE)
        assert objs == []
        assert "bit" in caplog.text.lower()

    def test_bit_non_integer_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        text = f"{_SENTINEL}\n{_BITFIELD_HEADER}\nholding:0,boolean,ro,,,1.0,0.0,,,,foo\n"
        objs = load_text(text, device=_DEVICE)
        assert objs == []

    def test_bit_with_non_boolean_data_type_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        text = f"{_SENTINEL}\n{_BITFIELD_HEADER}\nholding:0,uint16,ro,,,1.0,0.0,,,,3\n"
        objs = load_text(text, device=_DEVICE)
        assert objs == []
        assert "boolean" in caplog.text.lower()

    def test_bad_object_id_table_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        text = _csv("badtable:0,uint16,ro,,,1.0,0.0,")
        objs = load_text(text, device=_DEVICE)
        assert objs == []

    def test_bad_object_id_format_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        text = _csv("holding,uint16,ro,,,1.0,0.0,")
        objs = load_text(text, device=_DEVICE)
        assert objs == []

    def test_invalid_byte_order_defaults_to_big(self, caplog: pytest.LogCaptureFixture) -> None:
        header = "object_id,data_type,access,label,unit,scale,offset,description,byte_order"
        text = f"{_SENTINEL}\n{header}\nholding:0,uint16,ro,,,1.0,0.0,,notvalid\n"
        objs = load_text(text, device=_DEVICE)
        assert len(objs) == 1
        assert objs[0].metadata["byte_order"] == "big"

    def test_empty_object_id_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        text = _csv(",uint16,ro,,,1.0,0.0,")
        objs = load_text(text, device=_DEVICE)
        assert objs == []

    def test_good_rows_load_despite_bad_rows(self) -> None:
        text = _csv(
            "holding:0,uint16,ro,Good0,,1.0,0.0,",
            "holding:1,badtype,ro,Bad,,1.0,0.0,",
            "holding:2,uint16,ro,Good2,,1.0,0.0,",
        )
        objs = load_text(text, device=_DEVICE)
        assert len(objs) == 2
        assert objs[0].object_id == "holding:0"
        assert objs[1].object_id == "holding:2"


# ---------------------------------------------------------------------------
# Fatal errors (EncodingError)
# ---------------------------------------------------------------------------


class TestFatalErrors:
    def test_missing_version_sentinel_raises(self) -> None:
        text = f"{_HEADER}\nholding:0,uint16,ro,,,1.0,0.0,\n"
        with pytest.raises(EncodingError, match="first non-blank line"):
            load_text(text)

    def test_empty_file_raises(self) -> None:
        with pytest.raises(EncodingError, match="empty"):
            load_text("   \n  \n")

    def test_missing_required_column_raises(self) -> None:
        # Drop 'offset' column
        partial_header = "object_id,data_type,access,label,unit,scale,description"
        text = f"{_SENTINEL}\n{partial_header}\nholding:0,uint16,ro,,,1.0,\n"
        with pytest.raises(EncodingError, match="offset"):
            load_text(text)

    def test_unreadable_file_raises(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does_not_exist.csv"
        with pytest.raises(EncodingError, match="Cannot read"):
            load_csv(nonexistent)
