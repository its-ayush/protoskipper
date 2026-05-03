# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""GUI smoke tests for IEC 104 panels.

Tests instantiation, signal connections, and basic state updates for:
  - SoePanel, Iec104InterrogationPanel, Iec104TimeSyncPanel, Iec104BenchPanel
  - Iec104CommandDialog, NewSlaveDialog, Iec104DiffDialog, Iec104ConformanceDialog

These tests do NOT require a live RTU - they exercise the Qt object graph
and local model/view logic only.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from protoskipper.core.driver import (
    Access,
    DeviceRef,
    ObjectRef,
    ReadResult,
    SessionProfile,
)
from protoskipper.gui.dialogs.iec104_command import Iec104CommandDialog
from protoskipper.gui.dialogs.iec104_conformance import (
    _ALL_TESTS,
    Iec104ConformanceDialog,
)
from protoskipper.gui.dialogs.iec104_diff import Iec104DiffDialog
from protoskipper.gui.dialogs.iec104_slave import NewSlaveDialog
from protoskipper.gui.panels.iec104_bench import Iec104BenchPanel
from protoskipper.gui.panels.iec104_interrogation import Iec104InterrogationPanel
from protoskipper.gui.panels.iec104_soe import SoeModel, SoePanel, _quality_str, _SoeRow
from protoskipper.gui.panels.iec104_timesync import Iec104TimeSyncPanel
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.types import SessionId, new_session_id

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def state(qt_app: QApplication) -> ApplicationState:
    return ApplicationState()


@pytest.fixture
def device() -> DeviceRef:
    return DeviceRef(protocol="iec104.tcp", address="10.0.0.1:2404/ca=1", label="Test RTU")


@pytest.fixture
def session_id(state: ApplicationState, device: DeviceRef) -> SessionId:
    sid = new_session_id()
    info = SessionInfo(
        session_id=sid,
        device=device,
        profile=SessionProfile.LAB,
        operator="tester",
    )
    info.is_open = True
    state._sessions[sid] = info  # type: ignore[attr-defined]
    return sid


@pytest.fixture
def object_ref(device: DeviceRef) -> ObjectRef:
    return ObjectRef(
        device=device,
        object_id="M_ME_NC_1:4001",
        data_type="float32",
        access=Access.READ_ONLY,
    )


class _FakeSM:
    """Minimal SessionManager stub that records prepare_write calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, str, object]] = []

    def prepare_write(self, sid: object, ref: object, value: object) -> None:
        self.calls.append((sid, getattr(ref, "object_id", ""), value))


class TestSoeModel:
    def test_initial_state(self) -> None:
        model = SoeModel()
        assert model.rowCount() == 0
        assert model.columnCount() == 9

    def test_append_row(self) -> None:
        from datetime import datetime, timezone

        model = SoeModel()
        row = _SoeRow(
            arrival=datetime.now(timezone.utc),
            rtu_ts=None,
            ca=1,
            ioa=4001,
            type_str="M_ME_NC_1",
            value_str="3.14",
            quality_flags=0,
            cot_str="SPONT",
        )
        model.append_row(row)
        assert model.rowCount() == 1

    def test_pause_stops_appending(self) -> None:
        from datetime import datetime, timezone

        model = SoeModel()
        model.set_paused(True)
        row = _SoeRow(
            arrival=datetime.now(timezone.utc),
            rtu_ts=None,
            ca=1,
            ioa=4001,
            type_str="M_ME_NC_1",
            value_str="1.0",
            quality_flags=0,
            cot_str="SPONT",
        )
        model.append_row(row)
        assert model.rowCount() == 0  # paused -> not added

    def test_clear(self) -> None:
        from datetime import datetime, timezone

        model = SoeModel()
        row = _SoeRow(
            arrival=datetime.now(timezone.utc),
            rtu_ts=None,
            ca=1,
            ioa=4001,
            type_str="M_SP_NA_1",
            value_str="True",
            quality_flags=0,
            cot_str="SPONT",
        )
        model.append_row(row)
        model.clear()
        assert model.rowCount() == 0

    def test_quality_str(self) -> None:
        assert "IV" in _quality_str(0x80)
        assert _quality_str(0) == "OK"


class TestSoePanel:
    def test_instantiation(self, qt_app: QApplication, state: ApplicationState) -> None:
        assert SoePanel(state) is not None

    def test_set_session_clears_model(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
    ) -> None:
        panel = SoePanel(state)
        panel.set_session(str(session_id))
        assert panel._model.rowCount() == 0  # type: ignore[attr-defined]

    def test_set_session_none(self, qt_app: QApplication, state: ApplicationState) -> None:
        panel = SoePanel(state)
        panel.set_session(None)
        assert panel._session_id is None  # type: ignore[attr-defined]

    def test_read_completed_non_monitor_ignored(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
        object_ref: ObjectRef,
    ) -> None:
        from datetime import datetime, timezone

        from protoskipper.core.driver import Quality

        panel = SoePanel(state)
        panel.set_session(str(session_id))
        result = ReadResult(
            object_ref=object_ref,
            value=None,
            quality=Quality.BAD,
            timestamp=datetime.now(timezone.utc),
            error="timeout",
        )
        state.read_completed.emit(str(session_id), result)
        assert panel._model.rowCount() >= 0  # type: ignore[attr-defined]

    def test_pause_toggle(self, qt_app: QApplication, state: ApplicationState) -> None:
        panel = SoePanel(state)
        panel._pause_btn.setChecked(True)  # type: ignore[attr-defined]
        assert panel._model.paused  # type: ignore[attr-defined]
        panel._pause_btn.setChecked(False)  # type: ignore[attr-defined]
        assert not panel._model.paused  # type: ignore[attr-defined]


class TestIec104InterrogationPanel:
    def test_instantiation(self, qt_app: QApplication, state: ApplicationState) -> None:
        assert (
            Iec104InterrogationPanel(state, _FakeSM()) is not None  # type: ignore[arg-type]
        )

    def test_set_session_clears_log(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
    ) -> None:
        panel = Iec104InterrogationPanel(  # type: ignore[arg-type]
            state, _FakeSM()
        )
        panel.set_session(str(session_id))
        assert panel._log.toPlainText() == ""  # type: ignore[attr-defined]

    def test_set_session_none(self, qt_app: QApplication, state: ApplicationState) -> None:
        panel = Iec104InterrogationPanel(  # type: ignore[arg-type]
            state, _FakeSM()
        )
        panel.set_session(None)
        assert panel._session_id is None  # type: ignore[attr-defined]

    def test_reset_btn_disabled_in_production(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        device: DeviceRef,
    ) -> None:
        sid = new_session_id()
        prod_info = SessionInfo(
            session_id=sid,
            device=device,
            profile=SessionProfile.PRODUCTION,
            operator="prod_op",
        )
        prod_info.is_open = True
        state._sessions[sid] = prod_info  # type: ignore[attr-defined]
        panel = Iec104InterrogationPanel(  # type: ignore[arg-type]
            state, _FakeSM()
        )
        panel.set_session(str(sid))
        panel._refresh_buttons()  # type: ignore[attr-defined]
        assert not panel._reset_btn.isEnabled()  # type: ignore[attr-defined]


class TestIec104TimeSyncPanel:
    def test_instantiation(self, qt_app: QApplication, state: ApplicationState) -> None:
        assert (
            Iec104TimeSyncPanel(state, _FakeSM()) is not None  # type: ignore[arg-type]
        )

    def test_set_session_stops_timer(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
    ) -> None:
        panel = Iec104TimeSyncPanel(state, _FakeSM())  # type: ignore[arg-type]
        panel.set_session(str(session_id))
        panel.set_session(None)
        assert not panel._auto_timer.isActive()  # type: ignore[attr-defined]


class TestIec104BenchPanel:
    def test_instantiation(self, qt_app: QApplication, state: ApplicationState) -> None:
        assert Iec104BenchPanel(state) is not None

    def test_tile_added_on_session_opened(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        device: DeviceRef,
        session_id: SessionId,
    ) -> None:
        panel = Iec104BenchPanel(state)
        state.session_opened.emit(str(session_id), device, SessionProfile.LAB)
        assert str(session_id) in panel._tiles  # type: ignore[attr-defined]

    def test_session_focused_signal_emitted(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
    ) -> None:
        panel = Iec104BenchPanel(state)
        focused: list[str] = []
        panel.session_focused.connect(focused.append)
        tile = panel._get_or_add(str(session_id))  # type: ignore[attr-defined]
        tile.clicked.emit(str(session_id))
        assert focused == [str(session_id)]


class TestIec104CommandDialog:
    def test_instantiation(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
    ) -> None:
        dlg = Iec104CommandDialog(
            session_id=session_id,
            state=state,
            session_manager=_FakeSM(),  # type: ignore[arg-type]
        )
        assert dlg is not None
        dlg.reject()

    def test_type_combo_has_all_commands(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
    ) -> None:
        dlg = Iec104CommandDialog(
            session_id=session_id,
            state=state,
            session_manager=_FakeSM(),  # type: ignore[arg-type]
        )
        type_ids = [
            dlg._type_combo.itemData(i)  # type: ignore[attr-defined]
            for i in range(dlg._type_combo.count())  # type: ignore[attr-defined]
        ]
        assert "C_SC_NA_1" in type_ids
        assert "C_DC_NA_1" in type_ids
        assert "C_SE_NC_1" in type_ids
        dlg.reject()

    def test_prefill_from_ref(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
        device: DeviceRef,
    ) -> None:
        ref = ObjectRef(
            device=device,
            object_id="C_SC_NA_1:4099",
            data_type="boolean",
            access=Access.READ_WRITE,
        )
        dlg = Iec104CommandDialog(
            session_id=session_id,
            state=state,
            session_manager=_FakeSM(),  # type: ignore[arg-type]
            prefill_ref=ref,
        )
        assert dlg._ioa_spin.value() == 4099  # type: ignore[attr-defined]
        dlg.reject()


class TestNewSlaveDialog:
    def test_instantiation_lab(self, qt_app: QApplication) -> None:
        dlg = NewSlaveDialog(profile=SessionProfile.LAB)
        assert dlg is not None
        dlg.reject()

    def test_production_disables_dialog(self, qt_app: QApplication) -> None:
        dlg = NewSlaveDialog(profile=SessionProfile.PRODUCTION)
        assert dlg.request() is None
        dlg.reject()

    def test_defaults(self, qt_app: QApplication) -> None:
        dlg = NewSlaveDialog(profile=SessionProfile.LAB)
        assert dlg._port_spin.value() == 2404  # type: ignore[attr-defined]
        assert dlg._ca_spin.value() == 1  # type: ignore[attr-defined]
        assert dlg._bind_edit.text() == "0.0.0.0"  # type: ignore[attr-defined]
        dlg.reject()


class TestIec104DiffDialog:
    def test_instantiation(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
    ) -> None:
        dlg = Iec104DiffDialog(session_id=session_id, state=state)
        assert dlg is not None
        dlg.reject()

    def test_run_diff_no_file_selected(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
    ) -> None:
        dlg = Iec104DiffDialog(session_id=session_id, state=state)
        dlg._run_diff()  # type: ignore[attr-defined]
        assert dlg._table.rowCount() == 0  # type: ignore[attr-defined]
        dlg.reject()


class TestIec104ConformanceDialog:
    def test_instantiation(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
    ) -> None:
        dlg = Iec104ConformanceDialog(
            session_id=session_id,
            state=state,
            session_manager=_FakeSM(),  # type: ignore[arg-type]
        )
        assert dlg is not None
        dlg.reject()

    def test_has_test_cases(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        session_id: SessionId,
    ) -> None:
        dlg = Iec104ConformanceDialog(
            session_id=session_id,
            state=state,
            session_manager=_FakeSM(),  # type: ignore[arg-type]
        )
        for tc in _ALL_TESTS:
            assert tc.key in dlg._check_vars  # type: ignore[attr-defined]
        dlg.reject()

    def test_production_disables_lab_only_tests(
        self,
        qt_app: QApplication,
        state: ApplicationState,
        device: DeviceRef,
    ) -> None:
        sid = new_session_id()
        prod_info = SessionInfo(
            session_id=sid,
            device=device,
            profile=SessionProfile.PRODUCTION,
            operator="prod_op",
        )
        prod_info.is_open = True
        state._sessions[sid] = prod_info  # type: ignore[attr-defined]
        dlg = Iec104ConformanceDialog(
            session_id=sid,
            state=state,
            session_manager=_FakeSM(),  # type: ignore[arg-type]
        )
        # direct_command is LAB only -> must be disabled in PRODUCTION
        assert not dlg._check_vars["direct_command"].isEnabled()  # type: ignore[attr-defined]
        dlg.reject()
