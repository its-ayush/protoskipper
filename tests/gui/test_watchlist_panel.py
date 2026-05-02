# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Tests for WatchlistPanel polling timer (P1.C.2)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QObject

from protoskipper.core.driver import (
    DeviceRef,
    ObjectRef,
    SessionProfile,
)
from protoskipper.gui.panels.watchlist import WatchlistPanel
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.types import SessionId, new_session_id

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_device() -> DeviceRef:
    return DeviceRef(protocol="modbus.tcp", address="192.168.1.1")


def _make_obj(obj_id: str = "holding:0") -> ObjectRef:
    return ObjectRef(device=_make_device(), object_id=obj_id, data_type="uint16")


def _open_session(
    state: ApplicationState, profile: SessionProfile = SessionProfile.LAB
) -> SessionId:
    sid = new_session_id()
    info = SessionInfo(
        session_id=sid,
        device=_make_device(),
        profile=profile,
        operator="tester",
    )
    state.record_session_opened(info)
    return sid


@pytest.fixture()
def state(qapp: QObject) -> ApplicationState:
    return ApplicationState()


@pytest.fixture()
def session_manager() -> MagicMock:
    return MagicMock()


@pytest.fixture()
def panel(
    qtbot: object,
    state: ApplicationState,
    session_manager: MagicMock,
) -> WatchlistPanel:
    w = WatchlistPanel(state, session_manager)  # type: ignore[arg-type]
    qtbot.addWidget(w)  # type: ignore[union-attr]
    return w


# ---------------------------------------------------------------------------
# Poll interval combo
# ---------------------------------------------------------------------------


def test_default_poll_is_off(panel: WatchlistPanel) -> None:
    assert panel._poll_combo.currentText() == "Off"
    assert not panel._poll_timer.isActive()


def test_set_poll_1s_starts_timer(panel: WatchlistPanel) -> None:
    panel._poll_combo.setCurrentIndex(1)  # "1 s"
    assert panel._poll_timer.isActive()
    assert panel._poll_timer.interval() == 1000


def test_set_poll_off_stops_timer(panel: WatchlistPanel) -> None:
    panel._poll_combo.setCurrentIndex(1)  # start polling
    panel._poll_combo.setCurrentIndex(0)  # back to Off
    assert not panel._poll_timer.isActive()


# ---------------------------------------------------------------------------
# Polling fires read_many
# ---------------------------------------------------------------------------


def test_poll_tick_calls_read_many(
    qtbot: object,
    panel: WatchlistPanel,
    state: ApplicationState,
    session_manager: MagicMock,
) -> None:
    """Enabling 1 s polling and waiting 2.2 s must fire read_many ≥ 2 times."""
    sid = _open_session(state)
    obj = _make_obj()
    state.add_to_watchlist(sid, obj)

    panel._poll_combo.setCurrentIndex(1)  # 1 s

    # Wait 2.2 s and count calls.
    qtbot.waitUntil(  # type: ignore[union-attr]
        lambda: session_manager.read_many.call_count >= 2,
        timeout=2200,
    )
    assert session_manager.read_many.call_count >= 2
    # Each call should reference the right session.
    first_call_sid = session_manager.read_many.call_args_list[0][0][0]
    assert first_call_sid == sid


# ---------------------------------------------------------------------------
# Session-closed cancels timer
# ---------------------------------------------------------------------------


def test_session_closed_stops_timer(
    panel: WatchlistPanel,
    state: ApplicationState,
) -> None:
    sid = _open_session(state)
    obj = _make_obj()
    state.add_to_watchlist(sid, obj)

    panel._poll_combo.setCurrentIndex(1)  # start polling
    assert panel._poll_timer.isActive()

    state.record_session_closed(sid)
    # All sessions gone → timer must stop.
    assert not panel._poll_timer.isActive()
    assert panel._poll_combo.currentText() == "Off"


# ---------------------------------------------------------------------------
# PRODUCTION guard
# ---------------------------------------------------------------------------


def test_production_guard_shown_for_production_session(
    qtbot: object,
    panel: WatchlistPanel,
    state: ApplicationState,
) -> None:
    """Selecting a poll interval when a PRODUCTION session is open prompts
    confirmation; rejecting keeps polling off."""
    sid = _open_session(state, profile=SessionProfile.PRODUCTION)
    obj = _make_obj()
    state.add_to_watchlist(sid, obj)

    with patch("protoskipper.gui.panels.watchlist.QMessageBox.question") as mock_q:
        mock_q.return_value = pytest.importorskip("PySide6.QtWidgets").QMessageBox.StandardButton.No
        panel._poll_combo.setCurrentIndex(1)  # "1 s"

    mock_q.assert_called_once()
    # Rejected → should be back to "Off".
    assert panel._poll_combo.currentText() == "Off"
    assert not panel._poll_timer.isActive()
