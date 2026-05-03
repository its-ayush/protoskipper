# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Full GUI smoke test against the Modbus simulator.

Launches ``MainWindow``, drives ``SessionManager`` directly (bypassing the
dialogs), and verifies the end-to-end session lifecycle:

  open  →  read  →  write  →  read-back  →  close

The audit log is written to a temp directory; ``verify_log()`` is called
after the session closes to confirm the HMAC chain is intact.

Design notes
------------
* We bypass ``_open_new_connection_dialog`` intentionally: dialog UI is
  tested by the dialog unit tests. Here we want the *wiring* between
  ``SessionManager``, ``ApplicationState``, and the real driver.
* The ``GuiConfirmHandler`` is constructed with a fake
  ``dialog_factory`` that immediately accepts, so writes are never blocked.
* All cross-thread synchronisation is via ``qtbot.waitSignal`` /
  ``qtbot.waitUntil``; we never call ``time.sleep``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QDialog

from protoskipper.core.audit import verify_log
from protoskipper.core.driver import Access, DeviceRef, ObjectRef, SessionProfile
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.confirm_handler import GuiConfirmHandler
from protoskipper.gui.services.session_manager import SessionManager

pytestmark = [pytest.mark.gui, pytest.mark.integration]


class _AcceptingDialog:
    """Fake confirm dialog that always accepts immediately."""

    def exec(self) -> int:
        return int(QDialog.Accepted)


def _make_services(
    audit_dir: Path,
    qapp: QObject,
) -> tuple[ApplicationState, SessionManager]:
    state = ApplicationState(parent=qapp)
    confirm = GuiConfirmHandler(
        dialog_factory=lambda wi, p: _AcceptingDialog(),
        parent=qapp,
    )
    manager = SessionManager(
        state=state,
        confirm_handler=confirm,
        audit_dir=audit_dir,
        parent=qapp,
    )
    return state, manager


def _make_ref(device: DeviceRef, obj_id: str = "holding:5") -> ObjectRef:
    return ObjectRef(
        device=device,
        object_id=obj_id,
        data_type="uint16",
        access=Access.READ_WRITE,
        label="smoke-reg",
    )


@pytest.fixture()
def audit_dir() -> Path:
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def test_main_window_full_session_lifecycle(
    qtbot: object,
    qapp: QObject,
    modbus_simulator: tuple[str, int],
    audit_dir: Path,
) -> None:
    host, port = modbus_simulator
    state, manager = _make_services(audit_dir, qapp)

    device = DeviceRef(
        protocol="modbus.tcp",
        address=f"{host}:{port}/unit=1",
        label="smoke-sim",
    )
    ref = _make_ref(device)

    # ---- open session --------------------------------------------------
    # Pass initial_objects so the session has registers to work with.
    # Modbus has no self-description protocol so enumerate_objects returns
    # nothing; initial_objects are injected after that empty enumeration.
    with qtbot.waitSignal(state.session_opened, timeout=5_000):  # type: ignore[union-attr]
        sid = manager.open_session(device, SessionProfile.LAB, "smoke-test", initial_objects=[ref])

    # objects_enumerated fires asynchronously on the worker thread after
    # enumerate_objects completes and initial_objects are injected.
    qtbot.waitUntil(  # type: ignore[union-attr]
        lambda: len(state.session(sid).objects) > 0,  # type: ignore[union-attr]
        timeout=5_000,
    )

    info = state.session(sid)
    assert info is not None
    assert info.is_open
    assert len(info.objects) > 0, "initial objects were not injected"

    # ---- read holding:5 ------------------------------------------------
    blocker_read = qtbot.waitSignal(state.read_completed, timeout=5_000)  # type: ignore[union-attr]
    with blocker_read:
        manager.read(sid, ref)
    # Signal args: (session_id: str, result: ReadResult)
    _read_sid, read_result = blocker_read.args
    assert read_result is not None

    # ---- write 4242 ----------------------------------------------------
    # Two-step: prepare → write_intent_prepared → commit → write_completed
    blocker_intent = qtbot.waitSignal(state.write_intent_prepared, timeout=5_000)  # type: ignore[union-attr]
    with blocker_intent:
        manager.prepare_write(sid, ref, 4242)
    _intent_sid, intent = blocker_intent.args
    assert intent is not None

    blocker_write = qtbot.waitSignal(state.write_completed, timeout=5_000)  # type: ignore[union-attr]
    with blocker_write:
        manager.commit_write(sid, intent)
    _write_sid, wr = blocker_write.args
    assert wr.success, f"write failed: {wr}"

    # ---- read back -----------------------------------------------------
    blocker_read2 = qtbot.waitSignal(state.read_completed, timeout=5_000)  # type: ignore[union-attr]
    with blocker_read2:
        manager.read(sid, ref)
    _rsid2, read_result2 = blocker_read2.args
    assert read_result2.value == 4242, f"expected 4242, got {read_result2.value}"

    # ---- close session -------------------------------------------------
    with qtbot.waitSignal(state.session_closed, timeout=5_000):  # type: ignore[union-attr]
        manager.close_session(sid)

    info_after = state.session(sid)
    assert info_after is not None
    assert not info_after.is_open

    # Shut down all worker threads before pytest teardown to prevent
    # "QThread: Destroyed while thread is still running" + abort on exit.
    manager.shutdown()

    # ---- verify audit log ----------------------------------------------
    logs = list(audit_dir.glob("*.audit.sqlite"))
    assert len(logs) == 1, f"expected 1 audit log, found: {logs}"
    ok, msg = verify_log(logs[0])
    assert ok, f"Audit log verification failed: {msg}"
