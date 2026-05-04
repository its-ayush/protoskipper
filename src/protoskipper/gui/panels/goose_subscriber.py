# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""GooseSubscriberPanel — live GOOSE frame table (P8.C.4).

Displays a scrolling table of decoded GOOSE frames received on a chosen
network interface.  The user specifies:

* **Interface** — the Layer-2 network interface (e.g. ``eth0``).
* **GoCB references** — one per line; each becomes a
  :class:`GooseSubscriberService` subscription.

Frames arrive via ``GooseSubscriberQt.frame_received``, which is a
``QueuedConnection``-safe signal delivered on the Qt main thread.

Architecture
------------
The panel owns a :class:`~protoskipper.gui.services.goose_service.GooseSubscriberQt`
instance.  It does **not** import ``protoskipper_iec61850`` directly — all
GOOSE calls go through the service wrapper which lazy-imports the plugin.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from protoskipper.gui.services.goose_service import GooseSubscriberQt

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Table model
# ---------------------------------------------------------------------------

_HEADERS = ["Time (ms)", "GoCB Ref", "stNum", "sqNum", "confRev", "Sim", "Data"]
_MAX_ROWS = 500  # cap to avoid unbounded memory use


class GooseFrameModel(QAbstractTableModel):
    """In-memory table model backing the GOOSE subscriber view."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._rows: list[list[str]] = []

    # ------------------------------------------------------------------
    # QAbstractTableModel interface
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(_HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return row[col] if col < len(row) else ""
        if role == Qt.ItemDataRole.BackgroundRole and len(row) > 5 and row[5] == "yes":
            # Simulation frames get a yellow tint
            return QColor(0xFF, 0xFF, 0xCC)
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append_frame(self, frame: Any) -> None:
        """Append a decoded :class:`~protoskipper_iec61850.goose.GooseFrame`."""
        data_str = str(frame.all_data)
        row = [
            str(frame.t_ms),
            frame.go_cb_ref,
            str(frame.st_num),
            str(frame.sq_num),
            str(frame.conf_rev),
            "yes" if frame.simulation else "no",
            data_str,
        ]
        insert_pos = len(self._rows)
        self.beginInsertRows(QModelIndex(), insert_pos, insert_pos)
        self._rows.append(row)
        self.endInsertRows()
        # Trim oldest rows if we exceed the cap
        if len(self._rows) > _MAX_ROWS:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self._rows.pop(0)
            self.endRemoveRows()

    def clear(self) -> None:
        """Remove all rows."""
        if not self._rows:
            return
        self.beginResetModel()
        self._rows.clear()
        self.endResetModel()


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


class GooseSubscriberPanel(QWidget):
    """Live GOOSE frame viewer (P8.C.4).

    Parameters
    ----------
    parent:
        Optional parent widget.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Service
        self._svc = GooseSubscriberQt(self)
        self._svc.frame_received.connect(self._on_frame)
        self._svc.error_occurred.connect(self._on_error)
        self._running = False

        # Model + view
        self._model = GooseFrameModel(self)
        self._view = QTableView(self)
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        hdr = self._view.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(True)
        self._view.verticalHeader().setVisible(False)
        self._view.setAlternatingRowColors(True)

        # Config widgets
        iface_row = QHBoxLayout()
        iface_row.addWidget(QLabel("Interface:"))
        self._iface_edit = QLineEdit(self)
        self._iface_edit.setPlaceholderText("e.g. eth0")
        iface_row.addWidget(self._iface_edit)

        self._gcb_edit = QTextEdit(self)
        self._gcb_edit.setPlaceholderText(
            "One GoCB reference per line\ne.g. simpleIO/LLN0$GO$gcbAnalogValues"
        )
        self._gcb_edit.setMaximumHeight(90)

        # Toolbar
        self._toolbar = QToolBar(self)
        self._start_btn = QPushButton("Start", self)
        self._stop_btn = QPushButton("Stop", self)
        self._clear_btn = QPushButton("Clear", self)
        self._stop_btn.setEnabled(False)
        self._toolbar.addWidget(self._start_btn)
        self._toolbar.addWidget(self._stop_btn)
        self._toolbar.addWidget(self._clear_btn)

        self._status_label = QLabel("Stopped", self)

        self._start_btn.clicked.connect(self._start)
        self._stop_btn.clicked.connect(self._stop)
        self._clear_btn.clicked.connect(self._model.clear)

        # Layout
        cfg_widget = QWidget(self)
        cfg_layout = QVBoxLayout(cfg_widget)
        cfg_layout.setContentsMargins(0, 0, 0, 0)
        cfg_layout.addLayout(iface_row)
        cfg_layout.addWidget(QLabel("GoCB References:"))
        cfg_layout.addWidget(self._gcb_edit)
        cfg_layout.addWidget(self._toolbar)
        cfg_layout.addWidget(self._status_label)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(cfg_widget)
        splitter.addWidget(self._view)
        splitter.setStretchFactor(1, 3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(splitter)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _start(self) -> None:
        iface = self._iface_edit.text().strip()
        if not iface:
            QMessageBox.warning(self, "GOOSE Subscriber", "Interface name is required.")
            return
        refs = [r.strip() for r in self._gcb_edit.toPlainText().splitlines() if r.strip()]
        if not refs:
            QMessageBox.warning(
                self,
                "GOOSE Subscriber",
                "At least one GoCB reference is required.",
            )
            return

        # Rebuild service (new subscriptions after stop require a new service)
        self._svc = GooseSubscriberQt(self)
        self._svc.frame_received.connect(self._on_frame)
        self._svc.error_occurred.connect(self._on_error)

        for ref in refs:
            self._svc.add_subscription(ref)
        self._svc.start(iface)

        self._running = True
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._iface_edit.setEnabled(False)
        self._gcb_edit.setEnabled(False)
        self._status_label.setText(f"Listening on {iface!r} …")

    def _stop(self) -> None:
        self._svc.stop()
        self._running = False
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._iface_edit.setEnabled(True)
        self._gcb_edit.setEnabled(True)
        self._status_label.setText("Stopped")

    def _on_frame(self, frame: Any) -> None:
        self._model.append_frame(frame)
        # Auto-scroll to latest
        self._view.scrollToBottom()

    def _on_error(self, msg: str) -> None:
        _log.error("GooseSubscriberQt error: %s", msg)
        self._status_label.setText(f"Error: {msg}")
        if self._running:
            self._stop()
        QMessageBox.critical(self, "GOOSE Subscriber Error", msg)
