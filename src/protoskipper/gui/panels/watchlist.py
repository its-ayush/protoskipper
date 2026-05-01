# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""WatchlistPanel - persistent table of points the operator is monitoring.

Phase-1 refresh model is **manual**, by design: production-grade live
polling against substation gear should be a deliberate operator choice
configured per session, not the default. A polling timer lands as a
follow-up.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHeaderView,
    QPushButton,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from protoskipper.gui.models.watchlist_model import WatchlistModel
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId


class WatchlistPanel(QWidget):
    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._session_manager = session_manager

        self._model = WatchlistModel(state, parent=self)
        self._view = QTableView(self)
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._view.setAlternatingRowColors(True)
        self._view.verticalHeader().setVisible(False)

        self._toolbar = QToolBar(self)
        self._refresh_button = QPushButton("Refresh", self)
        self._refresh_selected_button = QPushButton("Refresh selected", self)
        self._remove_button = QPushButton("Remove", self)
        self._toolbar.addWidget(self._refresh_button)
        self._toolbar.addWidget(self._refresh_selected_button)
        self._toolbar.addWidget(self._remove_button)

        self._refresh_button.clicked.connect(self._on_refresh_all)
        self._refresh_selected_button.clicked.connect(self._on_refresh_selected)
        self._remove_button.clicked.connect(self._on_remove_selected)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._view)

    # ---- handlers --------------------------------------------------------

    def _on_refresh_all(self) -> None:
        for sid, obj in self._state.watchlist():
            session_info = self._state.session(sid)
            if session_info is None or not session_info.is_open:
                continue
            self._session_manager.read(sid, obj)

    def _on_refresh_selected(self) -> None:
        index = self._view.currentIndex()
        if not index.isValid():
            return
        row = self._model.row_at(index.row())
        if row is None:
            return
        sid, obj = row
        session_info = self._state.session(sid)
        if session_info is None or not session_info.is_open:
            return
        self._session_manager.read(SessionId(sid), obj)

    def _on_remove_selected(self) -> None:
        index = self._view.currentIndex()
        if not index.isValid():
            return
        row = self._model.row_at(index.row())
        if row is None:
            return
        sid, obj = row
        self._state.remove_from_watchlist(SessionId(sid), obj)
