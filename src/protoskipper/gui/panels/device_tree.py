# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""DeviceTreePanel - the always-visible navigator on the left dock.

Shows installed protocols, discovered devices, open sessions, and the
objects on each session. Selection drives the rest of the application:
the object browser shows the selected session's objects, the packet view
filters by the selected session, etc.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHeaderView,
    QMenu,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import ObjectRef
from protoskipper.gui.models.device_tree_model import (
    KIND_DEVICE,
    KIND_OBJECT,
    KIND_SESSION,
    ROLE_NODE_KIND,
    ROLE_PAYLOAD,
    DeviceTreeModel,
)
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId


class DeviceTreePanel(QWidget):
    """Tree of Protocols → Devices → Sessions → Objects."""

    session_selected = Signal(str)  # SessionId
    object_selected = Signal(str, object)  # SessionId, ObjectRef
    write_requested = Signal(str, object)  # SessionId, ObjectRef
    add_to_watchlist_requested = Signal(str, object)  # SessionId, ObjectRef
    disconnect_requested = Signal(str)  # SessionId

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._session_manager = session_manager

        self._model = DeviceTreeModel(state, parent=self)
        self._view = QTreeView(self)
        self._view.setModel(self._model)
        self._view.setHeaderHidden(False)
        self._view.expandAll()
        self._view.header().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_context_menu)
        self._view.selectionModel().currentChanged.connect(self._on_current_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._view)

        # Auto-expand newly inserted rows so the user does not have to dig
        # into each protocol after every discovery / connect.
        self._model.rowsInserted.connect(
            lambda parent, _first, _last: self._view.expand(parent)
        )

    # ---- selection -------------------------------------------------------

    def _on_current_changed(self, current, _previous) -> None:
        if not current.isValid():
            return
        kind = self._model.data(current, ROLE_NODE_KIND)
        payload = self._model.data(current, ROLE_PAYLOAD)
        if kind == KIND_SESSION and isinstance(payload, SessionInfo):
            self.session_selected.emit(payload.session_id)
        elif kind == KIND_OBJECT and isinstance(payload, ObjectRef):
            session_info = self._find_session_for_object(current)
            if session_info is not None:
                self.session_selected.emit(session_info.session_id)
                self.object_selected.emit(session_info.session_id, payload)

    def _find_session_for_object(self, index) -> SessionInfo | None:
        parent = index.parent()
        if not parent.isValid():
            return None
        payload = self._model.data(parent, ROLE_PAYLOAD)
        if isinstance(payload, SessionInfo):
            return payload
        return None

    # ---- context menu ----------------------------------------------------

    def _on_context_menu(self, point) -> None:
        index = self._view.indexAt(point)
        if not index.isValid():
            return
        kind = self._model.data(index, ROLE_NODE_KIND)
        payload = self._model.data(index, ROLE_PAYLOAD)
        menu = QMenu(self._view)

        if kind == KIND_SESSION and isinstance(payload, SessionInfo):
            disconnect_action = QAction("Disconnect", self)
            disconnect_action.triggered.connect(
                lambda _checked=False, sid=payload.session_id:
                self.disconnect_requested.emit(sid)
            )
            disconnect_action.setEnabled(payload.is_open)
            menu.addAction(disconnect_action)

        elif kind == KIND_OBJECT and isinstance(payload, ObjectRef):
            session_info = self._find_session_for_object(index)
            if session_info is not None:
                read_action = QAction("Read now", self)
                read_action.triggered.connect(
                    lambda _checked=False, sid=session_info.session_id, ref=payload:
                    self._session_manager.read(SessionId(sid), ref)
                )
                menu.addAction(read_action)

                write_action = QAction("Write…", self)
                write_action.setEnabled(payload.access.value != "ro")
                write_action.triggered.connect(
                    lambda _checked=False, sid=session_info.session_id, ref=payload:
                    self.write_requested.emit(sid, ref)
                )
                menu.addAction(write_action)

                menu.addSeparator()

                watch_action = QAction("Add to Watchlist", self)
                watch_action.triggered.connect(
                    lambda _checked=False, sid=session_info.session_id, ref=payload:
                    self.add_to_watchlist_requested.emit(sid, ref)
                )
                menu.addAction(watch_action)

        elif kind == KIND_DEVICE:
            # Future: "Open connection..." pre-filled with this device.
            pass

        if menu.actions():
            menu.exec(self._view.viewport().mapToGlobal(point))
