# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""ObjectBrowserPanel - flat table view of one session's addressable objects.

Bound to whichever session is selected in the device tree. Provides the
"Read" and "Write…" actions on the toolbar, plus an "Add to Watchlist"
shortcut on the selected row.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHeaderView,
    QPushButton,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import ObjectRef
from protoskipper.gui.models.object_browser_model import ObjectBrowserModel
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId


class ObjectBrowserPanel(QWidget):
    """Table of objects for the currently focused session."""

    write_requested = Signal(str, object)  # SessionId, ObjectRef
    add_to_watchlist_requested = Signal(str, object)  # SessionId, ObjectRef

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._session_manager = session_manager
        self._session_id: SessionId | None = None

        self._model = ObjectBrowserModel(state, parent=self)
        self._view = QTableView(self)
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._view.horizontalHeader().setStretchLastSection(True)
        self._view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._view.setAlternatingRowColors(True)

        self._toolbar = QToolBar(self)
        self._read_button = QPushButton("Read selected", self)
        self._read_all_button = QPushButton("Read all", self)
        self._write_button = QPushButton("Write…", self)
        self._watch_button = QPushButton("Add to Watchlist", self)
        for btn in (self._read_button, self._read_all_button,
                    self._write_button, self._watch_button):
            self._toolbar.addWidget(btn)

        self._read_button.clicked.connect(self._on_read_clicked)
        self._read_all_button.clicked.connect(self._on_read_all_clicked)
        self._write_button.clicked.connect(self._on_write_clicked)
        self._watch_button.clicked.connect(self._on_watch_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._view)

        self._update_actions()
        self._view.selectionModel().selectionChanged.connect(self._update_actions)

    def set_session(self, session_id: str | None) -> None:
        sid = SessionId(session_id) if session_id else None
        self._session_id = sid
        self._model.set_session(sid)
        self._update_actions()

    # ---- selection helper -----------------------------------------------

    def _selected_object(self) -> ObjectRef | None:
        if self._session_id is None:
            return None
        info = self._state.session(self._session_id)
        if info is None:
            return None
        index = self._view.currentIndex()
        if not index.isValid():
            return None
        if index.row() >= len(info.objects):
            return None
        return info.objects[index.row()]

    def _update_actions(self, *_args) -> None:
        has_session = self._session_id is not None
        info = self._state.session(self._session_id) if has_session else None
        is_open = info.is_open if info else False
        has_selection = has_session and self._view.currentIndex().isValid()
        obj = self._selected_object()
        is_writable = obj is not None and obj.access.value != "ro"

        self._read_all_button.setEnabled(is_open)
        self._read_button.setEnabled(is_open and has_selection)
        self._write_button.setEnabled(is_open and has_selection and is_writable)
        self._watch_button.setEnabled(has_selection)

    # ---- handlers --------------------------------------------------------

    def _on_read_clicked(self) -> None:
        obj = self._selected_object()
        if obj is None or self._session_id is None:
            return
        self._session_manager.read(self._session_id, obj)

    def _on_read_all_clicked(self) -> None:
        if self._session_id is None:
            return
        info = self._state.session(self._session_id)
        if info is None:
            return
        for obj in info.objects:
            self._session_manager.read(self._session_id, obj)

    def _on_write_clicked(self) -> None:
        obj = self._selected_object()
        if obj is None or self._session_id is None:
            return
        self.write_requested.emit(self._session_id, obj)

    def _on_watch_clicked(self) -> None:
        obj = self._selected_object()
        if obj is None or self._session_id is None:
            return
        self.add_to_watchlist_requested.emit(self._session_id, obj)
