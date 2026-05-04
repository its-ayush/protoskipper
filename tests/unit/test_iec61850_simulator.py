# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for the IED simulator core (P8.G.1)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from protoskipper_iec61850.scl.model import (
    FCDA,
    IED,
    LN,
    AccessPoint,
    DataSet,
    LDevice,
    SclDocument,
)
from protoskipper_iec61850.simulator import (
    _MMS_BOOLEAN,
    _MMS_FLOAT,
    _MMS_VISIBLE_STRING,
    IedSimulator,
    SimulatorConfig,
    SimulatorError,
    _collect_points,
    _fcda_ref,
    _parse_ref,
    _python_to_mms,
)

# ---------------------------------------------------------------------------
# SCL fixtures
# ---------------------------------------------------------------------------


def _make_fcda(
    ld_inst: str,
    ln_class: str,
    ln_inst: str,
    do_name: str,
    da_name: str,
    fc: str,
    prefix: str = "",
) -> FCDA:
    return FCDA(
        ld_inst=ld_inst,
        prefix=prefix,
        ln_class=ln_class,
        ln_inst=ln_inst,
        do_name=do_name,
        da_name=da_name,
        fc=fc,
    )


def _make_scl(ieds: tuple[IED, ...]) -> SclDocument:
    return SclDocument(ieds=ieds)


def _simple_ied(name: str = "IED1") -> IED:
    """IED with one LD, one LN, one dataset with two FCDAs."""
    ds = DataSet(
        name="DS1",
        fcdas=(
            _make_fcda("LD0", "MMXU", "1", "PhV", "phsA.mag.f", "MX"),
            _make_fcda("LD0", "MMXU", "1", "A", "phsA.mag.f", "MX"),
        ),
    )
    ln = LN(prefix="", ln_class="MMXU", inst="1", ln_type="MMXU", datasets=(ds,))
    ln0 = LN(prefix="", ln_class="LLN0", inst="", ln_type="LLN0")
    ld = LDevice(inst="LD0", ln0=ln0, lns=(ln,))
    ap = AccessPoint(name="AP1", ldevices=(ld,))
    return IED(name=name, access_points=(ap,))


def _multi_dataset_ied() -> IED:
    """IED with two datasets that share one FCDA (deduplication test)."""
    shared = _make_fcda("LD0", "XCBR", "1", "Pos", "stVal", "ST")
    ds1 = DataSet(name="DS1", fcdas=(shared, _make_fcda("LD0", "XCBR", "1", "Pos", "q", "ST")))
    ds2 = DataSet(name="DS2", fcdas=(shared,))  # duplicate of shared
    ln = LN(prefix="", ln_class="XCBR", inst="1", ln_type="XCBR", datasets=(ds1, ds2))
    ld = LDevice(inst="LD0", lns=(ln,))
    ap = AccessPoint(name="AP1", ldevices=(ld,))
    return IED(name="IED2", access_points=(ap,))


# ---------------------------------------------------------------------------
# _fcda_ref
# ---------------------------------------------------------------------------


class TestFcdaRef:
    def test_full_ref(self) -> None:
        fcda = _make_fcda("LD0", "MMXU", "1", "PhV", "phsA.mag.f", "MX")
        assert _fcda_ref(fcda) == "LD0/MMXU1.PhV.phsA.mag.f[MX]"

    def test_no_da_name(self) -> None:
        fcda = _make_fcda("LD0", "XCBR", "1", "Pos", "", "ST")
        assert _fcda_ref(fcda) == "LD0/XCBR1.Pos[ST]"

    def test_with_prefix(self) -> None:
        fcda = _make_fcda("LD0", "MMXU", "1", "PhV", "mag.f", "MX", prefix="P")
        assert _fcda_ref(fcda) == "LD0/PMMXU1.PhV.mag.f[MX]"

    def test_no_fc(self) -> None:
        fcda = _make_fcda("LD0", "MMXU", "1", "PhV", "mag.f", "")
        assert _fcda_ref(fcda) == "LD0/MMXU1.PhV.mag.f"


# ---------------------------------------------------------------------------
# _parse_ref
# ---------------------------------------------------------------------------


class TestParseRef:
    def test_nested_da_parts(self) -> None:
        ld, ln, do, parts = _parse_ref("LD0/MMXU1.PhV.phsA.mag.f[MX]")
        assert ld == "LD0"
        assert ln == "MMXU1"
        assert do == "PhV"
        assert parts == ["phsA", "mag", "f"]

    def test_no_da(self) -> None:
        ld, ln, do, parts = _parse_ref("LD0/XCBR1.Pos[ST]")
        assert ld == "LD0"
        assert ln == "XCBR1"
        assert do == "Pos"
        assert parts == []

    def test_single_da(self) -> None:
        _, _, _, parts = _parse_ref("LD0/XCBR1.Pos.stVal[ST]")
        assert parts == ["stVal"]

    def test_no_fc_suffix(self) -> None:
        _ld, _ln, _do, parts = _parse_ref("LD0/MMXU1.PhV.phsA.mag.f")
        assert parts == ["phsA", "mag", "f"]


# ---------------------------------------------------------------------------
# _collect_points
# ---------------------------------------------------------------------------


class TestCollectPoints:
    def test_collects_all_fcdas(self) -> None:
        ied = _simple_ied()
        pts = _collect_points(ied)
        assert len(pts) == 2

    def test_deduplicates_same_fcda(self) -> None:
        ied = _multi_dataset_ied()
        pts = _collect_points(ied)
        # DS1 has 2, DS2 has 1 duplicate → total unique = 2
        assert len(pts) == 2

    def test_ref_format(self) -> None:
        ied = _simple_ied()
        pts = _collect_points(ied)
        assert "LD0/MMXU1.PhV.phsA.mag.f[MX]" in pts

    def test_mx_fc_maps_to_float(self) -> None:
        ied = _simple_ied()
        pts = _collect_points(ied)
        assert pts["LD0/MMXU1.PhV.phsA.mag.f[MX]"].mms_type == _MMS_FLOAT

    def test_st_fc_maps_to_boolean(self) -> None:
        ied = _multi_dataset_ied()
        pts = _collect_points(ied)
        assert pts["LD0/XCBR1.Pos.stVal[ST]"].mms_type == _MMS_BOOLEAN

    def test_empty_datasets_produces_empty_points(self) -> None:
        ln = LN(prefix="", ln_class="LLN0", inst="", ln_type="LLN0")
        ld = LDevice(inst="LD0", ln0=ln)
        ap = AccessPoint(name="AP1", ldevices=(ld,))
        ied = IED(name="EMPTY", access_points=(ap,))
        assert _collect_points(ied) == {}


# ---------------------------------------------------------------------------
# SimulatorConfig
# ---------------------------------------------------------------------------


class TestSimulatorConfig:
    def test_defaults(self) -> None:
        cfg = SimulatorConfig(ied_name="IED1")
        assert cfg.port == 102
        assert cfg.host == "0.0.0.0"

    def test_custom_port(self) -> None:
        cfg = SimulatorConfig(ied_name="IED1", port=10102)
        assert cfg.port == 10102


# ---------------------------------------------------------------------------
# IedSimulator construction
# ---------------------------------------------------------------------------


class TestIedSimulatorConstruction:
    def test_unknown_ied_raises(self) -> None:
        scl = _make_scl((_simple_ied("IED1"),))
        with pytest.raises(SimulatorError, match="not found in SCL"):
            IedSimulator(SimulatorConfig(ied_name="MISSING"), scl)

    def test_points_populated_from_scl(self) -> None:
        scl = _make_scl((_simple_ied(),))
        sim = IedSimulator(SimulatorConfig(ied_name="IED1"), scl)
        assert len(sim.points) == 2

    def test_not_started_initially(self) -> None:
        scl = _make_scl((_simple_ied(),))
        sim = IedSimulator(SimulatorConfig(ied_name="IED1"), scl)
        assert not sim.is_running


# ---------------------------------------------------------------------------
# read_da / update_da (without server)
# ---------------------------------------------------------------------------


class TestDataAccess:
    def setup_method(self) -> None:
        scl = _make_scl((_simple_ied(),))
        self.sim = IedSimulator(SimulatorConfig(ied_name="IED1"), scl)
        self.ref = "LD0/MMXU1.PhV.phsA.mag.f[MX]"

    def test_read_unknown_ref_raises(self) -> None:
        with pytest.raises(SimulatorError, match="Unknown DA reference"):
            self.sim.read_da("BOGUS/LN1.DO.DA[MX]")

    def test_initial_value_is_none(self) -> None:
        assert self.sim.read_da(self.ref) is None

    def test_update_then_read(self) -> None:
        self.sim.update_da(self.ref, 230.0)
        assert self.sim.read_da(self.ref) == 230.0

    def test_update_unknown_ref_raises(self) -> None:
        with pytest.raises(SimulatorError, match="Unknown DA reference"):
            self.sim.update_da("BOGUS/LN.DO.DA[MX]", 1.0)

    def test_multiple_updates(self) -> None:
        self.sim.update_da(self.ref, 100.0)
        self.sim.update_da(self.ref, 200.0)
        assert self.sim.read_da(self.ref) == 200.0


# ---------------------------------------------------------------------------
# _python_to_mms (with mocked pyiec61850)
# ---------------------------------------------------------------------------


class TestPythonToMms:
    def _mock_iec(self) -> MagicMock:
        m = MagicMock()
        m.MmsValue_newBoolean.return_value = object()
        m.MmsValue_newFloat.return_value = object()
        m.MmsValue_newInteger.return_value = object()
        m.MmsValue_newVisibleString.return_value = object()
        return m

    def test_float_type(self) -> None:
        _iec = self._mock_iec()
        _python_to_mms(_iec, 3.14, _MMS_FLOAT)
        _iec.MmsValue_newFloat.assert_called_once_with(3.14)

    def test_bool_type(self) -> None:
        _iec = self._mock_iec()
        _python_to_mms(_iec, True, _MMS_BOOLEAN)
        _iec.MmsValue_newBoolean.assert_called_once_with(True)

    def test_string_type(self) -> None:
        _iec = self._mock_iec()
        _python_to_mms(_iec, "hello", _MMS_VISIBLE_STRING)
        _iec.MmsValue_newVisibleString.assert_called_once_with("hello")

    def test_unknown_type_returns_none(self) -> None:
        _iec = self._mock_iec()
        result = _python_to_mms(_iec, 0, 99)
        assert result is None


# ---------------------------------------------------------------------------
# start / stop lifecycle (pyiec61850 mocked)
# ---------------------------------------------------------------------------


def _make_iec_mock() -> MagicMock:
    """Create a mock pyiec61850 module with realistic return values."""
    m = MagicMock(name="pyiec61850")
    m.IedModel_create.return_value = MagicMock(name="IedModel")
    m.IedDomain_create.return_value = MagicMock(name="IedDomain")
    m.LogicalNode_create.return_value = MagicMock(name="LogicalNode")
    m.DataObject_create.return_value = MagicMock(name="DataObject")
    m.DataAttribute_create.return_value = MagicMock(name="DataAttribute")
    m.IedServer_create.return_value = MagicMock(name="IedServer")
    m.MmsValue_newFloat.return_value = MagicMock(name="MmsValue")
    m.MmsValue_newBoolean.return_value = MagicMock(name="MmsValue")
    return m


class TestLifecycle:
    def setup_method(self) -> None:
        scl = _make_scl((_simple_ied(),))
        self.sim = IedSimulator(SimulatorConfig(ied_name="IED1", port=10102), scl)

    def test_start_missing_pyiec61850_raises(self) -> None:
        with (
            patch.dict(sys.modules, {"pyiec61850": None}),
            pytest.raises(  # type: ignore[dict-item]
                SimulatorError, match="pyiec61850 is required"
            ),
        ):
            self.sim.start()

    def test_start_creates_server(self) -> None:
        mock_iec = _make_iec_mock()
        with patch.dict(sys.modules, {"pyiec61850": mock_iec}):
            self.sim.start()
        mock_iec.IedServer_create.assert_called_once()
        mock_iec.IedServer_start.assert_called_once_with(
            mock_iec.IedServer_create.return_value, 10102
        )
        assert self.sim.is_running

    def test_start_twice_raises(self) -> None:
        mock_iec = _make_iec_mock()
        with patch.dict(sys.modules, {"pyiec61850": mock_iec}):
            self.sim.start()
            with pytest.raises(SimulatorError, match="already running"):
                self.sim.start()

    def test_stop_without_start_is_safe(self) -> None:
        self.sim.stop()  # must not raise
        assert not self.sim.is_running

    def test_stop_calls_destroy(self) -> None:
        mock_iec = _make_iec_mock()
        with patch.dict(sys.modules, {"pyiec61850": mock_iec}):
            self.sim.start()
            self.sim.stop()
        mock_iec.IedServer_stop.assert_called_once()
        mock_iec.IedServer_destroy.assert_called_once()
        mock_iec.IedModel_destroy.assert_called_once()
        assert not self.sim.is_running

    def test_double_stop_is_safe(self) -> None:
        mock_iec = _make_iec_mock()
        with patch.dict(sys.modules, {"pyiec61850": mock_iec}):
            self.sim.start()
            self.sim.stop()
            self.sim.stop()  # must not raise
        assert mock_iec.IedServer_stop.call_count == 1  # only called once

    def test_context_manager_stops_on_exit(self) -> None:
        mock_iec = _make_iec_mock()
        with patch.dict(sys.modules, {"pyiec61850": mock_iec}), self.sim:
            assert self.sim.is_running
        assert not self.sim.is_running
        mock_iec.IedServer_stop.assert_called_once()


# ---------------------------------------------------------------------------
# Server model construction (mocked pyiec61850)
# ---------------------------------------------------------------------------


class TestModelConstruction:
    def _start_sim(self, ied: IED) -> tuple[IedSimulator, MagicMock]:
        scl = _make_scl((ied,))
        sim = IedSimulator(SimulatorConfig(ied_name=ied.name), scl)
        mock_iec = _make_iec_mock()
        with patch.dict(sys.modules, {"pyiec61850": mock_iec}):
            sim.start()
        return sim, mock_iec

    def test_creates_domain_for_each_ld(self) -> None:
        _sim, mock_iec = self._start_sim(_simple_ied())
        # LD0 should be created once
        ld_calls = [c for c in mock_iec.IedDomain_create.call_args_list]
        ld_names = [c.args[1] for c in ld_calls]
        assert ld_names.count("LD0") == 1

    def test_creates_ln_for_each_unique_ln(self) -> None:
        _sim, mock_iec = self._start_sim(_simple_ied())
        ln_calls = [c.args[0] for c in mock_iec.LogicalNode_create.call_args_list]
        assert "MMXU1" in ln_calls

    def test_da_handle_set_after_start(self) -> None:
        sim, _mock_iec = self._start_sim(_simple_ied())
        for pt in sim.points.values():
            assert pt._da_handle is not None

    def test_da_handles_cleared_after_stop(self) -> None:
        scl = _make_scl((_simple_ied(),))
        sim = IedSimulator(SimulatorConfig(ied_name="IED1"), scl)
        mock_iec = _make_iec_mock()
        with patch.dict(sys.modules, {"pyiec61850": mock_iec}):
            sim.start()
            sim.stop()
        for pt in sim.points.values():
            assert pt._da_handle is None


# ---------------------------------------------------------------------------
# Live update (mocked pyiec61850)
# ---------------------------------------------------------------------------


class TestLiveUpdate:
    def test_update_da_pushes_to_server(self) -> None:
        scl = _make_scl((_simple_ied(),))
        sim = IedSimulator(SimulatorConfig(ied_name="IED1"), scl)
        ref = "LD0/MMXU1.PhV.phsA.mag.f[MX]"
        mock_iec = _make_iec_mock()
        with patch.dict(sys.modules, {"pyiec61850": mock_iec}):
            sim.start()
            sim.update_da(ref, 230.0)
        mock_iec.IedServer_lockDataModel.assert_called()
        mock_iec.IedServer_unlockDataModel.assert_called()
        mock_iec.IedServer_updateAttributeValue.assert_called()

    def test_update_before_start_cached_only(self) -> None:
        scl = _make_scl((_simple_ied(),))
        sim = IedSimulator(SimulatorConfig(ied_name="IED1"), scl)
        ref = "LD0/MMXU1.PhV.phsA.mag.f[MX]"
        sim.update_da(ref, 42.0)
        assert sim.read_da(ref) == 42.0
        # No server interaction
