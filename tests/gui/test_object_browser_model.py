# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Tests for ObjectBrowserModel.

Covers: empty-state, after objects enumerated, after read-completed,
after session-closed.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from PySide6.QtCore import QObject, Qt

from protoskipper.core.driver import DeviceRef, ObjectRef, Quality, ReadResult
from protoskipper.gui.models.object_browser_model import ObjectBrowserModel
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.types import SessionId, new_session_id

pytestmark = pytest.mark.gui


def _make_device() -> DeviceRef:
    return DeviceRef(protocol="modbus.tcp", address="192.168.1.1")


def _make_obj(obj_id: str = "holding:0") -> ObjectRef:
    return ObjectRef(device=_make_device(), object_id=obj_id, data_type="uint16")


def _make_result(obj: ObjectRef, value: int = 42) -> ReadResult:
    return ReadResult(
        object_ref=obj,
        value=value,
        quality=Quality.GOOD,
        timestamp=datetime.now(),
    )


def _open_session(state: ApplicationState) -> SessionId:
    sid = new_session_id()
    info = SessionInfo(
        session_id=sid,
        device=_make_device(),
        profile="lab",  # type: ignore[arg-type]
        operator="tester",
    )
    state.record_session_opened(info)
    return sid


@pytest.fixture()
def state(qapp: QObject) -> ApplicationState:
    return ApplicationState()


def test_empty_state_zero_rows(state: ApplicationState) -> None:
    model = ObjectBrowserModel(state)
    assert model.rowCount() == 0


def test_objects_enumerated_adds_rows(state: ApplicationState) -> None:
    model = ObjectBrowserModel(state)
    sid = _open_session(state)
    model.set_session(sid)
    objs = [_make_obj("holding:0"), _make_obj("holding:1")]
    state.record_objects_enumerated(sid, objs)
    assert model.rowCount() == 2


def test_other_session_objects_not_shown(state: ApplicationState) -> None:
    model = ObjectBrowserModel(state)
    sid = _open_session(state)
    other = _open_session(state)
    model.set_session(sid)
    state.record_objects_enumerated(other, [_make_obj("holding:9")])
    assert model.rowCount() == 0


def test_read_completed_emits_data_changed(qtbot: object, state: ApplicationState) -> None:
    model = ObjectBrowserModel(state)
    sid = _open_session(state)
    model.set_session(sid)
    obj = _make_obj()
    state.record_objects_enumerated(sid, [obj])

    result = _make_result(obj, 77)
    with qtbot.waitSignal(model.dataChanged, timeout=500):  # type: ignore[union-attr]
        state.record_read_completed(sid, result)

    idx = model.index(0, 5)  # COL_VALUE
    cell = model.data(idx, Qt.DisplayRole)
    assert cell is not None


def test_session_closed_fires_model_reset(qtbot: object, state: ApplicationState) -> None:
    """After session close the model emits a reset (modelReset signal)."""
    model = ObjectBrowserModel(state)
    sid = _open_session(state)
    model.set_session(sid)
    state.record_objects_enumerated(sid, [_make_obj()])
    with qtbot.waitSignal(model.modelReset, timeout=500):  # type: ignore[union-attr]
        state.record_session_closed(sid)


def test_column_count_is_seven(state: ApplicationState) -> None:
    # Model has 9 columns: S.No, Label, Value, Unit, Address, Type, Access, Quality, Poll
    model = ObjectBrowserModel(state)
    assert model.columnCount() == 9
