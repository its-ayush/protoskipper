# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Tests for PacketLogModel.

Covers: empty-state, frame insertion, FIFO eviction at max_rows,
session filtering, and clear().
"""

from __future__ import annotations

from datetime import datetime

import pytest
from PySide6.QtCore import QObject

from protoskipper.gui.models.packet_log_model import PacketLogModel
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.types import CapturedFrame, Direction, SessionId, new_session_id

pytestmark = pytest.mark.gui


def _frame(sid: SessionId, direction: Direction = Direction.TX) -> CapturedFrame:
    return CapturedFrame(
        session_id=sid,
        timestamp=datetime.now(),
        direction=direction,
        payload=b"\x01\x02",
    )


@pytest.fixture()
def state(qapp: QObject) -> ApplicationState:
    return ApplicationState()


@pytest.fixture()
def sid() -> SessionId:
    return new_session_id()


def test_empty_state_rowcount_zero(state: ApplicationState) -> None:
    model = PacketLogModel(state)
    assert model.rowCount() == 0


def test_frame_captured_increments_rowcount(state: ApplicationState, sid: SessionId) -> None:
    model = PacketLogModel(state)
    state.record_frame_captured(_frame(sid))
    assert model.rowCount() == 1


def test_fifo_eviction_at_max_rows(state: ApplicationState, sid: SessionId) -> None:
    model = PacketLogModel(state, max_rows=3)
    for _ in range(4):
        state.record_frame_captured(_frame(sid))
    # max_rows=3, so we should never exceed 3 visible rows
    assert model.rowCount() == 3


def test_session_filter_excludes_other_sessions(state: ApplicationState, sid: SessionId) -> None:
    other = new_session_id()
    model = PacketLogModel(state)
    model.set_session_filter(sid)
    state.record_frame_captured(_frame(other, Direction.RX))
    state.record_frame_captured(_frame(sid, Direction.TX))
    assert model.rowCount() == 1


def test_clear_removes_all_rows(state: ApplicationState, sid: SessionId) -> None:
    model = PacketLogModel(state)
    for _ in range(5):
        state.record_frame_captured(_frame(sid))
    model.clear()
    assert model.rowCount() == 0


def test_data_returns_direction_string(state: ApplicationState, sid: SessionId) -> None:
    from PySide6.QtCore import Qt

    model = PacketLogModel(state)
    state.record_frame_captured(_frame(sid, Direction.RX))
    idx = model.index(0, 1)  # COL_DIR
    assert model.data(idx, Qt.DisplayRole) == "←RX"


def test_column_count_is_five(state: ApplicationState) -> None:
    model = PacketLogModel(state)
    assert model.columnCount() == 5
