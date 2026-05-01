# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""ApplicationState - the central QObject every panel subscribes to.

Holds the application's runtime state (discovered devices, open sessions,
watchlist contents, captured frames, last error) and emits signals when the
state changes. Panels react to signals; they never poll, and they never
mutate state directly. The only mutations come from
:class:`SessionManager` and other service-layer components.

Why a single QObject and not a global/store: a single object passed by
constructor injection is testable (use a fake), debuggable (one place to
breakpoint), and explicit. There is exactly one production instance at a
time, owned by :class:`MainWindow`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal

from protoskipper.core.driver import (
    DeviceRef,
    ObjectRef,
    ReadResult,
    SessionProfile,
    WriteIntent,
    WriteResult,
)
from protoskipper.gui.services.types import CapturedFrame, SessionId


@dataclass
class SessionInfo:
    """Per-session state held by ApplicationState.

    A SessionInfo is a thin projection of what the GUI needs to know about
    a session - it deliberately does NOT hold the underlying core
    :class:`Session` (that lives on the worker thread).
    """

    session_id: SessionId
    device: DeviceRef
    profile: SessionProfile
    operator: str
    is_open: bool = True
    objects: list[ObjectRef] = field(default_factory=list)
    last_values: dict[str, ReadResult] = field(default_factory=dict)
    last_error: str | None = None


class ApplicationState(QObject):
    """The central state object for the ProtoSkipper GUI.

    All panels and dialogs receive this in their constructor and connect to
    its signals. State mutations happen only via the public ``record_*``
    methods, which are called by :class:`SessionManager` after a worker
    reports back. This makes mutation paths auditable.
    """

    # ---- discovery / connection lifecycle ---------------------------------
    device_discovered = Signal(DeviceRef)
    device_lost = Signal(DeviceRef)
    discovery_started = Signal(str)  # protocol_id
    discovery_finished = Signal(str, int)  # protocol_id, count

    session_opened = Signal(str, DeviceRef, SessionProfile)  # session_id
    session_closed = Signal(str)  # session_id
    session_failed = Signal(str, str)  # session_id, error

    # ---- per-session state ------------------------------------------------
    objects_enumerated = Signal(str, list)  # session_id, list[ObjectRef]
    read_completed = Signal(str, ReadResult)  # session_id, ReadResult
    write_intent_prepared = Signal(str, WriteIntent)
    write_completed = Signal(str, WriteResult)
    write_denied = Signal(str, WriteIntent)

    # ---- watchlist / capture / misc --------------------------------------
    watchlist_changed = Signal()
    frame_captured = Signal(CapturedFrame)
    error_raised = Signal(str, str)  # operation, message
    profile_changed = Signal(str, SessionProfile)  # session_id, new_profile

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Authoritative state. Read-only from outside; mutate via record_*.
        self._discovered: dict[str, DeviceRef] = {}  # address -> DeviceRef
        self._sessions: dict[SessionId, SessionInfo] = {}
        self._watchlist: list[tuple[SessionId, ObjectRef]] = []

    # ---- read-only accessors ---------------------------------------------

    def discovered_devices(self) -> list[DeviceRef]:
        return list(self._discovered.values())

    def sessions(self) -> list[SessionInfo]:
        return list(self._sessions.values())

    def session(self, session_id: SessionId) -> SessionInfo | None:
        return self._sessions.get(session_id)

    def watchlist(self) -> list[tuple[SessionId, ObjectRef]]:
        return list(self._watchlist)

    # ---- mutators (called by SessionManager) -----------------------------

    def record_device_discovered(self, device: DeviceRef) -> None:
        key = f"{device.protocol}|{device.address}"
        if key in self._discovered:
            return
        self._discovered[key] = device
        self.device_discovered.emit(device)

    def record_session_opened(self, info: SessionInfo) -> None:
        self._sessions[info.session_id] = info
        self.session_opened.emit(info.session_id, info.device, info.profile)

    def record_session_closed(self, session_id: SessionId) -> None:
        info = self._sessions.get(session_id)
        if info is None:
            return
        info.is_open = False
        self.session_closed.emit(session_id)
        # We keep the SessionInfo around so the audit log path stays visible
        # in the session-status panel even after disconnect; full removal is
        # left to a manual "Clear closed sessions" action.

    def record_session_failed(self, session_id: SessionId, error: str) -> None:
        info = self._sessions.get(session_id)
        if info is not None:
            info.is_open = False
            info.last_error = error
        self.session_failed.emit(session_id, error)

    def record_objects_enumerated(self, session_id: SessionId, objects: list[ObjectRef]) -> None:
        info = self._sessions.get(session_id)
        if info is None:
            return
        info.objects = list(objects)
        self.objects_enumerated.emit(session_id, list(objects))

    def record_read_completed(self, session_id: SessionId, result: ReadResult) -> None:
        info = self._sessions.get(session_id)
        if info is not None:
            info.last_values[result.object_ref.object_id] = result
        self.read_completed.emit(session_id, result)

    def record_write_intent_prepared(self, session_id: SessionId, intent: WriteIntent) -> None:
        self.write_intent_prepared.emit(session_id, intent)

    def record_write_completed(self, session_id: SessionId, result: WriteResult) -> None:
        self.write_completed.emit(session_id, result)

    def record_write_denied(self, session_id: SessionId, intent: WriteIntent) -> None:
        self.write_denied.emit(session_id, intent)

    def record_frame_captured(self, frame: CapturedFrame) -> None:
        self.frame_captured.emit(frame)

    def record_error(self, operation: str, message: str) -> None:
        self.error_raised.emit(operation, message)

    def add_to_watchlist(self, session_id: SessionId, obj: ObjectRef) -> bool:
        entry = (session_id, obj)
        if entry in self._watchlist:
            return False
        self._watchlist.append(entry)
        self.watchlist_changed.emit()
        return True

    def remove_from_watchlist(self, session_id: SessionId, obj: ObjectRef) -> bool:
        try:
            self._watchlist.remove((session_id, obj))
        except ValueError:
            return False
        self.watchlist_changed.emit()
        return True
