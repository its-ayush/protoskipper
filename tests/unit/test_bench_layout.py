# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for bench_layout — P8.E.1 / P8.E.2."""

from __future__ import annotations

import time

import pytest
from protoskipper_iec61850.bench_layout import (
    BenchLayout,
    BenchLayoutError,
    BenchStatusUpdater,
    IedHealth,
    IedTileSpec,
    load_bench_layout,
    save_bench_layout,
)

# ===========================================================================
# TestIedTileSpec
# ===========================================================================


class TestIedTileSpec:
    def test_roundtrip(self):
        spec = IedTileSpec(
            ied_name="IED001",
            x=2,
            y=3,
            colour="#aabbcc",
            expected_goose_refs=["IED001/LLN0$GO$gcb1"],
            notes="bay 3 switch",
        )
        d = spec.to_dict()
        spec2 = IedTileSpec.from_dict(d)
        assert spec2.ied_name == "IED001"
        assert spec2.x == 2
        assert spec2.y == 3
        assert spec2.colour == "#aabbcc"
        assert spec2.expected_goose_refs == ["IED001/LLN0$GO$gcb1"]
        assert spec2.notes == "bay 3 switch"

    def test_defaults(self):
        spec = IedTileSpec.from_dict({"ied_name": "X"})
        assert spec.x == 0
        assert spec.y == 0
        assert spec.colour == ""
        assert spec.expected_goose_refs == []
        assert spec.notes == ""


# ===========================================================================
# TestBenchLayout
# ===========================================================================


class TestBenchLayout:
    def test_empty_layout(self):
        layout = BenchLayout()
        assert layout.scd_path == ""
        assert layout.tiles == {}

    def test_to_dict_has_version(self):
        layout = BenchLayout()
        d = layout.to_dict()
        assert d["version"] == 1

    def test_from_dict_roundtrip(self):
        layout = BenchLayout(scd_path="/tmp/test.scd")
        layout.tiles["IED1"] = IedTileSpec("IED1", x=0, y=0)
        layout.tiles["IED2"] = IedTileSpec("IED2", x=1, y=0, colour="#123456")

        d = layout.to_dict()
        layout2 = BenchLayout.from_dict(d)
        assert layout2.scd_path == "/tmp/test.scd"
        assert "IED1" in layout2.tiles
        assert layout2.tiles["IED2"].colour == "#123456"


# ===========================================================================
# TestSaveLoadBenchLayout
# ===========================================================================


class TestSaveLoadBenchLayout:
    def test_save_and_load(self, tmp_path):
        layout = BenchLayout(scd_path="/proj/test.scd")
        layout.tiles["A"] = IedTileSpec("A", x=0, y=0, colour="#ff0000")
        layout.tiles["B"] = IedTileSpec("B", x=1, y=0, notes="secondary bay")

        path = tmp_path / "bench.bench-layout.json"
        save_bench_layout(layout, path)
        assert path.exists()

        loaded = load_bench_layout(path)
        assert loaded.scd_path == "/proj/test.scd"
        assert loaded.tiles["A"].colour == "#ff0000"
        assert loaded.tiles["B"].notes == "secondary bay"

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_bench_layout(tmp_path / "nonexistent.json")

    def test_load_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json}", encoding="utf-8")
        with pytest.raises(BenchLayoutError, match="Invalid JSON"):
            load_bench_layout(bad)

    def test_load_wrong_version_raises(self, tmp_path):
        bad = tmp_path / "old.json"
        bad.write_text('{"version": 99, "tiles": {}, "scd_path": ""}', encoding="utf-8")
        with pytest.raises(BenchLayoutError, match="schema version"):
            load_bench_layout(bad)

    def test_load_not_object_raises(self, tmp_path):
        bad = tmp_path / "arr.json"
        bad.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(BenchLayoutError, match="JSON object"):
            load_bench_layout(bad)


# ===========================================================================
# TestIedHealth
# ===========================================================================


class TestIedHealth:
    def test_offline_red(self):
        h = IedHealth(ied_name="X", mms_online=False)
        assert h.overall_colour == "#cc0000"

    def test_online_no_goose_green(self):
        h = IedHealth(ied_name="X", mms_online=True)
        assert h.overall_colour == "#00aa44"

    def test_online_goose_red(self):
        h = IedHealth(ied_name="X", mms_online=True, goose_red=1)
        assert h.overall_colour == "#cc6600"

    def test_online_goose_amber(self):
        h = IedHealth(ied_name="X", mms_online=True, goose_amber=2)
        assert h.overall_colour == "#ffcc00"


# ===========================================================================
# TestBenchStatusUpdater
# ===========================================================================


class TestBenchStatusUpdater:
    def _make_layout(self) -> BenchLayout:
        layout = BenchLayout()
        spec_a = IedTileSpec("IED_A", x=0, y=0, expected_goose_refs=["IED_A/LLN0$GO$gcb1"])
        spec_b = IedTileSpec("IED_B", x=1, y=0)
        layout.tiles["IED_A"] = spec_a
        layout.tiles["IED_B"] = spec_b
        return layout

    def test_online_all_green(self):
        now = time.time()
        results: list[IedHealth] = []
        layout = self._make_layout()

        updater = BenchStatusUpdater(
            layout=layout,
            get_mms_last_seen=lambda n: now - 1.0,
            get_goose_last_seen=lambda r: now - 0.5,
            get_sv_last_seen=lambda n: now - 0.1,
            on_health=results.append,
            mms_timeout_s=5.0,
            goose_max_time_s=4.0,
            goose_holdoff_s=8.0,
        )
        updater.poll()
        assert len(results) == 2
        ied_a = next(h for h in results if h.ied_name == "IED_A")
        assert ied_a.mms_online is True
        assert ied_a.goose_green == 1
        assert ied_a.goose_red == 0

    def test_mms_offline(self):
        results: list[IedHealth] = []
        layout = self._make_layout()

        updater = BenchStatusUpdater(
            layout=layout,
            get_mms_last_seen=lambda n: 0.0,  # never seen
            get_goose_last_seen=lambda r: 0.0,
            get_sv_last_seen=lambda n: 0.0,
            on_health=results.append,
        )
        updater.poll()
        for h in results:
            assert h.mms_online is False

    def test_goose_stale(self):
        now = time.time()
        results: list[IedHealth] = []
        layout = self._make_layout()

        updater = BenchStatusUpdater(
            layout=layout,
            get_mms_last_seen=lambda n: now - 1.0,
            get_goose_last_seen=lambda r: now - 100.0,  # very old
            get_sv_last_seen=lambda n: 0.0,
            on_health=results.append,
            goose_holdoff_s=8.0,
        )
        updater.poll()
        ied_a = next(h for h in results if h.ied_name == "IED_A")
        assert ied_a.goose_red == 1

    def test_goose_amber(self):
        now = time.time()
        results: list[IedHealth] = []
        layout = self._make_layout()

        updater = BenchStatusUpdater(
            layout=layout,
            get_mms_last_seen=lambda n: now - 1.0,
            get_goose_last_seen=lambda r: now - 5.0,  # between max_time=4 and holdoff=8
            get_sv_last_seen=lambda n: 0.0,
            on_health=results.append,
            goose_max_time_s=4.0,
            goose_holdoff_s=8.0,
        )
        updater.poll()
        ied_a = next(h for h in results if h.ied_name == "IED_A")
        assert ied_a.goose_amber == 1
        assert ied_a.goose_red == 0

    def test_sv_ok(self):
        now = time.time()
        results: list[IedHealth] = []
        layout = self._make_layout()

        updater = BenchStatusUpdater(
            layout=layout,
            get_mms_last_seen=lambda n: now - 1.0,
            get_goose_last_seen=lambda r: now - 0.1,
            get_sv_last_seen=lambda n: now - 0.5,
            on_health=results.append,
            sv_timeout_s=1.0,
        )
        updater.poll()
        for h in results:
            assert h.sv_ok is True

    def test_sv_timeout(self):
        now = time.time()
        results: list[IedHealth] = []
        layout = self._make_layout()

        updater = BenchStatusUpdater(
            layout=layout,
            get_mms_last_seen=lambda n: now - 1.0,
            get_goose_last_seen=lambda r: now - 0.1,
            get_sv_last_seen=lambda n: now - 10.0,  # stale
            on_health=results.append,
            sv_timeout_s=1.0,
        )
        updater.poll()
        for h in results:
            assert h.sv_ok is False

    def test_no_expected_goose_no_alarm(self):
        """IED_B has no expected GOOSE refs — should not show red."""
        now = time.time()
        results: list[IedHealth] = []
        layout = self._make_layout()

        updater = BenchStatusUpdater(
            layout=layout,
            get_mms_last_seen=lambda n: now - 1.0,
            get_goose_last_seen=lambda r: 0.0,
            get_sv_last_seen=lambda n: 0.0,
            on_health=results.append,
        )
        updater.poll()
        ied_b = next(h for h in results if h.ied_name == "IED_B")
        assert ied_b.goose_red == 0
        assert ied_b.goose_green == 0

    def test_poll_returns_all_healths(self):
        now = time.time()
        layout = self._make_layout()

        updater = BenchStatusUpdater(
            layout=layout,
            get_mms_last_seen=lambda n: now,
            get_goose_last_seen=lambda r: now,
            get_sv_last_seen=lambda n: now,
            on_health=lambda h: None,
        )
        results = updater.poll()
        assert len(results) == 2
        names = {h.ied_name for h in results}
        assert "IED_A" in names
        assert "IED_B" in names
