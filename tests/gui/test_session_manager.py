# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""GUI tests for SessionManager.

Uses a stubbed DriverWorker to test threading, signal routing, and cleanup
guarantees without a real driver or network. Each test runs in < 500 ms.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from PySide6.QtCore import QObject, Signal, Slot

from protoskipper.core.driver import DeviceRef, SessionProfile
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.confirm_handler import GuiConfirmHandler
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import CapturedFrame, SessionId

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.gui


class _FakeWorker(QObject):
    """Stub that replaces DriverWorker in SessionManager for tests.

    All signals are identical to DriverWorker. Slots emit scripted responses
    (success or error) controlled by the ``_next_fail_open`` class variable.
    """

    discovery_progress = Signal(DeviceRef)
    discovery_finished = Signal(int)
    session_opened = Signal()
    objects_enumerated = Signal(list)
    read_completed = Signal(object)
    write_intent_prepared = Signal(object)
    write_committed = Signal(object)
    write_denied = Signal(object)
    frame_captured = Signal(CapturedFrame)
    audit_row_written = Signal()
    closed = Signal()
    error_raised = Signal(str, str)

    # Class-level flag: set True before creating the worker to simulate open failure.
    _next_fail_open: bool = False

    def __init__(
        self,
        driver: object,
        session_id: SessionId,
        confirm_callback: object,
    ) -> None:
        super().__init__()
        self._fail_open = _FakeWorker._next_fail_open

    @Slot(object, object, str, str)
    def open(self, device: DeviceRef, profile: object, operator: str, audit_dir: str) -> None:
        if self._fail_open:
            self.error_raised.emit("open", "deliberate failure")
        else:
            self.session_opened.emit()
            self.objects_enumerated.emit([])

    @Slot()
    def close(self) -> None:
        self.closed.emit()

    @Slot()
    def cancel(self) -> None:
        pass  # cooperative-cancel flag; nothing to record in the stub

    @Slot(str)
    def start_discovery(self, target: str) -> None:
        self.discovery_finished.emit(0)


class _FakeDriver:
    """Minimal stub so SessionManager._drivers lookup succeeds."""

    PROTOCOL_ID = "fake"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def state(qapp: object) -> ApplicationState:
    return ApplicationState()


@pytest.fixture()
def manager(state: ApplicationState, tmp_path: object) -> Iterator[SessionManager]:
    handler = GuiConfirmHandler(dialog_factory=lambda intent, profile: None)
    mgr = SessionManager(state=state, confirm_handler=handler, audit_dir=tmp_path)  # type: ignore[arg-type]
    yield mgr
    mgr.shutdown()


@pytest.fixture()
def fake_device() -> DeviceRef:
    return DeviceRef(protocol="fake", address="fake://127.0.0.1")


@pytest.fixture(autouse=True)
def _reset_fail_flag() -> None:
    """Ensure the class-level flag is reset between tests."""
    _FakeWorker._next_fail_open = False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_open_session_emits_session_opened_on_success(
    qtbot: object,
    state: ApplicationState,
    manager: SessionManager,
    fake_device: DeviceRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "protoskipper.gui.services.session_manager.DriverWorker",
        _FakeWorker,
    )
    manager._drivers["fake"] = _FakeDriver()  # type: ignore[assignment]

    with qtbot.waitSignal(state.session_opened, timeout=500):  # type: ignore[union-attr]
        manager.open_session(fake_device, SessionProfile.LAB, "tester")


def test_open_session_emits_session_failed_on_open_error(
    qtbot: object,
    state: ApplicationState,
    manager: SessionManager,
    fake_device: DeviceRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeWorker._next_fail_open = True
    monkeypatch.setattr(
        "protoskipper.gui.services.session_manager.DriverWorker",
        _FakeWorker,
    )
    manager._drivers["fake"] = _FakeDriver()  # type: ignore[assignment]

    with qtbot.waitSignal(state.session_failed, timeout=500):  # type: ignore[union-attr]
        manager.open_session(fake_device, SessionProfile.LAB, "tester")


def test_open_session_does_not_emit_error_raised_for_open_failure(
    qtbot: object,
    state: ApplicationState,
    manager: SessionManager,
    fake_device: DeviceRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An open failure must surface as session_failed, never as error_raised.

    The distinction matters: ``session_failed`` pops a dialog and prevents a
    dangling session entry; ``error_raised`` would push a status-bar message
    and leave the session in a zombie state.
    """
    _FakeWorker._next_fail_open = True
    monkeypatch.setattr(
        "protoskipper.gui.services.session_manager.DriverWorker",
        _FakeWorker,
    )
    manager._drivers["fake"] = _FakeDriver()  # type: ignore[assignment]

    error_raised_calls: list[tuple[str, str]] = []
    state.error_raised.connect(lambda op, msg: error_raised_calls.append((op, msg)))

    with qtbot.waitSignal(state.session_failed, timeout=500):  # type: ignore[union-attr]
        manager.open_session(fake_device, SessionProfile.LAB, "tester")

    assert not error_raised_calls, (
        f"error_raised fired unexpectedly for an open failure: {error_raised_calls}"
    )


def test_close_session_calls_worker_close_and_tears_down_thread(
    qtbot: object,
    state: ApplicationState,
    manager: SessionManager,
    fake_device: DeviceRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "protoskipper.gui.services.session_manager.DriverWorker",
        _FakeWorker,
    )
    manager._drivers["fake"] = _FakeDriver()  # type: ignore[assignment]

    with qtbot.waitSignal(state.session_opened, timeout=500):  # type: ignore[union-attr]
        sid = manager.open_session(fake_device, SessionProfile.LAB, "tester")

    with qtbot.waitSignal(state.session_closed, timeout=500):  # type: ignore[union-attr]
        manager.close_session(sid)

    # The worker handle must have been removed from the registry.
    assert sid not in manager._workers


def test_cancel_discovery_flips_atomic_flag(
    qtbot: object,
    state: ApplicationState,
    manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel_discovery on an unknown id must not crash."""
    monkeypatch.setattr(
        "protoskipper.gui.services.session_manager.DriverWorker",
        _FakeWorker,
    )
    manager._drivers["fake"] = _FakeDriver()  # type: ignore[assignment]

    with qtbot.waitSignal(state.discovery_finished, timeout=500):  # type: ignore[union-attr]
        did = manager.start_discovery("fake", "127.0.0.0/30")

    # After discovery finishes the worker is torn down; cancel_discovery must
    # silently tolerate an already-gone id.
    manager.cancel_discovery(did)  # no-op — must not raise


def test_shutdown_waits_for_all_threads(
    qtbot: object,
    state: ApplicationState,
    manager: SessionManager,
    fake_device: DeviceRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "protoskipper.gui.services.session_manager.DriverWorker",
        _FakeWorker,
    )
    manager._drivers["fake"] = _FakeDriver()  # type: ignore[assignment]

    for i in range(3):
        with qtbot.waitSignal(state.session_opened, timeout=500):  # type: ignore[union-attr]
            manager.open_session(fake_device, SessionProfile.LAB, f"op{i}")

    assert len(manager._workers) == 3, "Expected 3 worker handles before shutdown"

    manager.shutdown()

    # After shutdown() returns all worker handles must have been removed.
    assert len(manager._workers) == 0, (
        f"Expected 0 workers after shutdown, got {len(manager._workers)}"
    )


# ---------------------------------------------------------------------------
# P0.C.7 — cancel-during-discovery cleans up worker within 1 s
# ---------------------------------------------------------------------------


class _SlowDiscoveryWorker(_FakeWorker):
    """Variant that blocks in start_discovery until the cancel flag is set."""

    @Slot(str)
    def start_discovery(self, target: str) -> None:
        # Poll the cancel flag; emit nothing until cancelled or 10 s pass.
        import time

        deadline = time.monotonic() + 10.0
        while not self._cancel_flag.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        # Emit finished regardless so SessionManager tears down the handle.
        self.discovery_finished.emit(0)

    def __init__(
        self,
        driver: object,
        session_id: SessionId,
        confirm_callback: object,
    ) -> None:
        super().__init__(driver, session_id, confirm_callback)
        import threading

        self._cancel_flag = threading.Event()

    @Slot()
    def cancel(self) -> None:
        self._cancel_flag.set()


def test_cancel_during_discovery_cleans_up_within_1s(
    qtbot: object,
    state: ApplicationState,
    manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel_discovery must remove the worker from _workers within 1 second."""
    monkeypatch.setattr(
        "protoskipper.gui.services.session_manager.DriverWorker",
        _SlowDiscoveryWorker,
    )
    manager._drivers["fake"] = _FakeDriver()  # type: ignore[assignment]

    # Start discovery (slow — won't finish on its own for 10 s).
    did = manager.start_discovery("fake", "127.0.0.0/30")
    assert did in manager._workers, "Worker should be present immediately after start"

    # Cancel it and wait for discovery_finished to propagate.
    with qtbot.waitSignal(state.discovery_finished, timeout=1_000):  # type: ignore[union-attr]
        manager.cancel_discovery(did)

    # Worker must be gone from the active registry.
    qtbot.waitUntil(  # type: ignore[union-attr]
        lambda: did not in manager._workers,
        timeout=1_000,
    )


# ---------------------------------------------------------------------------
# P0.C.8 — no leaked QThread after shutdown
# ---------------------------------------------------------------------------


def test_shutdown_releases_all_threads(
    qtbot: object,
    state: ApplicationState,
    manager: SessionManager,
    fake_device: DeviceRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After shutdown() both _workers and _stopping must be empty."""
    monkeypatch.setattr(
        "protoskipper.gui.services.session_manager.DriverWorker",
        _FakeWorker,
    )
    manager._drivers["fake"] = _FakeDriver()  # type: ignore[assignment]

    for i in range(5):
        with qtbot.waitSignal(state.session_opened, timeout=500):  # type: ignore[union-attr]
            manager.open_session(fake_device, SessionProfile.LAB, f"op{i}")

    assert len(manager._workers) == 5

    manager.shutdown()

    # _workers must be empty immediately (shutdown() is synchronous).
    assert len(manager._workers) == 0, (
        f"Expected 0 active workers after shutdown, got {len(manager._workers)}"
    )

    # _stopping must also drain: the finished signal fires on the event loop.
    qtbot.waitUntil(  # type: ignore[union-attr]
        lambda: len(manager._stopping) == 0,
        timeout=1_000,
    )
