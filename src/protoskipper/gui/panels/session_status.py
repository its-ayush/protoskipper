# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""SessionStatusPanel - small list of active sessions with profile + audit info.

Lives in the right dock so the operator always sees, at a glance, which
sessions are open, what profile each is on, and where its audit log is
being written. This is the panel the safety story relies on for visibility.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.theme import active_theme


class SessionStatusPanel(QWidget):
    HEADERS = ("Status", "Protocol", "Address", "Profile", "Operator")

    def __init__(self, state: ApplicationState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._table = QTableWidget(0, len(self.HEADERS), self)
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._table)

        state.session_opened.connect(lambda *_args: self._refresh())
        state.session_closed.connect(lambda *_args: self._refresh())
        state.session_failed.connect(lambda *_args: self._refresh())

    def _refresh(self) -> None:
        sessions = self._state.sessions()
        self._table.setRowCount(len(sessions))
        theme = active_theme()
        for row, info in enumerate(sessions):
            self._set(row, 0, _status_text(info))
            self._set(row, 1, info.device.protocol)
            self._set(row, 2, info.device.label or info.device.address)
            profile_item = QTableWidgetItem(info.profile.value.upper())
            profile_item.setForeground(theme.profile_color(info.profile))
            self._table.setItem(row, 3, profile_item)
            self._set(row, 4, info.operator)

    def _set(self, row: int, col: int, text: str) -> None:
        self._table.setItem(row, col, QTableWidgetItem(text))


def _status_text(info: SessionInfo) -> str:
    if info.is_open:
        return "● open"
    if info.last_error:
        return "✗ failed"
    return "○ closed"
