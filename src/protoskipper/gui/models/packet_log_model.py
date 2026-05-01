# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""PacketLogModel - QAbstractTableModel for the packet view.

Captured frames flow into the model via :attr:`ApplicationState.frame_captured`.
The model is bounded (default 10 000 rows); older rows are evicted FIFO.
A bigger backlog is held by the per-session capture file (pcapng) on disk.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.types import CapturedFrame, Direction, SessionId
from protoskipper.gui.theme import active_theme

COL_TIME = 0
COL_DIR = 1
COL_LEN = 2
COL_DECODED = 3
COL_HEX = 4

DEFAULT_MAX_ROWS = 10_000


class PacketLogModel(QAbstractTableModel):
    HEADERS = ("Time", "Dir", "Len", "Decoded", "Hex")

    def __init__(
        self,
        state: ApplicationState,
        parent=None,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._max_rows = max_rows
        self._frames: deque[CapturedFrame] = deque(maxlen=max_rows)
        self._session_filter: SessionId | None = None
        state.frame_captured.connect(self._on_frame)

    def set_session_filter(self, session_id: SessionId | None) -> None:
        if session_id == self._session_filter:
            return
        self._session_filter = session_id
        self.beginResetModel()
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._frames.clear()
        self.endResetModel()

    # ---- signal handler -------------------------------------------------

    def _on_frame(self, frame: CapturedFrame) -> None:
        if self._session_filter is not None and frame.session_id != self._session_filter:
            # Still keep it so the user can pivot the filter, just don't
            # surface it as a row right now.
            self._frames.append(frame)
            return
        is_full = len(self._frames) == self._max_rows
        if is_full:
            # Evict the oldest visible row before inserting.
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self._frames.popleft()
            self.endRemoveRows()
        row = len(self._frames)
        self.beginInsertRows(QModelIndex(), row, row)
        self._frames.append(frame)
        self.endInsertRows()

    # ---- API ------------------------------------------------------------

    def _visible_frames(self) -> list[CapturedFrame]:
        if self._session_filter is None:
            return list(self._frames)
        return [f for f in self._frames if f.session_id == self._session_filter]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._visible_frames())

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
        frames = self._visible_frames()
        if index.row() >= len(frames):
            return None
        frame = frames[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == COL_TIME:
                return _format_time(frame.timestamp)
            if col == COL_DIR:
                return "TX→" if frame.direction == Direction.TX else "←RX"
            if col == COL_LEN:
                return str(len(frame.payload))
            if col == COL_DECODED:
                return frame.decoded or ""
            if col == COL_HEX:
                hex_preview = frame.payload[:32].hex(" ")
                return hex_preview + ("…" if len(frame.payload) > 32 else "")

        if role == Qt.ForegroundRole and col == COL_DIR:
            theme = active_theme()
            return theme.direction_tx if frame.direction == Direction.TX else theme.direction_rx

        return None


def _format_time(ts: datetime) -> str:
    return ts.astimezone().strftime("%H:%M:%S.%f")[:-3]
