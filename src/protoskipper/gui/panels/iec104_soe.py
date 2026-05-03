# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 SOE (Sequence of Events) panel (§5.6 of IEC104_PLAN.md).

Streams every monitor-direction ASDU with a CP56Time2a timestamp into a
filterable, sortable table. Columns: arrival time (laptop UTC), RTU
CP56Time2a, drift, CA, IOA, type, value, quality, COT.

Filter bar: type combo, COT combo, IOA range, quality-flag filter.
Actions: Pause/Resume, Clear, Export CSV/JSON.

Listens on ``ApplicationState.objects_enumerated`` (to know the session
CA) and ``ApplicationState.frame_captured`` (raw bytes if the packet view
is live). Because we want the decoded ASDU — not raw bytes — we actually
subscribe to ``ApplicationState.read_completed`` which the IEC 104
DriverWorker emits for every spontaneous I-frame it receives.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.types import SessionId

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_COLS = (
    "Arrival (UTC)",
    "RTU Time (CP56)",
    "Drift (ms)",
    "CA",
    "IOA",
    "Type",
    "Value",
    "Quality",
    "COT",
)
_COL_ARRIVAL = 0
_COL_RTU_TS = 1
_COL_DRIFT = 2
_COL_CA = 3
_COL_IOA = 4
_COL_TYPE = 5
_COL_VALUE = 6
_COL_QUALITY = 7
_COL_COT = 8

# Quality flag string builder
_QF = {0x01: "OV", 0x10: "BL", 0x20: "SB", 0x40: "NT", 0x80: "IV"}


def _quality_str(flags: int) -> str:
    if flags == 0:
        return "OK"
    return " ".join(v for bit, v in sorted(_QF.items()) if flags & bit)


class _SoeRow:
    __slots__ = (
        "arrival",
        "ca",
        "cot_str",
        "drift_ms",
        "ioa",
        "quality_flags",
        "rtu_ts",
        "type_str",
        "value_str",
    )

    def __init__(
        self,
        arrival: datetime,
        rtu_ts: datetime | None,
        ca: int,
        ioa: int,
        type_str: str,
        value_str: str,
        quality_flags: int,
        cot_str: str,
    ) -> None:
        self.arrival = arrival
        self.rtu_ts = rtu_ts
        self.drift_ms: float | None = None
        if rtu_ts is not None:
            self.drift_ms = (arrival - rtu_ts).total_seconds() * 1000
        self.ca = ca
        self.ioa = ioa
        self.type_str = type_str
        self.value_str = value_str
        self.quality_flags = quality_flags
        self.cot_str = cot_str


class SoeModel(QAbstractTableModel):
    row_appended = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[_SoeRow] = []
        self._paused = False

    # --- QAbstractTableModel -----------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return len(_COLS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _COLS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = self._rows[index.row()]
        col = index.column()
        if col == _COL_ARRIVAL:
            return row.arrival.strftime("%H:%M:%S.%f")[:-3]
        if col == _COL_RTU_TS:
            return row.rtu_ts.strftime("%H:%M:%S.%f")[:-3] if row.rtu_ts else "—"
        if col == _COL_DRIFT:
            return f"{row.drift_ms:.1f}" if row.drift_ms is not None else "—"
        if col == _COL_CA:
            return str(row.ca)
        if col == _COL_IOA:
            return str(row.ioa)
        if col == _COL_TYPE:
            return row.type_str
        if col == _COL_VALUE:
            return row.value_str
        if col == _COL_QUALITY:
            return _quality_str(row.quality_flags)
        if col == _COL_COT:
            return row.cot_str
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # --- mutation -------------------------------------------------------

    def append_row(self, row: _SoeRow) -> None:
        if self._paused:
            return
        pos = len(self._rows)
        self.beginInsertRows(QModelIndex(), pos, pos)
        self._rows.append(row)
        self.endInsertRows()
        self.row_appended.emit()

    def clear(self) -> None:
        self.beginResetModel()
        self._rows.clear()
        self.endResetModel()

    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, v: bool) -> None:
        self._paused = v

    def all_rows(self) -> list[_SoeRow]:
        return list(self._rows)


# ---------------------------------------------------------------------------
# Filter proxy
# ---------------------------------------------------------------------------


class _SoeFilterProxy(QSortFilterProxyModel):
    """Filters by type prefix, COT, IOA range, quality flag."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._type_filter = ""  # empty = all
        self._cot_filter = ""
        self._ioa_min = 0
        self._ioa_max = 16_777_215
        self._quality_only_bad = False

    def set_type_filter(self, t: str) -> None:
        self._type_filter = t
        self.invalidateFilter()

    def set_cot_filter(self, c: str) -> None:
        self._cot_filter = c
        self.invalidateFilter()

    def set_ioa_range(self, lo: int, hi: int) -> None:
        self._ioa_min = lo
        self._ioa_max = hi
        self.invalidateFilter()

    def set_quality_bad_only(self, v: bool) -> None:
        self._quality_only_bad = v
        self.invalidateFilter()

    def filterAcceptsRow(  # type: ignore[override]
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        src = self.sourceModel()
        assert isinstance(src, SoeModel)
        if source_row >= len(src._rows):
            return True
        row = src._rows[source_row]
        if self._type_filter and not row.type_str.startswith(self._type_filter):
            return False
        if self._cot_filter and row.cot_str != self._cot_filter:
            return False
        if not (self._ioa_min <= row.ioa <= self._ioa_max):
            return False
        return not (self._quality_only_bad and row.quality_flags == 0)


# ---------------------------------------------------------------------------
# Panel widget
# ---------------------------------------------------------------------------

_COT_OPTIONS = ["(all)", "SPONT", "INIT", "PER_CYC", "BACK", "REQ", "INTROGEN", "REQCOGEN"]
_TYPE_OPTIONS = [
    "(all)",
    "M_SP",
    "M_DP",
    "M_ST",
    "M_BO",
    "M_ME",
    "M_IT",
    "M_EP",
    "M_PS",
    "C_IC",
    "C_CI",
    "C_CS",
]


class SoePanel(QWidget):
    """Sequence-of-Events streaming panel (§5.6)."""

    def __init__(
        self,
        state: ApplicationState,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._session_id: SessionId | None = None
        self._model = SoeModel(self)
        self._proxy = _SoeFilterProxy(self)
        self._proxy.setSourceModel(self._model)
        self._build_ui()
        self._model.row_appended.connect(self._on_row_appended)
        state.read_completed.connect(self._on_read_completed)

    def set_session(self, session_id: str | None) -> None:
        self._session_id = SessionId(session_id) if session_id else None
        self._row_count_label.setText("0 events")
        self._model.clear()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # ---- Toolbar ----
        toolbar = QHBoxLayout()
        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setCheckable(True)
        self._pause_btn.toggled.connect(self._on_pause_toggled)
        toolbar.addWidget(self._pause_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._model.clear)
        toolbar.addWidget(clear_btn)

        export_csv_btn = QPushButton("Export CSV…")
        export_csv_btn.clicked.connect(self._export_csv)
        toolbar.addWidget(export_csv_btn)

        export_json_btn = QPushButton("Export JSON…")
        export_json_btn.clicked.connect(self._export_json)
        toolbar.addWidget(export_json_btn)

        toolbar.addStretch()
        self._row_count_label = QLabel("0 events")
        toolbar.addWidget(self._row_count_label)
        layout.addLayout(toolbar)

        # ---- Filter bar ----
        filter_box = QGroupBox("Filters")
        flt = QHBoxLayout(filter_box)

        flt.addWidget(QLabel("Type:"))
        self._type_combo = QComboBox()
        for opt in _TYPE_OPTIONS:
            self._type_combo.addItem(opt)
        self._type_combo.currentTextChanged.connect(
            lambda t: self._proxy.set_type_filter("" if t == "(all)" else t)
        )
        flt.addWidget(self._type_combo)

        flt.addWidget(QLabel("COT:"))
        self._cot_combo = QComboBox()
        for opt in _COT_OPTIONS:
            self._cot_combo.addItem(opt)
        self._cot_combo.currentTextChanged.connect(
            lambda c: self._proxy.set_cot_filter("" if c == "(all)" else c)
        )
        flt.addWidget(self._cot_combo)

        flt.addWidget(QLabel("IOA min:"))
        self._ioa_min_spin = QSpinBox()
        self._ioa_min_spin.setRange(0, 16_777_215)
        self._ioa_min_spin.setValue(0)
        self._ioa_min_spin.valueChanged.connect(
            lambda v: self._proxy.set_ioa_range(v, self._ioa_max_spin.value())
        )
        flt.addWidget(self._ioa_min_spin)

        flt.addWidget(QLabel("max:"))
        self._ioa_max_spin = QSpinBox()
        self._ioa_max_spin.setRange(0, 16_777_215)
        self._ioa_max_spin.setValue(16_777_215)
        self._ioa_max_spin.valueChanged.connect(
            lambda v: self._proxy.set_ioa_range(self._ioa_min_spin.value(), v)
        )
        flt.addWidget(self._ioa_max_spin)

        self._bad_quality_check = QCheckBox("Bad quality only")
        self._bad_quality_check.toggled.connect(self._proxy.set_quality_bad_only)
        flt.addWidget(self._bad_quality_check)

        layout.addWidget(filter_box)

        # ---- Table ----
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(True)
        layout.addWidget(self._table)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_read_completed(self, session_id: str, result: Any) -> None:
        if self._session_id is not None and SessionId(session_id) != self._session_id:
            return
        # Only process timestamped monitor-direction results.
        meta = getattr(result, "metadata", {}) or {}
        type_str = str(getattr(result, "asdu_type", "") or meta.get("asdu_type", ""))
        if not type_str.startswith("M_"):
            return
        arrival = datetime.now(timezone.utc)
        rtu_ts_raw = getattr(result, "timestamp", None) or meta.get("timestamp")
        rtu_ts: datetime | None = None
        if isinstance(rtu_ts_raw, datetime):
            rtu_ts = rtu_ts_raw.astimezone(timezone.utc)
        ca_raw = getattr(result, "ca", None) or meta.get("ca", 0)
        ioa_raw = getattr(result, "ioa", None) or meta.get("ioa", 0)
        value_str = str(getattr(result, "value", ""))
        quality_flags = int(getattr(result, "quality", 0) or 0)
        cot_str = str(getattr(result, "cot", "") or meta.get("cot", "SPONT"))
        row = _SoeRow(
            arrival=arrival,
            rtu_ts=rtu_ts,
            ca=int(ca_raw),
            ioa=int(ioa_raw),
            type_str=type_str,
            value_str=value_str,
            quality_flags=quality_flags,
            cot_str=cot_str,
        )
        self._model.append_row(row)

    def _on_row_appended(self) -> None:
        total = self._model.rowCount()
        visible = self._proxy.rowCount()
        self._row_count_label.setText(f"{visible} / {total} events")
        # Auto-scroll only if not paused and not sorted
        if not self._model.paused:
            self._table.scrollToBottom()

    def _on_pause_toggled(self, checked: bool) -> None:
        self._model.set_paused(checked)
        self._pause_btn.setText("Resume" if checked else "Pause")

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export SOE as CSV", str(Path.home()), "CSV (*.csv);;All (*)"
        )
        if not path:
            return
        rows = self._model.all_rows()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(_COLS)
            for r in rows:
                writer.writerow(
                    [
                        r.arrival.isoformat(),
                        r.rtu_ts.isoformat() if r.rtu_ts else "",
                        f"{r.drift_ms:.1f}" if r.drift_ms is not None else "",
                        r.ca,
                        r.ioa,
                        r.type_str,
                        r.value_str,
                        _quality_str(r.quality_flags),
                        r.cot_str,
                    ]
                )

    def _export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export SOE as JSON", str(Path.home()), "JSON (*.json);;All (*)"
        )
        if not path:
            return
        rows = self._model.all_rows()
        data = [
            {
                "arrival": r.arrival.isoformat(),
                "rtu_ts": r.rtu_ts.isoformat() if r.rtu_ts else None,
                "drift_ms": r.drift_ms,
                "ca": r.ca,
                "ioa": r.ioa,
                "type": r.type_str,
                "value": r.value_str,
                "quality": _quality_str(r.quality_flags),
                "cot": r.cot_str,
            }
            for r in rows
        ]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
