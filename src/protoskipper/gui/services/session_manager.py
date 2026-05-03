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

from PySide6.QtCore import Q_ARG, QMetaObject, QObject, Qt, QThread, QTimer, Slot

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
        # Per-register poll timers: (session_id, object_id) → QTimer.
        self._poll_timers: dict[tuple[SessionId, str], QTimer] = {}
        # Handles whose threads have been asked to stop but have not yet exited.
        # Holding them here prevents premature GC of the C++ QThread object while
        # the thread is still running (PySide6 uses weak refs in signal connections).
        self._stopping: set[_WorkerHandle] = set()

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

        # Explicit QueuedConnection: Python lambdas have no QObject affinity, so
        # PySide6 defaults to DirectConnection even across threads. We must force
        # QueuedConnection to ensure these callbacks run on the main thread, not the
        # worker thread.
        worker.discovery_progress.connect(
            lambda device: self._state.record_device_discovered(device),
            Qt.QueuedConnection,
        )
        worker.discovery_finished.connect(
            lambda count, pid=protocol_id, did=discovery_id: self._on_discovery_finished(
                did, pid, count
            ),
            Qt.QueuedConnection,
        )
        worker.error_raised.connect(
            lambda op, msg: self._state.record_error(op, msg),
            Qt.QueuedConnection,
        )

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
        *,
        initial_objects: list[ObjectRef] | None = None,
        _reuse_session_id: SessionId | None = None,
    ) -> SessionId:
        """Open a new session. Returns the SessionId immediately; the
        actual open is asynchronous and reported via ApplicationState
        signals (``session_opened`` or ``session_failed``).

        If *initial_objects* is supplied they are injected into the session
        immediately after the worker's ``enumerate_objects()`` completes
        (which for Modbus returns nothing), so the object browser is
        pre-populated when loading a saved setup.

        If *_reuse_session_id* is supplied the existing session-tree entry is
        updated in place (used by :meth:`reconnect_session`).
        """
        driver = self.driver_for(device.protocol)
        session_id = _reuse_session_id if _reuse_session_id is not None else new_session_id()

        worker = DriverWorker(
            driver=driver,
            session_id=session_id,
            confirm_callback=self._confirm_handler.make_callback(),
        )
        thread = QThread()
        worker.moveToThread(thread)

        # Wire worker signals into ApplicationState.record_* mutators.
        # Explicit QueuedConnection on every lambda: Python callables have no QObject
        # thread affinity, so PySide6 would otherwise use DirectConnection and call
        # these lambdas on the worker thread — which would then mutate ApplicationState
        # (a main-thread QObject) from the wrong thread.
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
            ),
            Qt.QueuedConnection,
        )
        worker.objects_enumerated.connect(
            lambda objects, sid=session_id: self._state.record_objects_enumerated(
                sid, list(objects)
            ),
            Qt.QueuedConnection,
        )
        # If initial_objects were supplied (e.g. loading a saved setup), inject
        # them after the driver's enumerate result is applied.  Both connections
        # use QueuedConnection so they fire in order on the main-thread event loop.
        if initial_objects:
            _injected = [False]
            _objs: list[ObjectRef] = list(initial_objects)

            def _inject_initial(
                _objects: list,
                _sid: SessionId = session_id,
                _flag: list = _injected,
                _initial: list[ObjectRef] = _objs,
            ) -> None:
                if not _flag[0]:
                    _flag[0] = True
                    self._state.record_objects_enumerated(_sid, _initial)

            worker.objects_enumerated.connect(_inject_initial, Qt.QueuedConnection)
        worker.read_completed.connect(
            lambda result, sid=session_id: self._state.record_read_completed(sid, result),
            Qt.QueuedConnection,
        )
        worker.write_intent_prepared.connect(
            lambda intent, sid=session_id: self._state.record_write_intent_prepared(sid, intent),
            Qt.QueuedConnection,
        )
        worker.write_committed.connect(
            lambda result, sid=session_id: self._state.record_write_completed(sid, result),
            Qt.QueuedConnection,
        )
        worker.write_denied.connect(
            lambda intent, sid=session_id: self._state.record_write_denied(sid, intent),
            Qt.QueuedConnection,
        )
        worker.frame_captured.connect(
            self._state.record_frame_captured,
            Qt.QueuedConnection,
        )
        worker.audit_row_written.connect(
            self._state.record_audit_row_appended,
            Qt.QueuedConnection,
        )
        worker.closed.connect(
            lambda sid=session_id: self._on_worker_closed(sid),
            Qt.QueuedConnection,
        )
        worker.error_raised.connect(
            lambda op, msg, sid=session_id: self._on_worker_error(sid, op, msg),
            Qt.QueuedConnection,
        )

        thread.start()
        self._workers[session_id] = _WorkerHandle(worker=worker, thread=thread)

        # Schedule the open() slot to run on the worker thread.
        # QTimer.singleShot with a context QObject fires the callable on the context's
        # thread — no Q_ARG metatype serialization needed for arbitrary Python objects.
        _w, _d, _p, _op, _ad = worker, device, profile, operator, str(self._audit_dir)
        QTimer.singleShot(0, _w, lambda: _w.open(_d, _p, _op, _ad))

        return session_id

    def reconnect_session(self, session_id: SessionId) -> None:
        """Re-open a closed session using its stored device/profile/operator.

        The same :class:`SessionId` is reused so the device-tree entry updates
        in place rather than adding a duplicate node.  If the session is already
        open, not found, or a reconnect attempt is already in flight, this is a
        no-op.
        """
        if session_id in self._workers:
            return  # already being opened
        info = self._state.session(session_id)
        if info is None or info.is_open:
            return
        self.open_session(
            info.device,
            info.profile,
            info.operator,
            _reuse_session_id=session_id,
        )

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
        _w, _r = handle.worker, ref
        QTimer.singleShot(0, _w, lambda: _w.read(_r))

    def read_many(self, session_id: SessionId, refs: list[ObjectRef]) -> None:
        """Dispatch a batch read on the worker thread (non-blocking)."""
        handle = self._require(session_id)
        _w, _refs = handle.worker, list(refs)
        QTimer.singleShot(0, _w, lambda: _w.read_many(_refs))

    def prepare_write(self, session_id: SessionId, ref: ObjectRef, value: Any) -> None:
        handle = self._require(session_id)
        _w, _r, _v = handle.worker, ref, value
        QTimer.singleShot(0, _w, lambda: _w.prepare_write(_r, _v))

    def commit_write(self, session_id: SessionId, intent: WriteIntent) -> None:
        handle = self._require(session_id)
        _w, _i = handle.worker, intent
        QTimer.singleShot(0, _w, lambda: _w.commit_write(_i))

    def import_register_map(self, session_id: SessionId, csv_path: Path) -> None:
        """Load a CSV register map and inject its objects into *session_id*.

        The CSV is parsed on the calling thread (always the UI thread) so any
        :class:`~protoskipper.core.errors.EncodingError` surfaces synchronously.
        The parsed :class:`~protoskipper.core.driver.ObjectRef` list is then
        routed through :class:`ApplicationState` exactly as if the driver had
        enumerated them, so every connected panel updates automatically.

        Parameters
        ----------
        session_id:
            The session whose object browser should be repopulated.
        csv_path:
            Path to the CSV register-map file.

        Raises
        ------
        EncodingError
            If the CSV is structurally invalid (missing sentinel, missing
            required columns, unreadable file).  Per-row problems are
            non-fatal and only produce log warnings.
        KeyError
            If *session_id* is not an active session.
        """
        info = self._state.session(session_id)
        if info is None:
            raise KeyError(f"No session {session_id!r}")

        from protoskipper.builtin_drivers.modbus.regmap import load_csv

        objects = load_csv(csv_path, device=info.device)
        self._state.record_objects_enumerated(session_id, objects)

    def add_object(self, session_id: SessionId, ref: ObjectRef) -> None:
        """Append *ref* to the session's object list.

        Silently ignored if the session does not exist.  No uniqueness check
        is performed: the same register may appear more than once (useful for
        reading the same address with different decode options side-by-side).
        """
        info = self._state.session(session_id)
        if info is None:
            return
        new_objects = [*info.objects, ref]
        self._state.record_objects_enumerated(session_id, new_objects)

    def remove_object(self, session_id: SessionId, ref: ObjectRef) -> None:
        """Remove the first occurrence of *ref* from the session's object list.

        Matches by identity (``is``) first, then by ``object_id``.
        Silently ignored if not found.
        """
        info = self._state.session(session_id)
        if info is None:
            return
        objects = list(info.objects)
        # Try identity first (exact same ObjectRef instance).
        for i, obj in enumerate(objects):
            if obj is ref:
                del objects[i]
                self._state.record_objects_enumerated(session_id, objects)
                return
        # Fall back to object_id equality.
        for i, obj in enumerate(objects):
            if obj.object_id == ref.object_id:
                del objects[i]
                self._state.record_objects_enumerated(session_id, objects)
                return

    def clear_objects(self, session_id: SessionId) -> None:
        """Remove all objects from the session's object list."""
        info = self._state.session(session_id)
        if info is None:
            return
        self._state.record_objects_enumerated(session_id, [])

    # ---- polling ---------------------------------------------------------

    def set_poll_interval(self, session_id: SessionId, ref: ObjectRef, interval_ms: int) -> None:
        """Start or stop continuous polling of *ref* at *interval_ms* ms.

        Pass *interval_ms* = 0 to stop polling that register.
        Each ``(session_id, object_id)`` pair has at most one timer.
        """
        key = (session_id, ref.object_id)
        existing: QTimer | None = self._poll_timers.get(key)
        if existing is not None:
            existing.stop()
            existing.deleteLater()
            del self._poll_timers[key]

        if interval_ms <= 0:
            return  # 0 = off

        timer = QTimer(self)
        timer.setInterval(interval_ms)
        timer.timeout.connect(lambda: self.read(session_id, ref))
        timer.start()
        self._poll_timers[key] = timer

    def poll_interval(self, session_id: SessionId, ref: ObjectRef) -> int:
        """Return the active poll interval in ms for *ref*, or 0 if not polling."""
        key = (session_id, ref.object_id)
        t = self._poll_timers.get(key)
        return t.interval() if t is not None else 0

    def stop_all_polling(self, session_id: SessionId) -> None:
        """Stop all poll timers for *session_id* (called on session close)."""
        to_remove = [k for k in self._poll_timers if k[0] == session_id]
        for key in to_remove:
            self._poll_timers[key].stop()
            self._poll_timers[key].deleteLater()
            del self._poll_timers[key]

    def save_capture(self, session_id: SessionId, path: Path) -> None:
        """Flush the session's ring-buffer capture to a pcapng file at *path*.

        Dispatched asynchronously to the worker thread.  If the session is
        not found, the call is silently ignored.
        """
        handle = self._workers.get(session_id)
        if handle is None:
            return
        _w, _p = handle.worker, str(path)
        QTimer.singleShot(0, _w, lambda: _w.flush_capture(_p))

    def clear_capture(self, session_id: SessionId) -> None:
        """Discard all buffered capture frames for *session_id*.

        Dispatched asynchronously to the worker thread.  If the session is
        not found, the call is silently ignored.
        """
        handle = self._workers.get(session_id)
        if handle is None:
            return
        _w = handle.worker
        QTimer.singleShot(0, _w, lambda: _w.clear_capture())

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
        self.stop_all_polling(session_id)
        self._state.record_session_closed(session_id)
        handle = self._workers.get(session_id)
        if handle is not None:
            self._teardown_thread(handle)

    def _teardown_thread(self, handle: _WorkerHandle) -> None:
        """Stop and clean up a worker thread. Safe to call from any thread."""
        # Remove from active registry first so no new requests are dispatched.
        for sid, h in list(self._workers.items()):
            if h is handle:
                del self._workers[sid]
                break

        # Move to _stopping BEFORE quit() so the handle is alive for the whole
        # shutdown sequence, preventing premature GC of the C++ QThread.
        self._stopping.add(handle)
        # Connect BEFORE quit() to guarantee we never miss the finished signal.
        handle.thread.finished.connect(self._on_thread_finished_slot)
        handle.thread.quit()

        if QThread.currentThread() is not handle.thread and not handle.thread.wait(5_000):
            _logger.warning("Worker thread did not exit within 5s; abandoning")

    @Slot()
    def _on_thread_finished_slot(self) -> None:
        """Runs on the main thread (QueuedConnection) when a worker thread finishes.

        Schedules the C++ QObject cleanup via deleteLater and releases the handle
        from the stopping set so the Python wrappers can be GC'd safely.
        """
        sender_thread = self.sender()
        handle = next(
            (h for h in self._stopping if h.thread is sender_thread),
            None,
        )
        if handle is None:
            return
        self._stopping.discard(handle)
        handle.worker.deleteLater()
        handle.thread.deleteLater()

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
            else:
                # Thread has stopped; schedule safe deferred deletion of C++ objects.
                handle.worker.deleteLater()
                handle.thread.deleteLater()


class _WorkerHandle:
    """Bundle of (worker, thread) so SessionManager can clean up reliably."""

    __slots__ = ("thread", "worker")

    def __init__(self, worker: DriverWorker, thread: QThread) -> None:
        self.worker = worker
        self.thread = thread
