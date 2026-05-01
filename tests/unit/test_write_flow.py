# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for WriteFlowController.

Tests verify the prepare → intent → commit signal chain using a fake
SessionManager and a fake ApplicationState.  No real QDialog is shown;
we patch WriteDialog.exec to return immediately.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from protoskipper.core.driver import (
    Access,
    DeviceRef,
    ObjectRef,
    SessionProfile,
    WriteIntent,
)
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.types import SessionId, new_session_id
from protoskipper.gui.services.write_flow import WriteFlowController

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_app():
    """Return or reuse the QApplication singleton required by Qt."""
    return QApplication.instance() or QApplication([])


def _ref(device: DeviceRef) -> ObjectRef:
    return ObjectRef(
        device=device,
        object_id="holding:0",
        data_type="uint16",
        access=Access.READ_WRITE,
        label="hold0",
    )


def _device() -> DeviceRef:
    return DeviceRef(protocol="modbus.tcp", address="127.0.0.1:502/unit=1", label="sim")


class _FakeSessionManager:
    """Records which methods were called and with what args."""

    def __init__(self) -> None:
        self.prepare_write_calls: list[tuple] = []
        self.commit_write_calls: list[tuple] = []

    def prepare_write(self, sid: SessionId, ref: ObjectRef, value: Any) -> None:
        self.prepare_write_calls.append((sid, ref, value))

    def commit_write(self, sid: SessionId, intent: WriteIntent) -> None:
        self.commit_write_calls.append((sid, intent))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def qt_app():
    return _make_app()


def test_write_flow_dispatches_prepare_and_commit():
    """Full happy-path: prepare is dispatched when dialog advances, commit when accepted."""
    state = ApplicationState()
    device = _device()
    sid = new_session_id()

    info = SessionInfo(
        session_id=sid,
        device=device,
        profile=SessionProfile.LAB,
        operator="tester",
    )
    state.record_session_opened(info)

    fake_sm = _FakeSessionManager()
    parent = QWidget()
    controller = WriteFlowController(state=state, session_manager=fake_sm, parent=parent)

    ref = _ref(device)

    # We patch WriteDialog so it never shows a real dialog.
    # The patch makes exec() return Accepted and simulates the operator
    # clicking "Prepare" by directly emitting prepare_write via fake_sm.
    with patch("protoskipper.gui.services.write_flow.WriteDialog") as mock_dialog_cls:
        mock_instance = mock_dialog_cls.return_value
        mock_instance.exec.return_value = QDialog.Accepted
        mock_instance.typed_value.return_value = 1234
        mock_instance.intent.return_value = WriteIntent(
            object_ref=ref,
            requested_value=1234,
            encoded_bytes=b"\x04\xd2",
            description="write holding:0 = 1234",
        )
        mock_instance.set_intent = MagicMock()
        mock_instance.report_prepare_failed = MagicMock()
        mock_instance._on_prepare_clicked = MagicMock()

        # Simulate the Next button: clicking it calls prepare_dispatched,
        # which calls fake_sm.prepare_write.
        next_button = MagicMock()
        next_button.clicked = MagicMock()
        next_button.clicked.disconnect = MagicMock()
        next_button.clicked.connect = MagicMock()
        mock_instance._next = next_button

        controller.start(sid, ref)

    # After exec() returns Accepted with a non-None intent, commit_write must be called.
    assert len(fake_sm.commit_write_calls) == 1
    committed_sid, committed_intent = fake_sm.commit_write_calls[0]
    assert committed_sid == sid
    assert committed_intent.description == "write holding:0 = 1234"

    parent.deleteLater()
    state.deleteLater()


def test_write_flow_skipped_when_session_not_open():
    """If the session is closed, start() is a no-op."""
    state = ApplicationState()
    fake_sm = _FakeSessionManager()
    parent = QWidget()
    controller = WriteFlowController(state=state, session_manager=fake_sm, parent=parent)

    device = _device()
    ref = _ref(device)
    non_existent_sid = new_session_id()

    # No WriteDialog should be created.
    with patch("protoskipper.gui.services.write_flow.WriteDialog") as mock_dialog_cls:
        controller.start(non_existent_sid, ref)
        mock_dialog_cls.assert_not_called()

    assert fake_sm.prepare_write_calls == []
    assert fake_sm.commit_write_calls == []

    parent.deleteLater()
    state.deleteLater()


def test_write_flow_no_commit_when_dialog_rejected():
    """If the operator cancels the dialog, commit_write must not be called."""
    state = ApplicationState()
    device = _device()
    sid = new_session_id()
    state.record_session_opened(
        SessionInfo(
            session_id=sid,
            device=device,
            profile=SessionProfile.LAB,
            operator="tester",
        )
    )

    fake_sm = _FakeSessionManager()
    parent = QWidget()
    controller = WriteFlowController(state=state, session_manager=fake_sm, parent=parent)

    ref = _ref(device)

    with patch("protoskipper.gui.services.write_flow.WriteDialog") as mock_dialog_cls:
        mock_instance = mock_dialog_cls.return_value
        mock_instance.exec.return_value = QDialog.Rejected
        mock_instance.intent.return_value = None
        mock_instance._on_prepare_clicked = MagicMock()
        next_button = MagicMock()
        next_button.clicked = MagicMock()
        next_button.clicked.disconnect = MagicMock()
        next_button.clicked.connect = MagicMock()
        mock_instance._next = next_button

        controller.start(sid, ref)

    assert fake_sm.commit_write_calls == []

    parent.deleteLater()
    state.deleteLater()
