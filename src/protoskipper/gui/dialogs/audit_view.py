# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""AuditViewDialog — chronological viewer for a ProtoSkipper audit log.

Opens a SQLite audit-log file produced by :class:`~protoskipper.core.audit.AuditLog`
and displays the rows in a filterable, read-only table. No writes are made to
the database. The dialog is intentionally self-contained: it uses only stdlib +
PySide6 and does NOT import protoskipper.core so it can open logs from any
ProtoSkipper version without driver dependencies.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QSortFilterProxyModel, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableView,
    QVBoxLayout,
    QWidget,
)

# Event categories used by the filter combo.
_ALL_EVENTS = "All events"
_CATEGORIES: dict[str, set[str]] = {
    "Reads": {"read", "read_many"},
    "Writes": {"write_authorization", "write_committed", "write_failed"},
    "Errors": {"error", "replay_mode_blocked"},
    "Session lifecycle": {"connect", "disconnect", "session_closed"},
}


class AuditViewDialog(QDialog):
    """Read-only chronological viewer for a single ProtoSkipper audit log.

    Usage::

        dlg = AuditViewDialog(path, parent=window)
        dlg.exec()
    """

    def __init__(self, db_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self.setWindowTitle(f"Audit Log — {db_path.name}")
        self.resize(900, 600)

        # ---- source model (4 visible columns: Seq, Time, Event, Details) ----
        self._source_model = QStandardItemModel(0, 4, self)
        self._source_model.setHorizontalHeaderLabels(["Seq", "Timestamp (UTC)", "Event", "Details"])

        # ---- filter proxy ----
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._source_model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        # Default: filter against event column (index 2).
        self._proxy.setFilterKeyColumn(2)

        # ---- table view ----
        self._view = QTableView(self)
        self._view.setModel(self._proxy)
        self._view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self._view.setAlternatingRowColors(True)
        self._view.verticalHeader().setVisible(False)
        self._view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._view.horizontalHeader().setStretchLastSection(True)
        self._view.setSortingEnabled(True)

        # ---- toolbar: category combo + free-text search ----
        self._category_combo = QComboBox(self)
        self._category_combo.addItem(_ALL_EVENTS)
        for cat in _CATEGORIES:
            self._category_combo.addItem(cat)
        self._category_combo.currentTextChanged.connect(self._on_filter_changed)

        self._search_edit = QLineEdit(self)
        self._search_edit.setPlaceholderText("Search…")
        self._search_edit.textChanged.connect(self._on_search_changed)

        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Filter:", self))
        filter_layout.addWidget(self._category_combo)
        filter_layout.addWidget(QLabel("Search:", self))
        filter_layout.addWidget(self._search_edit, stretch=1)

        # ---- row count label ----
        self._count_label = QLabel(self)

        # ---- close button ----
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(filter_layout)
        layout.addWidget(self._view)
        layout.addWidget(self._count_label)
        layout.addWidget(button_box)

        self._load_rows()
        self._update_count()
        self._proxy.rowsInserted.connect(self._update_count)
        self._proxy.rowsRemoved.connect(self._update_count)

    # ---- data loading ---------------------------------------------------

    def _load_rows(self) -> None:
        """Read all audit_log rows from the SQLite file into the model."""
        self._source_model.removeRows(0, self._source_model.rowCount())
        try:
            with closing(sqlite3.connect(self._db_path)) as conn:
                cur = conn.cursor()
                rows = cur.execute(
                    "SELECT seq, ts_utc, event, payload FROM audit_log ORDER BY seq ASC"
                ).fetchall()
        except sqlite3.Error as exc:
            self._source_model.appendRow(
                [
                    QStandardItem("—"),
                    QStandardItem("—"),
                    QStandardItem("error"),
                    QStandardItem(str(exc)),
                ]
            )
            return

        for seq, ts_utc, event, payload_str in rows:
            try:
                detail = json.dumps(json.loads(payload_str), separators=(", ", ": "))
            except (json.JSONDecodeError, TypeError):
                detail = str(payload_str)

            seq_item = QStandardItem(str(seq))
            seq_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._source_model.appendRow(
                [
                    seq_item,
                    QStandardItem(str(ts_utc)),
                    QStandardItem(str(event)),
                    QStandardItem(detail),
                ]
            )

    # ---- filter logic ---------------------------------------------------

    def _on_filter_changed(self, category: str) -> None:
        if category == _ALL_EVENTS:
            # Show all — use a pattern that matches any event string.
            self._proxy.setFilterKeyColumn(2)
            self._proxy.setFilterRegularExpression("")
        else:
            events = _CATEGORIES.get(category, set())
            pattern = "|".join(f"^{e}$" for e in sorted(events))
            self._proxy.setFilterKeyColumn(2)
            self._proxy.setFilterRegularExpression(pattern)

    def _on_search_changed(self, text: str) -> None:
        # When text is non-empty, search across ALL columns by using -1.
        self._proxy.setFilterKeyColumn(-1 if text else 2)
        if text:
            self._proxy.setFilterRegularExpression(text)
        else:
            # Re-apply category filter.
            self._on_filter_changed(self._category_combo.currentText())

    # ---- status ---------------------------------------------------------

    def _update_count(self) -> None:
        visible = self._proxy.rowCount()
        total = self._source_model.rowCount()
        if visible == total:
            self._count_label.setText(f"{total} row(s)")
        else:
            self._count_label.setText(f"{visible} of {total} row(s) shown")
