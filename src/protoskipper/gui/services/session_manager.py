# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""SessionManager - the only thing that creates DriverWorkers and routes calls.

UI panels invoke :class:`SessionManager` methods (open, read, write, close).
Internally, it creates a :class:`QThread` per session, instantiates a
:class:`DriverWorker` on it, and translates UI requests into queued slot
invocations. Worker signals are translated into ``ApplicationState.record_*``
calls, which emit the public signals panels listen to.

This file has more wiring than logic; the logic lives in
:class:`DriverWorker` and the core. The wiring is what guarantees the
"never call drivers from the UI thread" rule.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Q_ARG, QMetaObject, QObject, Qt, QThread

from protoskipper.core.driver import (
    DeviceRef,
    ObjectRef,
    ProtocolDriver,
    SessionProfile,
    WriteIntent,
)
from protoskipper.core.plugin_loader import load_protocol_drivers
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.confirm_handler import GuiConfirmHandler
from protoskipper.gui.services.types import SessionId, new_session_id
from protoskipper.gui.services.worker import DriverWorker

_logger = logging.getLogger(__name__)


class SessionManager(QObject):
    """Owns worker threads, dispatches UI requests, mirrors results into state."""

    def __init__(
        self,
        state: ApplicationState,
        confirm_handler: GuiConfirmHandler,
        audit_dir: Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._confirm_handler = confirm_handler
        self._audit_dir = audit_dir
        self._workers: dict[SessionId, _WorkerHandle] = {}
        self._drivers: dict[str, ProtocolDriver] = {}

    # ---- driver registry --------------------------------------------------

    def driver_for(self, protocol_id: str) -> ProtocolDriver:
        """Return a singleton driver instance for the given protocol id.

        Drivers are stateless; one instance per protocol per process is
        plenty. Created lazily so a missing optional dependency only
        surfaces when the user actually picks that protocol.
        """
        if protocol_id not in self._drivers:
            classes = load_protocol_drivers()
            cls = classes.get(protocol_id)
            if cls is None:
                raise KeyError(f"No driver registered for protocol {protocol_id!r}")
            self._drivers[protocol_id] = cls()
        return self._drivers[protocol_id]

    def available_protocols(self) -> dict[str, type[ProtocolDriver]]:
        return load_protocol_drivers()

    # ---- discovery (transient worker, no session) ------------------------

    def start_discovery(self, protocol_id: str, target: str) -> SessionId:
        """Spawn a transient discovery worker. Streams discovered devices into
        :attr:`ApplicationState.device_discovered` and emits
        :attr:`ApplicationState.discovery_finished` when done.

        Returns a discovery_id that can be passed to :meth:`cancel_discovery`.
        The worker is automatically torn down when discovery completes.
        """
        driver = self.driver_for(protocol_id)
        discovery_id = new_session_id()

        # Discovery never writes, so the confirm callback is a no-op deny.
        def _no_writes(_intent, _profile) -> bool:  # pragma: no cover
            return False

        worker = DriverWorker(
            driver=driver,
            session_id=discovery_id,
            confirm_callback=_no_writes,
        )
        thread = QThread()
        worker.moveToThread(thread)

        worker.discovery_progress.connect(
            lambda device: self._state.record_device_discovered(device)
        )
        worker.discovery_finished.connect(
            lambda count, pid=protocol_id, did=discovery_id: self._on_discovery_finished(
                did, pid, count
            )
        )
        worker.error_raised.connect(lambda op, msg: self._state.record_error(op, msg))

        self._state.discovery_started.emit(protocol_id)
        thread.start()
        self._workers[discovery_id] = _WorkerHandle(worker=worker, thread=thread)

        QMetaObject.invokeMethod(
            worker,
            "start_discovery",
            Qt.QueuedConnection,
            Q_ARG(str, target),
        )
        return discovery_id

    def cancel_discovery(self, discovery_id: SessionId) -> None:
        """Cooperatively cancel an in-flight discovery."""
        handle = self._workers.get(discovery_id)
        if handle is None:
            return
        QMetaObject.invokeMethod(handle.worker, "cancel", Qt.DirectConnection)

    def _on_discovery_finished(
        self,
        discovery_id: SessionId,
        protocol_id: str,
        count: int,
    ) -> None:
        self._state.discovery_finished.emit(protocol_id, count)
        handle = self._workers.get(discovery_id)
        if handle is not None:
            self._teardown_thread(handle)

    # ---- session lifecycle ------------------------------------------------

    def open_session(
        self,
        device: DeviceRef,
        profile: SessionProfile,
        operator: str,
    ) -> SessionId:
        """Open a new session. Returns the SessionId immediately; the
        actual open is asynchronous and reported via ApplicationState
        signals (``session_opened`` or ``session_failed``)."""
        driver = self.driver_for(device.protocol)
        session_id = new_session_id()

        worker = DriverWorker(
            driver=driver,
            session_id=session_id,
            confirm_callback=self._confirm_handler.make_callback(),
        )
        thread = QThread()
        worker.moveToThread(thread)

        # Wire worker signals into ApplicationState.record_* mutators.
        worker.session_opened.connect(
            lambda sid=session_id, dev=device, prof=profile, op=operator: (
                self._state.record_session_opened(
                    SessionInfo(
                        session_id=sid,
                        device=dev,
                        profile=prof,
                        operator=op,
                    )
                )
            )
        )
        worker.objects_enumerated.connect(
            lambda objects, sid=session_id: self._state.record_objects_enumerated(
                sid, list(objects)
            )
        )
        worker.read_completed.connect(
            lambda result, sid=session_id: self._state.record_read_completed(sid, result)
        )
        worker.write_intent_prepared.connect(
            lambda intent, sid=session_id: self._state.record_write_intent_prepared(sid, intent)
        )
        worker.write_committed.connect(
            lambda result, sid=session_id: self._state.record_write_completed(sid, result)
        )
        worker.write_denied.connect(
            lambda intent, sid=session_id: self._state.record_write_denied(sid, intent)
        )
        worker.frame_captured.connect(self._state.record_frame_captured)
        worker.closed.connect(lambda sid=session_id: self._on_worker_closed(sid))
        worker.error_raised.connect(
            lambda op, msg, sid=session_id: self._on_worker_error(sid, op, msg)
        )

        thread.start()
        self._workers[session_id] = _WorkerHandle(worker=worker, thread=thread)

        # Schedule the open() slot to run on the worker thread.
        QMetaObject.invokeMethod(
            worker,
            "open",
            Qt.QueuedConnection,
            Q_ARG(DeviceRef, device),
            Q_ARG(object, profile),
            Q_ARG(str, operator),
            Q_ARG(str, str(self._audit_dir)),
        )

        return session_id

    def close_session(self, session_id: SessionId) -> None:
        handle = self._workers.get(session_id)
        if handle is None:
            return
        # The closed signal is already wired to _on_worker_closed in open_session;
        # just queue the close slot and let that handler do teardown.
        QMetaObject.invokeMethod(handle.worker, "close", Qt.QueuedConnection)

    def cancel(self, session_id: SessionId) -> None:
        handle = self._workers.get(session_id)
        if handle is None:
            return
        QMetaObject.invokeMethod(handle.worker, "cancel", Qt.DirectConnection)
        # DirectConnection: cancel just flips an atomic flag, safe to do
        # cross-thread without queuing.

    # ---- per-session operations -----------------------------------------

    def read(self, session_id: SessionId, ref: ObjectRef) -> None:
        handle = self._require(session_id)
        QMetaObject.invokeMethod(
            handle.worker,
            "read",
            Qt.QueuedConnection,
            Q_ARG(object, ref),
        )

    def prepare_write(self, session_id: SessionId, ref: ObjectRef, value: Any) -> None:
        handle = self._require(session_id)
        QMetaObject.invokeMethod(
            handle.worker,
            "prepare_write",
            Qt.QueuedConnection,
            Q_ARG(object, ref),
            Q_ARG(object, value),
        )

    def commit_write(self, session_id: SessionId, intent: WriteIntent) -> None:
        handle = self._require(session_id)
        QMetaObject.invokeMethod(
            handle.worker,
            "commit_write",
            Qt.QueuedConnection,
            Q_ARG(object, intent),
        )

    # ---- internal --------------------------------------------------------

    def _require(self, session_id: SessionId) -> _WorkerHandle:
        handle = self._workers.get(session_id)
        if handle is None:
            raise KeyError(f"No active session {session_id!r}")
        return handle

    def _on_worker_error(self, session_id: SessionId, operation: str, message: str) -> None:
        # Failures during open(): translate to session_failed (shows a dialog).
        # Failures during other operations: surface via error_raised (status bar).
        info = self._state.session(session_id)
        if info is None:
            self._state.record_session_failed(session_id, message)
        else:
            self._state.record_error(operation, message)

    def _on_worker_closed(self, session_id: SessionId) -> None:
        """Called when a session worker's closed signal fires (any thread)."""
        self._state.record_session_closed(session_id)
        handle = self._workers.get(session_id)
        if handle is not None:
            self._teardown_thread(handle)

    def _teardown_thread(self, handle: _WorkerHandle) -> None:
        """Stop and clean up a worker thread. Safe to call from any thread."""
        # Remove from registry first so no new requests arrive.
        for sid, h in list(self._workers.items()):
            if h is handle:
                del self._workers[sid]
                break
        handle.thread.quit()
        # Schedule deferred deletion via the finished signal — the canonical Qt
        # pattern for worker-thread cleanup that is safe from any thread.
        handle.thread.finished.connect(handle.worker.deleteLater)
        handle.thread.finished.connect(handle.thread.deleteLater)
        # Only call wait() from the main thread. Calling it from the worker
        # thread itself raises QThread::wait: Thread tried to wait on itself.
        if QThread.currentThread() is not handle.thread and not handle.thread.wait(5_000):
            _logger.warning("Worker thread did not exit within 5s; abandoning")

    def shutdown(self) -> None:
        """Synchronously stop all sessions. Called from MainWindow.closeEvent()."""
        handles = list(self._workers.values())
        for handle in handles:
            # Cancel any blocking I/O (threading.Event.set() is thread-safe).
            QMetaObject.invokeMethod(handle.worker, "cancel", Qt.DirectConnection)
            handle.thread.quit()
        self._workers.clear()
        for handle in handles:
            if not handle.thread.wait(3_000):
                _logger.warning("Worker thread did not finish within 3s during shutdown")


class _WorkerHandle:
    """Bundle of (worker, thread) so SessionManager can clean up reliably."""

    __slots__ = ("thread", "worker")

    def __init__(self, worker: DriverWorker, thread: QThread) -> None:
        self.worker = worker
        self.thread = thread
