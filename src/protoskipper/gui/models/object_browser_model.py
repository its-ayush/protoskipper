# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""ObjectBrowserModel - QAbstractTableModel for the object-browser panel.

Shows the objects on the currently-focused session, plus the most recent
read result for each.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from protoskipper.core.driver import Quality, ReadResult
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.types import SessionId
from protoskipper.gui.theme import active_theme

COL_ID = 0
COL_TYPE = 1
COL_ACCESS = 2
COL_UNIT = 3
COL_LABEL = 4
COL_VALUE = 5
COL_QUALITY = 6


class ObjectBrowserModel(QAbstractTableModel):
    HEADERS = ("ID", "Type", "Access", "Unit", "Label", "Value", "Quality")

    def __init__(self, state: ApplicationState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self._session_id: SessionId | None = None
        state.objects_enumerated.connect(self._on_objects_enumerated)
        state.read_completed.connect(self._on_read_completed)
        state.session_closed.connect(self._on_session_closed)

    def set_session(self, session_id: SessionId | None) -> None:
        if session_id == self._session_id:
            return
        self.beginResetModel()
        self._session_id = session_id
        self.endResetModel()

    # ---- signal handlers ------------------------------------------------

    def _on_objects_enumerated(self, session_id: str, _objects: list) -> None:
        if SessionId(session_id) == self._session_id:
            self.beginResetModel()
            self.endResetModel()

    def _on_read_completed(self, session_id: str, result: ReadResult) -> None:
        if SessionId(session_id) != self._session_id:
            return
        info = self._state.session(self._session_id)
        if info is None:
            return
        for row, obj in enumerate(info.objects):
            if obj.object_id == result.object_ref.object_id:
                left = self.index(row, COL_VALUE)
                right = self.index(row, COL_QUALITY)
                self.dataChanged.emit(left, right, [Qt.DisplayRole, Qt.ForegroundRole])
                break

    def _on_session_closed(self, session_id: str) -> None:
        if SessionId(session_id) == self._session_id:
            self.beginResetModel()
            self.endResetModel()

    # ---- API ------------------------------------------------------------

    def _objects(self) -> list:
        if self._session_id is None:
            return []
        info = self._state.session(self._session_id)
        return info.objects if info else []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._objects())

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.DisplayRole) -> Any:
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        objects = self._objects()
        row = index.row()
        col = index.column()
        if row >= len(objects):
            return None
        obj = objects[row]

        info = self._state.session(self._session_id) if self._session_id else None
        last = info.last_values.get(obj.object_id) if info else None

        if role == Qt.DisplayRole:
            if col == COL_ID:
                return obj.object_id
            if col == COL_TYPE:
                return obj.data_type
            if col == COL_ACCESS:
                return obj.access.value
            if col == COL_UNIT:
                return obj.unit or ""
            if col == COL_LABEL:
                return obj.label or ""
            if col == COL_VALUE:
                return "-" if last is None else str(last.value)
            if col == COL_QUALITY:
                return "-" if last is None else last.quality.value

        if role == Qt.ForegroundRole and col in (COL_VALUE, COL_QUALITY):
            theme = active_theme()
            if last is None:
                return theme.quality_color(Quality.UNKNOWN)
            return theme.quality_color(last.quality)

        return None
