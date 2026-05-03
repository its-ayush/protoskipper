# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 PCAP Offline Analyzer Dialog (§5.11 of IEC104_PLAN.md).

Opens a .pcap/.pcapng file and shows four tabs:
  1. Frames   — every dissected APDU with filter DSL
  2. Sessions — per TCP-flow summary
  3. Statistics — per-type APDU count + rates
  4. Raw      — hex dump of selected frame

Uses only ``builtin_drivers.iec104.pcap`` (pure offline, no live I/O).
This dialog may import from builtin_drivers because it is viewer-only
and never writes to a device via SessionManager.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    QThread,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from protoskipper.builtin_drivers.iec104.pcap import (
    DissectedFrame,
    iter_iec104_frames,
    summarize_pcap,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load worker (bg thread so the GUI doesn't freeze on big files)
# ---------------------------------------------------------------------------


class _PcapLoaderThread(QThread):
    frame_loaded = Signal(object)  # emits DissectedFrame
    finished_loading = Signal(int)  # emits total frame count

    def __init__(self, path: str, iec104_port: int = 2404) -> None:
        super().__init__()
        self._path = path
        self._port = iec104_port

    def run(self) -> None:  # type: ignore[override]
        count = 0
        try:
            for frame in iter_iec104_frames(self._path, iec104_port=self._port):
                self.frame_loaded.emit(frame)
                count += 1
        except Exception as exc:
            _logger.exception("PCAP load error: %s", exc)
        self.finished_loading.emit(count)


# ---------------------------------------------------------------------------
# Frames model
# ---------------------------------------------------------------------------

_FRAME_COLS = ("Time", "Src", "Dst", "Format", "ASDU Type", "COT", "CA", "IOA", "Value")
_COL_TIME = 0
_COL_SRC = 1
_COL_DST = 2
_COL_FMT = 3
_COL_TYPE = 4
_COL_COT = 5
_COL_CA = 6
_COL_IOA = 7
_COL_VAL = 8


class _FrameRow:
    __slots__ = (
        "ca",
        "cot_str",
        "dst",
        "fmt",
        "frame",
        "ioa",
        "raw",
        "src",
        "ts",
        "type_str",
        "val",
    )

    def __init__(self, frame: DissectedFrame) -> None:
        self.frame = frame
        self.ts = f"{frame.timestamp:.6f}"
        flow = frame.flow
        self.src = f"{flow.src_ip}:{flow.src_port}"
        self.dst = f"{flow.dst_ip}:{flow.dst_port}"
        apdu = frame.apdu or {}
        self.fmt = str(apdu.get("format", "?"))
        asdu = frame.asdu or {}
        self.type_str = str(asdu.get("type_id", ""))
        self.cot_str = str(asdu.get("cot", ""))
        self.ca = str(asdu.get("ca", ""))
        objs = asdu.get("objects", [])
        if objs:
            first = objs[0]
            self.ioa = str(first.get("ioa", ""))
            self.val = str(first.get("value", ""))
        else:
            self.ioa = ""
            self.val = ""
        self.raw = frame.raw or b""


class _FramesModel(QAbstractTableModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[_FrameRow] = []

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return len(_FRAME_COLS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _FRAME_COLS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        r = self._rows[index.row()]
        c = index.column()
        return (r.ts, r.src, r.dst, r.fmt, r.type_str, r.cot_str, r.ca, r.ioa, r.val)[c]

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def append(self, row: _FrameRow) -> None:
        pos = len(self._rows)
        self.beginInsertRows(QModelIndex(), pos, pos)
        self._rows.append(row)
        self.endInsertRows()

    def raw_at(self, proxy_row: int) -> bytes:
        if proxy_row < len(self._rows):
            return self._rows[proxy_row].raw
        return b""

    def all_rows(self) -> list[_FrameRow]:
        return list(self._rows)


class _FrameFilterProxy(QSortFilterProxyModel):
    """Simple DSL filter: 'iec104.type == M_SP_TB_1', 'iec104.cot == spontaneous' etc."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rules: list[tuple[str, str]] = []  # [(field, value), …]

    def set_filter(self, dsl: str) -> None:
        self._rules = []
        for clause in dsl.split("&&"):
            clause = clause.strip()
            if "==" not in clause:
                continue
            lhs, rhs = clause.split("==", 1)
            field = lhs.strip().removeprefix("iec104.").strip().lower()
            value = rhs.strip().strip("\"'")
            self._rules.append((field, value))
        self.invalidateFilter()

    def filterAcceptsRow(  # type: ignore[override]
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        src = self.sourceModel()
        assert isinstance(src, _FramesModel)
        if source_row >= len(src._rows):
            return True
        r = src._rows[source_row]
        for field, value in self._rules:
            if field == "type" and r.type_str.lower() != value.lower():
                return False
            if field == "cot" and r.cot_str.lower() != value.lower():
                return False
            if field == "ioa" and r.ioa != value:
                return False
            if field == "ca" and r.ca != value:
                return False
            if field == "format" and r.fmt.lower() != value.lower():
                return False
        return True


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class Iec104PcapViewerDialog(QDialog):
    """Offline IEC 104 PCAP viewer (§5.11)."""

    def __init__(self, path: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"IEC 104 PCAP — {Path(path).name}")
        self.setMinimumSize(1100, 680)
        self._path = path
        self._frames_model = _FramesModel(self)
        self._proxy = _FrameFilterProxy(self)
        self._proxy.setSourceModel(self._frames_model)
        self._build_ui()
        self._load(path)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Status bar (top)
        self._status_lbl = QLabel("Loading…")
        root.addWidget(self._status_lbl)

        tabs = QTabWidget()
        root.addWidget(tabs)

        # --- Tab 1: Frames ---
        frames_widget = QWidget()
        frames_layout = QVBoxLayout(frames_widget)
        # Filter DSL bar
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter (DSL):"))
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("e.g. iec104.type == M_SP_TB_1 && iec104.cot == SPONT")
        self._filter_edit.textChanged.connect(self._proxy.set_filter)
        filter_row.addWidget(self._filter_edit, 1)
        clear_filter_btn = QPushButton("Clear")
        clear_filter_btn.clicked.connect(self._filter_edit.clear)
        filter_row.addWidget(clear_filter_btn)
        frames_layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self._frame_table = QTableView()
        self._frame_table.setModel(self._proxy)
        self._frame_table.setSortingEnabled(True)
        self._frame_table.setAlternatingRowColors(True)
        self._frame_table.verticalHeader().setVisible(False)
        self._frame_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hdr = self._frame_table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(True)
        self._frame_table.selectionModel().currentRowChanged.connect(self._on_frame_selected)
        splitter.addWidget(self._frame_table)

        self._raw_view = QPlainTextEdit()
        self._raw_view.setReadOnly(True)
        self._raw_view.setFont(self.font())
        self._raw_view.setMaximumHeight(160)
        splitter.addWidget(self._raw_view)
        frames_layout.addWidget(splitter)

        export_btn = QPushButton("Export CSV…")
        export_btn.clicked.connect(self._export_csv)
        frames_layout.addWidget(export_btn)
        tabs.addTab(frames_widget, "Frames")

        # --- Tab 2: Sessions ---
        self._sessions_table = QTableWidget(0, 6)
        self._sessions_table.setHorizontalHeaderLabels(
            ["Src", "Dst", "Total", "U-frames", "I-frames", "S-frames"]
        )
        self._sessions_table.horizontalHeader().setStretchLastSection(True)
        self._sessions_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tabs.addTab(self._sessions_table, "Sessions")

        # --- Tab 3: Statistics ---
        self._stats_table = QTableWidget(0, 2)
        self._stats_table.setHorizontalHeaderLabels(["ASDU Type / Metric", "Count"])
        self._stats_table.horizontalHeader().setStretchLastSection(True)
        self._stats_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tabs.addTab(self._stats_table, "Statistics")

        # --- Tab 4: Raw (same hex dump but scrollable, no table selection needed) ---
        self._hex_view = QPlainTextEdit()
        self._hex_view.setReadOnly(True)
        self._hex_view.setFont(self.font())
        tabs.addTab(self._hex_view, "Raw Hex")

    # ------------------------------------------------------------------
    def _load(self, path: str) -> None:
        self._loader = _PcapLoaderThread(path)
        self._loader.frame_loaded.connect(self._on_frame_loaded)
        self._loader.finished_loading.connect(self._on_load_finished)
        self._loader.start()

    def _on_frame_loaded(self, frame: DissectedFrame) -> None:
        row = _FrameRow(frame)
        self._frames_model.append(row)
        self._update_sessions(row)

    def _on_load_finished(self, total: int) -> None:
        self._status_lbl.setText(f"Loaded {total} frames from {Path(self._path).name}")
        self._populate_stats()

    # ------------------------------------------------------------------
    # Sessions tab
    # ------------------------------------------------------------------

    def _update_sessions(self, row: _FrameRow) -> None:
        for i in range(self._sessions_table.rowCount()):
            if (
                self._sessions_table.item(i, 0) is not None
                and self._sessions_table.item(i, 0).text() == row.src
                and self._sessions_table.item(i, 1) is not None
                and self._sessions_table.item(i, 1).text() == row.dst
            ):
                # update counts
                total = int(self._sessions_table.item(i, 2).text()) + 1
                self._sessions_table.item(i, 2).setText(str(total))
                fmt_col = {"U": 3, "I": 4, "S": 5}.get(row.fmt)
                if fmt_col is not None:
                    cur = int(self._sessions_table.item(i, fmt_col).text() or "0")
                    self._sessions_table.item(i, fmt_col).setText(str(cur + 1))
                return
        # New flow
        r = self._sessions_table.rowCount()
        self._sessions_table.setRowCount(r + 1)
        for col, val in enumerate([row.src, row.dst, "1", "0", "0", "0"]):
            self._sessions_table.setItem(r, col, QTableWidgetItem(val))
        fmt_col = {"U": 3, "I": 4, "S": 5}.get(row.fmt)
        if fmt_col is not None:
            self._sessions_table.item(r, fmt_col).setText("1")

    # ------------------------------------------------------------------
    # Statistics tab
    # ------------------------------------------------------------------

    def _populate_stats(self) -> None:
        try:
            summary = summarize_pcap(self._path)
        except Exception:
            return
        self._stats_table.setRowCount(0)
        for key, count in sorted(summary.items(), key=lambda x: -x[1]):
            row = self._stats_table.rowCount()
            self._stats_table.setRowCount(row + 1)
            self._stats_table.setItem(row, 0, QTableWidgetItem(str(key)))
            item = QTableWidgetItem(str(count))
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._stats_table.setItem(row, 1, item)

    # ------------------------------------------------------------------
    # Raw hex view
    # ------------------------------------------------------------------

    def _on_frame_selected(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if not current.isValid():
            return
        src_row = self._proxy.mapToSource(current).row()
        raw = self._frames_model.raw_at(src_row)
        hex_str = _format_hex(raw)
        self._raw_view.setPlainText(hex_str)
        self._hex_view.setPlainText(hex_str)

    # ------------------------------------------------------------------
    # Export CSV
    # ------------------------------------------------------------------

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Frames as CSV", str(Path.home()), "CSV (*.csv);;All (*)"
        )
        if not path:
            return
        rows = self._frames_model.all_rows()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(_FRAME_COLS)
            for r in rows:
                writer.writerow(
                    [r.ts, r.src, r.dst, r.fmt, r.type_str, r.cot_str, r.ca, r.ioa, r.val]
                )


def _format_hex(data: bytes) -> str:
    if not data:
        return "(empty)"
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:04x}  {hex_part:<47}  {ascii_part}")
    return "\n".join(lines)
