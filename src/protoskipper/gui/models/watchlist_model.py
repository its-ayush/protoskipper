# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""WatchlistModel - QAbstractTableModel for the watchlist panel.

Columns: Tag, Value, Quality, Updated. Rows are
``(SessionId, ObjectRef)`` tuples kept in :class:`ApplicationState`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from protoskipper.core.driver import Quality, ReadResult
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.types import SessionId
from protoskipper.gui.theme import active_theme

COL_TAG = 0
COL_VALUE = 1
COL_QUALITY = 2
COL_UPDATED = 3


class WatchlistModel(QAbstractTableModel):
    HEADERS = ("Tag", "Value", "Quality", "Updated")

    def __init__(self, state: ApplicationState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        # Per-row cached read result so the value column updates without
        # re-asking ApplicationState.
        self._cache: dict[tuple[SessionId, str], ReadResult] = {}
        state.watchlist_changed.connect(self._on_watchlist_changed)
        state.read_completed.connect(self._on_read_completed)
        state.session_closed.connect(self._on_session_closed)

    # ---- view of current rows -------------------------------------------

    def _rows(self) -> list[tuple[SessionId, Any]]:
        return self._state.watchlist()

    def row_at(self, row: int) -> tuple[SessionId, Any] | None:
        rows = self._rows()
        if 0 <= row < len(rows):
            return rows[row]
        return None

    # ---- signal handlers -------------------------------------------------

    def _on_watchlist_changed(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    def _on_read_completed(self, session_id: str, result: ReadResult) -> None:
        key = (SessionId(session_id), result.object_ref.object_id)
        self._cache[key] = result
        # Find the row, if it's in the watchlist.
        for row, (sid, obj) in enumerate(self._rows()):
            if sid == SessionId(session_id) and obj.object_id == result.object_ref.object_id:
                left = self.index(row, COL_VALUE)
                right = self.index(row, COL_UPDATED)
                self.dataChanged.emit(left, right, [Qt.DisplayRole, Qt.ForegroundRole])
                break

    def _on_session_closed(self, session_id: str) -> None:
        # Stale rows are kept; their values just stop updating. We mark them
        # by emitting dataChanged so the foreground colour can dim.
        sid = SessionId(session_id)
        affected = [i for i, (s, _) in enumerate(self._rows()) if s == sid]
        for row in affected:
            left = self.index(row, 0)
            right = self.index(row, self.columnCount() - 1)
            self.dataChanged.emit(left, right, [Qt.ForegroundRole])

    # ---- QAbstractTableModel API ----------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows())

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole
    ) -> Any:
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return self.HEADERS[section]

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        rows = self._rows()
        if row >= len(rows):
            return None
        sid, obj = rows[row]
        cache = self._cache.get((sid, obj.object_id))

        if role == Qt.DisplayRole:
            if col == COL_TAG:
                return obj.label or obj.object_id
            if col == COL_VALUE:
                if cache is None:
                    return "-"
                return _format_value(cache.value, obj.unit)
            if col == COL_QUALITY:
                return cache.quality.value if cache else "-"
            if col == COL_UPDATED:
                return _format_timestamp(cache.timestamp) if cache else "-"

        if role == Qt.ForegroundRole:
            theme = active_theme()
            if cache is None:
                return theme.quality_color(Quality.UNKNOWN)
            info = self._state.session(sid)
            if info is not None and not info.is_open:
                return theme.quality_color(Quality.UNKNOWN)
            return theme.quality_color(cache.quality)

        return None


def _format_value(value: Any, unit: str | None) -> str:
    if value is None:
        return "-"
    rendered = repr(value) if isinstance(value, (list, tuple, dict)) else str(value)
    return f"{rendered} {unit}" if unit else rendered


def _format_timestamp(ts: datetime) -> str:
    return ts.astimezone().strftime("%H:%M:%S")
