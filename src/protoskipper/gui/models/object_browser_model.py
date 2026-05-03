# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""ObjectBrowserModel - QAbstractTableModel for the object-browser panel.

Shows the objects on the currently-focused session, plus the most recent
read result for each.

Column order (user-visible):
  0  S.No.           — 1-based row number
  1  Label           — friendly tag name
  2  Value           — last read value ("-" if not yet read)
  3  Unit            — engineering unit (V, A, kWh, …)
  4  Address         — wire address (e.g. "holding:100" or "40101" Modbus notation)
  5  Type            — data type (uint16, float32, …)
  6  Access          — ro / rw
  7  Quality         — GOOD / BAD / UNCERTAIN / TIMEOUT / UNKNOWN
  8  Poll            — polling interval: "Off" | "1 s" | "5 s" | "10 s" | "30 s" | "60 s"
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from protoskipper.core.driver import Quality, ReadResult
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.types import SessionId
from protoskipper.gui.theme import active_theme

# Column indices — keep in sync with HEADERS.
COL_SNO = 0
COL_LABEL = 1
COL_VALUE = 2
COL_UNIT = 3
COL_ADDRESS = 4
COL_TYPE = 5
COL_ACCESS = 6
COL_QUALITY = 7
COL_POLL = 8

# Kept for backward-compat with any callers that used the old names.
COL_ID = COL_ADDRESS

# Polling choices: display label → interval in milliseconds (0 = off).
POLL_OPTIONS: list[tuple[str, int]] = [
    ("Off", 0),
    ("1 s", 1_000),
    ("2 s", 2_000),
    ("5 s", 5_000),
    ("10 s", 10_000),
    ("30 s", 30_000),
    ("60 s", 60_000),
]
POLL_LABELS = [label for label, _ in POLL_OPTIONS]
POLL_MS = {label: ms for label, ms in POLL_OPTIONS}

# 5-digit Modbus notation offsets (for the Address column hint).
_TABLE_NOTATION_OFFSET = {
    "coils": 1,
    "discrete": 10001,
    "input": 30001,
    "holding": 40001,
}


def _modbus_notation(object_id: str) -> str:
    """Return "table:addr  (Modbus NNNNN)" if parseable, else object_id as-is."""
    parts = object_id.split(":")
    if len(parts) < 2:
        return object_id
    table = parts[0]
    try:
        addr = int(parts[1])
    except ValueError:
        return object_id
    offset = _TABLE_NOTATION_OFFSET.get(table)
    if offset is None:
        return object_id
    return f"{object_id}  ({offset + addr})"


class ObjectBrowserModel(QAbstractTableModel):
    HEADERS = ("S.No.", "Label", "Value", "Unit", "Address", "Type", "Access", "Quality", "Poll")

    def __init__(self, state: ApplicationState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self._session_id: SessionId | None = None
        # In-memory poll state: object_id → poll label (e.g. "5 s"). The
        # SessionManager is the source of truth; this dict is the display cache.
        self._poll_labels: dict[str, str] = {}
        state.objects_enumerated.connect(self._on_objects_enumerated)
        state.read_completed.connect(self._on_read_completed)
        state.session_closed.connect(self._on_session_closed)

    def set_session(self, session_id: SessionId | None) -> None:
        if session_id == self._session_id:
            return
        self.beginResetModel()
        self._session_id = session_id
        self._poll_labels.clear()
        self.endResetModel()

    def set_poll_label(self, object_id: str, label: str) -> None:
        """Called by ObjectBrowserPanel when poll interval changes."""
        self._poll_labels[object_id] = label
        info = self._state.session(self._session_id) if self._session_id else None
        if info is None:
            return
        for row, obj in enumerate(info.objects):
            if obj.object_id == object_id:
                idx = self.index(row, COL_POLL)
                self.dataChanged.emit(idx, idx, [Qt.DisplayRole])
                break

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

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole
    ) -> Any:
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
            if col == COL_SNO:
                return row + 1
            if col == COL_LABEL:
                return obj.label or ""
            if col == COL_VALUE:
                if last is None:
                    return "-"
                val = last.value
                rd = obj.metadata.get("round_digits") if obj.metadata else None
                if rd and isinstance(val, float):
                    val = round(val, int(rd))
                return str(val)
            if col == COL_UNIT:
                return obj.unit or ""
            if col == COL_ADDRESS:
                return _modbus_notation(obj.object_id)
            if col == COL_TYPE:
                return obj.data_type
            if col == COL_ACCESS:
                return obj.access.value
            if col == COL_QUALITY:
                return "-" if last is None else last.quality.value
            if col == COL_POLL:
                return self._poll_labels.get(obj.object_id, "Off")

        if role == Qt.ForegroundRole and col in (COL_VALUE, COL_QUALITY):
            theme = active_theme()
            if last is None:
                return theme.quality_color(Quality.UNKNOWN)
            return theme.quality_color(last.quality)

        return None
