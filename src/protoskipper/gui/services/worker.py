# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""DriverWorker - a QObject hosted on a QThread that owns a core Session.

One worker per active session. Slots dispatched from the UI thread are
executed on the worker thread; signals emitted by the worker are received
on the UI thread (default queued connection). Drivers interact with the
network entirely from inside the worker, so no I/O ever blocks the UI.

Cancellation is cooperative: each long-running slot checks ``self._cancel``
at yield points. The UI calls :meth:`cancel` to flip the flag; we never
call :meth:`QThread.terminate`.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from protoskipper.core.driver import (
    DeviceRef,
    ObjectRef,
    ProtocolDriver,
    SafetyContext,
    SessionProfile,
    WriteIntent,
)
from protoskipper.core.errors import AuthorizationDenied, ProtoSkipperError
from protoskipper.core.session import Session, open_session
from protoskipper.gui.services.types import SessionId

_logger = logging.getLogger(__name__)


class DriverWorker(QObject):
    """Owns a single core :class:`Session` and serves UI requests.

    The worker is created by :class:`SessionManager`, moved onto a fresh
    :class:`QThread`, and then talked to via signals/slots only. Public
    state (current session, last error) is exposed through signals; nothing
    on this object should be read directly from the UI thread.
    """

    # ---- signals (received on UI thread) ---------------------------------
    discovery_progress = Signal(DeviceRef)
    discovery_finished = Signal(int)  # n_found
    session_opened = Signal()
    objects_enumerated = Signal(list)  # list[ObjectRef]
    read_completed = Signal(object)  # ReadResult
    write_intent_prepared = Signal(object)  # WriteIntent
    write_committed = Signal(object)  # WriteResult
    write_denied = Signal(object)  # WriteIntent
    closed = Signal()
    error_raised = Signal(str, str)  # operation, message

    def __init__(
        self,
        driver: ProtocolDriver,
        session_id: SessionId,
        confirm_callback: Any,
    ) -> None:
        super().__init__()
        self._driver = driver
        self._session_id = session_id
        self._confirm_callback = confirm_callback
        self._session: Session | None = None
        self._cancel = threading.Event()

    @property
    def session_id(self) -> SessionId:
        return self._session_id

    # ---- slots (invoked on the worker thread) ----------------------------

    @Slot(str)
    def start_discovery(self, target: str) -> None:
        self._cancel.clear()
        n_found = 0
        try:
            for device in self._driver.discover(target):
                if self._cancel.is_set():
                    break
                self.discovery_progress.emit(device)
                n_found += 1
        except Exception as exc:  # any driver-level explosion
            _logger.exception("Discovery failed")
            self.error_raised.emit("discover", str(exc))
        finally:
            self.discovery_finished.emit(n_found)

    @Slot(DeviceRef, object, str, str)
    def open(
        self,
        device: DeviceRef,
        profile: SessionProfile,
        operator: str,
        audit_dir: str,
    ) -> None:
        try:
            self._session = open_session(
                self._driver,
                device,
                profile=profile,
                operator=operator,
                audit_dir=Path(audit_dir),
                confirm=self._confirm_callback,
            )
        except Exception as exc:
            _logger.exception("Session open failed")
            self.error_raised.emit("open", str(exc))
            return
        self.session_opened.emit()

        # Eagerly enumerate objects so the GUI can populate the browser
        # without a second round-trip.
        try:
            objects = list(self._session.driver_session.enumerate_objects())
        except Exception as exc:
            _logger.exception("enumerate_objects failed")
            self.error_raised.emit("enumerate", str(exc))
            return
        self.objects_enumerated.emit(objects)

    @Slot(object)
    def read(self, ref: ObjectRef) -> None:
        if self._session is None:
            self.error_raised.emit("read", "session not open")
            return
        try:
            result = self._session.driver_session.read(ref)
        except Exception as exc:
            _logger.exception("read failed")
            self.error_raised.emit("read", str(exc))
            return
        self.read_completed.emit(result)

    @Slot(object, object)
    def prepare_write(self, ref: ObjectRef, value: Any) -> None:
        if self._session is None:
            self.error_raised.emit("prepare_write", "session not open")
            return
        try:
            intent = self._session.driver_session.prepare_write(ref, value)
        except ProtoSkipperError as exc:
            self.error_raised.emit("prepare_write", str(exc))
            return
        except Exception as exc:
            _logger.exception("prepare_write failed")
            self.error_raised.emit("prepare_write", f"internal error: {exc}")
            return
        self.write_intent_prepared.emit(intent)

    @Slot(object)
    def commit_write(self, intent: WriteIntent) -> None:
        if self._session is None:
            self.error_raised.emit("commit_write", "session not open")
            return
        try:
            result = self._session.driver_session.commit_write(intent)
        except AuthorizationDenied:
            self.write_denied.emit(intent)
            return
        except Exception as exc:
            _logger.exception("commit_write failed")
            self.error_raised.emit("commit_write", str(exc))
            return
        self.write_committed.emit(result)

    @Slot()
    def cancel(self) -> None:
        """Cooperative cancellation flag for long-running slots."""
        self._cancel.set()

    @Slot()
    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                _logger.exception("Error closing session; ignoring")
            self._session = None
        self.closed.emit()
