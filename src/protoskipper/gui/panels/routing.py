# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""RoutingPanel - BBMD/FD routing visualisation panel (P7.E.3).

Displays the BACnet internetwork topology visible from the active session:
  * BDT (Broadcast Distribution Table) from discovered BBMDs
  * FDT (Foreign Device Table) from discovered BBMDs
  * Routing announcements observed during the session

The panel is read-only in all safety profiles.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId

_logger = logging.getLogger(__name__)


class RoutingPanel(QWidget):
    """BBMD / routing topology panel (P7.E.3).

    Shows BDT and FDT tables from a user-specified BBMD address and a log of
    routing announcements received by the active BACnet session.
    """

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._sm = session_manager
        self._current_sid: SessionId | None = None

        self._build_ui()
        state.session_opened.connect(self._on_session_opened)
        state.session_closed.connect(self._on_session_closed)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        # BBMD address bar
        addr_row = QHBoxLayout()
        addr_row.addWidget(QLabel("BBMD address:"))
        self._bbmd_edit = QLineEdit()
        self._bbmd_edit.setPlaceholderText("e.g. 192.168.1.1:47808")
        self._bbmd_edit.setMaximumWidth(260)
        addr_row.addWidget(self._bbmd_edit)
        self._read_btn = QPushButton("Read BDT / FDT")
        self._read_btn.clicked.connect(self._on_read)
        addr_row.addWidget(self._read_btn)
        self._status_lbl = QLabel("")
        addr_row.addWidget(self._status_lbl, 1)
        root.addLayout(addr_row)

        # Splitter: BDT left, FDT right
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # BDT group
        bdt_group = QGroupBox("Broadcast Distribution Table (BDT)")
        bdt_layout = QVBoxLayout(bdt_group)
        self._bdt_table = self._make_table(["Address"])
        bdt_layout.addWidget(self._bdt_table)
        splitter.addWidget(bdt_group)

        # FDT group
        fdt_group = QGroupBox("Foreign Device Table (FDT)")
        fdt_layout = QVBoxLayout(fdt_group)
        self._fdt_table = self._make_table(["Address", "TTL (s)", "Remaining (s)"])
        fdt_layout.addWidget(self._fdt_table)
        splitter.addWidget(fdt_group)

        root.addWidget(splitter, 1)

        # Routing announcements log (bottom)
        log_group = QGroupBox("Routing announcements (this session)")
        log_layout = QVBoxLayout(log_group)
        self._log_table = self._make_table(["Time", "Type", "Network", "Source"])
        log_layout.addWidget(self._log_table)
        log_group.setMaximumHeight(200)
        root.addWidget(log_group)

        self._set_empty_state()

    @staticmethod
    def _make_table(headers: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        t.verticalHeader().setVisible(False)
        t.setAlternatingRowColors(True)
        return t

    def _set_empty_state(self) -> None:
        self._status_lbl.setText("No active BACnet session")
        self._read_btn.setEnabled(False)
        self._bdt_table.setRowCount(0)
        self._fdt_table.setRowCount(0)
        self._log_table.setRowCount(0)

    # ------------------------------------------------------------------
    # Session events
    # ------------------------------------------------------------------

    def _on_session_opened(self, sid: SessionId) -> None:
        self._current_sid = sid
        self._status_lbl.setText("Session active — enter a BBMD address and click Read")
        self._read_btn.setEnabled(True)

    def _on_session_closed(self, sid: SessionId) -> None:
        if sid == self._current_sid:
            self._current_sid = None
            self._set_empty_state()

    # ------------------------------------------------------------------
    # Read BDT / FDT
    # ------------------------------------------------------------------

    def _on_read(self) -> None:
        addr = self._bbmd_edit.text().strip()
        if not addr:
            self._status_lbl.setText("Enter a BBMD address first")
            return

        sid = self._current_sid
        if sid is None:
            self._status_lbl.setText("No active session")
            return

        self._status_lbl.setText(f"Reading BDT/FDT from {addr} …")
        self._read_btn.setEnabled(False)
        self.repaint()

        # Perform the read synchronously on the calling thread.
        # The session worker is on a QThread; we ask it via a blocking
        # signal/slot call so we don't block the UI thread directly.
        # For now use a simple single-shot timer so the repaint shows first.
        QTimer.singleShot(0, lambda: self._do_read(sid, addr))

    def _do_read(self, sid: SessionId, addr: str) -> None:
        try:
            worker = self._sm.worker_for(sid)
            if worker is None:
                self._status_lbl.setText("Session worker not found")
                return

            # Call the session directly via the worker's slot mechanism.
            # Both read_bdt and read_fdt are quick synchronous methods.
            session = getattr(worker, "_session", None)
            if session is None or not hasattr(session, "read_bdt"):
                self._status_lbl.setText("Active session is not a BACnet session")
                return

            bdt = session.read_bdt(addr)
            fdt = session.read_fdt(addr)
            self._populate_bdt(bdt)
            self._populate_fdt(fdt)
            self._status_lbl.setText(
                f"BDT: {len(bdt)} entries  FDT: {len(fdt)} entries  (from {addr})"
            )
        except Exception as exc:
            _logger.warning("RoutingPanel read failed: %s", exc)
            self._status_lbl.setText(f"Read failed: {exc}")
        finally:
            self._read_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Table population
    # ------------------------------------------------------------------

    def _populate_bdt(self, entries: list[dict[str, Any]]) -> None:
        self._bdt_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self._bdt_table.setItem(row, 0, QTableWidgetItem(str(entry.get("address", ""))))

    def _populate_fdt(self, entries: list[dict[str, Any]]) -> None:
        self._fdt_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self._fdt_table.setItem(row, 0, QTableWidgetItem(str(entry.get("address", ""))))
            self._fdt_table.setItem(row, 1, QTableWidgetItem(str(entry.get("ttl", ""))))
            self._fdt_table.setItem(row, 2, QTableWidgetItem(str(entry.get("remaining", ""))))

    def add_routing_announcement(
        self,
        announcement_type: str,
        network: str,
        source: str,
        timestamp: str = "",
    ) -> None:
        """Append a routing announcement row (called from session worker signals)."""
        row = self._log_table.rowCount()
        self._log_table.insertRow(row)
        self._log_table.setItem(row, 0, QTableWidgetItem(timestamp))
        self._log_table.setItem(row, 1, QTableWidgetItem(announcement_type))
        self._log_table.setItem(row, 2, QTableWidgetItem(network))
        self._log_table.setItem(row, 3, QTableWidgetItem(source))
        self._log_table.scrollToBottom()
