# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 Diff-vs-Point-List Dialog (§5.15 of IEC104_PLAN.md).

Compares the objects actually enumerated by the driver against an
expected point list supplied as a CSV or XLSX file.

Columns in the expected CSV (header row required):
    ioa, type, unit, description

Result table shows three categories:
  • Yellow  — IOA exists in expected list but NOT returned by the RTU
  • Blue    — IOA returned by RTU but NOT in the expected list
  • Red     — IOA in both but type/unit mismatch
  • Green   — IOA in both, types match (clean)

The operator can export the diff to a CSV report.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.types import SessionId

_logger = logging.getLogger(__name__)


@dataclass
class _PointEntry:
    ioa: int
    type_str: str
    unit: str
    description: str


def _load_csv_point_list(path: str) -> list[_PointEntry]:
    entries: list[_PointEntry] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ioa = int(row.get("ioa") or row.get("IOA") or row.get("Ioa") or 0)
            except (ValueError, TypeError):
                continue
            entries.append(
                _PointEntry(
                    ioa=ioa,
                    type_str=str(row.get("type", "") or row.get("Type", "") or ""),
                    unit=str(row.get("unit", "") or row.get("Unit", "") or ""),
                    description=str(row.get("description", "") or row.get("Description", "") or ""),
                )
            )
    return entries


def _try_load_xlsx(path: str) -> list[_PointEntry] | None:
    """Try to load XLSX using openpyxl. Returns None if not installed."""
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError:
        return None
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))  # type: ignore[union-attr]
    if not rows:
        return []
    header = [str(c or "").lower().strip() for c in rows[0]]
    entries: list[_PointEntry] = []
    for row in rows[1:]:
        d = dict(zip(header, row, strict=False))
        try:
            ioa = int(d.get("ioa", 0) or 0)
        except (ValueError, TypeError):
            continue
        entries.append(
            _PointEntry(
                ioa=ioa,
                type_str=str(d.get("type", "") or ""),
                unit=str(d.get("unit", "") or ""),
                description=str(d.get("description", "") or ""),
            )
        )
    return entries


def _load_point_list(path: str) -> list[_PointEntry]:
    if path.lower().endswith((".xlsx", ".xls")):
        result = _try_load_xlsx(path)
        if result is not None:
            return result
        # Fall through to CSV attempt
    return _load_csv_point_list(path)


_STATUS_MISSING = "Missing from RTU"
_STATUS_EXTRA = "Extra (not in point list)"
_STATUS_MISMATCH = "Type mismatch"
_STATUS_OK = "OK"

_STATUS_COLOURS = {
    _STATUS_MISSING: "#FFF59D",  # yellow
    _STATUS_EXTRA: "#90CAF9",  # blue
    _STATUS_MISMATCH: "#EF9A9A",  # red
    _STATUS_OK: "#A5D6A7",  # green
}


class Iec104DiffDialog(QDialog):
    """Point-list diff dialog (§5.15)."""

    def __init__(
        self,
        session_id: SessionId,
        state: ApplicationState,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("IEC 104 — Diff vs Point List")
        self.setMinimumSize(800, 560)
        self._session_id = session_id
        self._state = state
        self._diff_rows: list[dict[str, str]] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ---- File picker ----
        file_box = QGroupBox("Expected Point List")
        file_layout = QHBoxLayout(file_box)
        self._file_edit = QLabel("(no file selected)")
        self._file_edit.setStyleSheet("color: #666;")
        file_layout.addWidget(self._file_edit, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        file_layout.addWidget(browse_btn)
        layout.addWidget(file_box)

        # ---- Run button ----
        run_layout = QHBoxLayout()
        self._run_btn = QPushButton("Run Diff")
        self._run_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._run_diff)
        run_layout.addWidget(self._run_btn)
        self._status_lbl = QLabel("")
        run_layout.addWidget(self._status_lbl, 1)
        layout.addLayout(run_layout)

        # ---- Results table ----
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["IOA", "Type (expected)", "Type (actual)", "Unit", "Description", "Status"]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self._table)

        # ---- Legend ----
        legend = QHBoxLayout()
        for status, colour in _STATUS_COLOURS.items():
            chip = QLabel(f"  {status}  ")
            chip.setStyleSheet(f"background: {colour}; border-radius: 3px; padding: 1px 4px;")
            legend.addWidget(chip)
        legend.addStretch()
        layout.addLayout(legend)

        # ---- Buttons ----
        bb = QDialogButtonBox()
        self._export_btn = bb.addButton("Export CSV…", QDialogButtonBox.ButtonRole.ActionRole)
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_csv)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Point List",
            str(Path.home()),
            "CSV / Excel (*.csv *.xlsx *.xls);;All files (*)",
        )
        if path:
            self._point_list_path = path
            self._file_edit.setText(Path(path).name)
            self._file_edit.setStyleSheet("")
            self._run_btn.setEnabled(True)

    def _run_diff(self) -> None:
        path = getattr(self, "_point_list_path", None)
        if not path:
            return
        info = self._state.session(self._session_id)
        if info is None:
            QMessageBox.warning(self, "Session not found", "The selected session no longer exists.")
            return

        try:
            expected_list = _load_point_list(path)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot read point list", str(exc))
            return

        expected: dict[int, _PointEntry] = {e.ioa: e for e in expected_list}
        actual: dict[int, str] = {}  # ioa → type_str
        for ref in info.objects:
            if ":" in ref.object_id:
                type_str, ioa_str = ref.object_id.split(":", 1)
                import contextlib

                with contextlib.suppress(ValueError):
                    actual[int(ioa_str)] = type_str

        all_ioas = sorted(set(expected.keys()) | set(actual.keys()))
        self._table.setRowCount(0)
        self._diff_rows = []

        for ioa in all_ioas:
            exp = expected.get(ioa)
            act_type = actual.get(ioa, "")
            exp_type = exp.type_str if exp else ""

            if exp and ioa not in actual:
                status = _STATUS_MISSING
            elif ioa not in expected:
                status = _STATUS_EXTRA
            elif exp_type and act_type and exp_type != act_type:
                status = _STATUS_MISMATCH
            else:
                status = _STATUS_OK

            colour = _STATUS_COLOURS[status]
            row = self._table.rowCount()
            self._table.setRowCount(row + 1)

            cells = [
                str(ioa),
                exp_type,
                act_type,
                exp.unit if exp else "",
                exp.description if exp else "",
                status,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setBackground(Qt.GlobalColor.white)
                item.setData(
                    Qt.ItemDataRole.BackgroundRole,
                    _colour_to_brush(colour),
                )
                self._table.setItem(row, col, item)

            self._diff_rows.append(
                dict(
                    zip(
                        ["ioa", "type_expected", "type_actual", "unit", "description", "status"],
                        cells,
                        strict=False,
                    )
                )
            )

        missing = sum(1 for r in self._diff_rows if r["status"] == _STATUS_MISSING)
        extra = sum(1 for r in self._diff_rows if r["status"] == _STATUS_EXTRA)
        mismatch = sum(1 for r in self._diff_rows if r["status"] == _STATUS_MISMATCH)
        ok = sum(1 for r in self._diff_rows if r["status"] == _STATUS_OK)
        self._status_lbl.setText(
            f"{len(all_ioas)} IOAs - {ok} OK / {missing} missing / "
            f"{extra} extra / {mismatch} mismatch"
        )
        self._export_btn.setEnabled(bool(self._diff_rows))

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Diff as CSV", str(Path.home()), "CSV (*.csv);;All (*)"
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["ioa", "type_expected", "type_actual", "unit", "description", "status"],
            )
            writer.writeheader()
            writer.writerows(self._diff_rows)


def _colour_to_brush(hex_colour: str):
    """Convert a CSS hex colour string to a QBrush."""
    from PySide6.QtGui import QBrush, QColor

    return QBrush(QColor(hex_colour))
