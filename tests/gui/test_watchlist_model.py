# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Tests for WatchlistModel.

Covers: empty-state, add/remove items, read-result update, session-closed
foreground colour change.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from PySide6.QtCore import QObject, Qt

from protoskipper.core.driver import DeviceRef, ObjectRef, Quality, ReadResult
from protoskipper.gui.models.watchlist_model import WatchlistModel
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


def test_empty_watchlist_rowcount_zero(state: ApplicationState) -> None:
    model = WatchlistModel(state)
    assert model.rowCount() == 0


def test_add_to_watchlist_adds_row(state: ApplicationState) -> None:
    model = WatchlistModel(state)
    sid = _open_session(state)
    obj = _make_obj()
    state.add_to_watchlist(sid, obj)
    assert model.rowCount() == 1


def test_remove_from_watchlist_removes_row(state: ApplicationState) -> None:
    model = WatchlistModel(state)
    sid = _open_session(state)
    obj = _make_obj()
    state.add_to_watchlist(sid, obj)
    state.remove_from_watchlist(sid, obj)
    assert model.rowCount() == 0


def test_read_result_updates_value_column(qtbot: object, state: ApplicationState) -> None:
    model = WatchlistModel(state)
    sid = _open_session(state)
    obj = _make_obj()
    state.add_to_watchlist(sid, obj)

    result = _make_result(obj, value=99)
    with qtbot.waitSignal(model.dataChanged, timeout=500):  # type: ignore[union-attr]
        state.record_read_completed(sid, result)

    idx = model.index(0, 1)  # COL_VALUE
    assert "99" in str(model.data(idx, Qt.DisplayRole))


def test_column_count_is_four(state: ApplicationState) -> None:
    model = WatchlistModel(state)
    assert model.columnCount() == 4


def test_session_closed_fires_data_changed(qtbot: object, state: ApplicationState) -> None:
    """After session close the foreground colour must change (dataChanged emitted)."""
    model = WatchlistModel(state)
    sid = _open_session(state)
    obj = _make_obj()
    state.add_to_watchlist(sid, obj)

    with qtbot.waitSignal(model.dataChanged, timeout=500):  # type: ignore[union-attr]
        state.record_session_closed(sid)
