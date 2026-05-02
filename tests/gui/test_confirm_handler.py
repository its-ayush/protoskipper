# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""GUI tests for GuiConfirmHandler.

Covers the normal approval path and the timeout-defaults-to-deny path.
Uses a custom dialog_factory and a shortened timeout to keep the test fast.
"""

from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QDialog

from protoskipper.core.driver import DeviceRef, ObjectRef, WriteIntent
from protoskipper.gui.services.confirm_handler import GuiConfirmHandler

pytestmark = pytest.mark.gui


def _make_intent() -> WriteIntent:
    device = DeviceRef(protocol="modbus.tcp", address="192.168.1.1")
    obj = ObjectRef(device=device, object_id="holding:0", data_type="uint16")
    return WriteIntent(
        object_ref=obj,
        requested_value=42,
        encoded_bytes=b"\x00\x2a",
        description="write holding:0 = 42",
    )


def test_confirm_handler_normal_path_returns_dialog_answer(
    qtbot: object,
    qapp: QObject,
) -> None:
    """When the dialog accepts, make_callback must return True."""
    intent = _make_intent()

    class _AcceptingDialog:
        """Fake dialog whose exec() immediately returns QDialog.Accepted."""

        def exec(self) -> int:
            return int(QDialog.Accepted)

    handler = GuiConfirmHandler(dialog_factory=lambda wi, p: _AcceptingDialog())
    callback = handler.make_callback(timeout_s=2.0)

    result_holder: list[bool] = []
    done_event = threading.Event()

    def _call() -> None:
        result_holder.append(callback(intent, "lab"))  # type: ignore[arg-type]
        done_event.set()

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()

    # Process Qt events while the worker waits for the threading.Event.
    qtbot.waitUntil(lambda: done_event.is_set(), timeout=3000)  # type: ignore[union-attr]

    assert result_holder == [True], f"Expected [True], got {result_holder}"


def test_confirm_handler_times_out_to_deny(
    qtbot: object,
    qapp: QObject,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the dialog never answers, make_callback must return False after timeout."""
    intent = _make_intent()

    never_event = threading.Event()  # never set — dialog blocks forever

    def _factory(wi: object, profile: object) -> object:
        dialog = QDialog()

        # Block exec() by intercepting it: return when the test tears down.
        # We simulate "operator walked away" by making exec() block.
        def _slow_exec() -> int:
            never_event.wait(timeout=30)  # far longer than our 0.5s test timeout
            return 0

        dialog.exec = _slow_exec  # type: ignore[method-assign]
        return dialog

    handler = GuiConfirmHandler(dialog_factory=_factory)
    callback = handler.make_callback(timeout_s=0.5)  # shortened timeout for test speed

    result_holder: list[bool] = []

    def _call() -> None:
        result_holder.append(callback(intent, "lab"))  # type: ignore[arg-type]

    import logging

    with caplog.at_level(logging.WARNING, logger="protoskipper.gui.services.confirm_handler"):
        worker = threading.Thread(target=_call)
        worker.start()
        worker.join(timeout=5.0)
        never_event.set()  # unblock the dialog so it can clean up

    assert not worker.is_alive(), "Callback should have returned within 5s"
    assert result_holder == [False], f"Expected [False] (denied by timeout), got {result_holder}"
    assert any("timed out" in record.message.lower() for record in caplog.records), (
        "Expected a timeout warning in the logs"
    )
