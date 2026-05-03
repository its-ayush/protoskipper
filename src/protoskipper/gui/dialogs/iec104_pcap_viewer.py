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
    QSize,
    QSortFilterProxyModel,
    Qt,
    QThread,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen
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
    QScrollArea,
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


# ---------------------------------------------------------------------------
# APCI Timeline widget — §P4.E.3
# ---------------------------------------------------------------------------

_FMT_COLORS: dict[str, QColor] = {
    "I": QColor("#2ecc71"),  # green
    "S": QColor("#e67e22"),  # orange
    "U": QColor("#3498db"),  # blue
    "?": QColor("#95a5a6"),  # grey
}

_DOT_R = 4  # dot radius in pixels
_ROW_H = 12  # pixels between frame rows (vertical)
_MARGIN_LEFT = 70  # space for time labels
_MARGIN_RIGHT = 20
_MARGIN_TOP = 30
_MARGIN_BOTTOM = 30


class _TimelineWidget(QWidget):
    """Custom-painted APCI frame timeline.

    X-axis: relative time (seconds from first frame).
    Y-axis: frame index (top = first frame, bottom = last).
    Dot colour: I=green, S=orange, U=blue.
    The k/w outstanding-window is shown as a semi-transparent band between
    the send-sequence number on a per-flow basis.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[_FrameRow] = []
        self._t0: float = 0.0
        self._t1: float = 1.0
        self.setMinimumWidth(600)

    def append(self, row: _FrameRow) -> None:
        if not self._rows:
            self._t0 = row.frame.timestamp
        self._t1 = max(self._t1, row.frame.timestamp)
        self._rows.append(row)
        h = max(200, _MARGIN_TOP + _MARGIN_BOTTOM + len(self._rows) * _ROW_H)
        self.setMinimumHeight(h)
        self.update()

    def sizeHint(self) -> QSize:
        h = max(200, _MARGIN_TOP + _MARGIN_BOTTOM + len(self._rows) * _ROW_H)
        return QSize(800, h)

    def paintEvent(self, _event: object) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()

        span = max(self._t1 - self._t0, 1e-6)
        plot_w = w - _MARGIN_LEFT - _MARGIN_RIGHT

        # Axes
        axis_pen = QPen(QColor("#aaaaaa"), 1)
        p.setPen(axis_pen)
        p.drawLine(_MARGIN_LEFT, _MARGIN_TOP, _MARGIN_LEFT, self.height() - _MARGIN_BOTTOM)
        y_base = self.height() - _MARGIN_BOTTOM
        p.drawLine(_MARGIN_LEFT, y_base, w - _MARGIN_RIGHT, y_base)

        # X-axis labels (5 ticks)
        p.setPen(QColor("#666666"))
        for i in range(6):
            t = i * span / 5
            x = _MARGIN_LEFT + int(t / span * plot_w)
            p.drawLine(x, self.height() - _MARGIN_BOTTOM, x, self.height() - _MARGIN_BOTTOM + 4)
            p.drawText(
                x - 20,
                self.height() - _MARGIN_BOTTOM + 6,
                40,
                16,
                Qt.AlignmentFlag.AlignCenter,
                f"{t:.2f}s",
            )

        # Column header
        p.drawText(
            _MARGIN_LEFT,
            4,
            plot_w,
            _MARGIN_TOP - 4,
            Qt.AlignmentFlag.AlignCenter,
            "APCI Frame Timeline  (● I-frame  ● S-frame  ● U-frame)",
        )

        # Draw dots
        for idx, row in enumerate(self._rows):
            rel_t = row.frame.timestamp - self._t0
            x = _MARGIN_LEFT + int(rel_t / span * plot_w)
            y = _MARGIN_TOP + idx * _ROW_H
            color = _FMT_COLORS.get(row.fmt, _FMT_COLORS["?"])
            p.setBrush(color)
            p.setPen(QPen(color.darker(130), 1))
            p.drawEllipse(x - _DOT_R, y - _DOT_R, _DOT_R * 2, _DOT_R * 2)

        p.end()


# ---------------------------------------------------------------------------
# k-w outstanding-window analyser (per flow)
# ---------------------------------------------------------------------------


def _compute_kw_stats(rows: list[_FrameRow]) -> list[dict[str, object]]:
    """Return a list of per-flow dicts with k/w window metrics.

    Keys: flow, total_i, total_s, total_u, max_outstanding, avg_outstanding
    """
    # Track per-flow send/ack queues
    from collections import defaultdict

    # outstanding[flow] = number of I-frames sent but not yet acked
    outstanding: dict[str, int] = defaultdict(int)
    max_out: dict[str, int] = defaultdict(int)
    total_out: dict[str, float] = defaultdict(float)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"I": 0, "S": 0, "U": 0})

    # We track per directional flow (src→dst)
    for row in rows:
        flow = f"{row.src}→{row.dst}"
        counts[flow][row.fmt] = counts[flow].get(row.fmt, 0) + 1

        apdu = row.frame.apdu
        if apdu is None:
            continue

        if row.fmt == "I":
            # Each I-frame increments the sender's outstanding count
            outstanding[flow] += 1
            # N(R) from this I-frame acks frames from the reverse direction
            rev_flow = f"{row.dst}→{row.src}"
            if apdu.recv_seq is not None and outstanding[rev_flow] > 0:
                # Conservative: just clear to 0 on any ack for simplicity
                # (proper tracking would need per-seq bookkeeping)
                nr = apdu.recv_seq
                acked = min(nr, outstanding[rev_flow])
                outstanding[rev_flow] = max(0, outstanding[rev_flow] - acked)
        elif row.fmt == "S":
            # S-frame acks all I-frames up to N(R) in the reverse direction
            rev_flow = f"{row.dst}→{row.src}"
            if apdu.recv_seq is not None:
                acked = min(apdu.recv_seq, outstanding[rev_flow])
                outstanding[rev_flow] = max(0, outstanding[rev_flow] - acked)

        if outstanding[flow] > max_out[flow]:
            max_out[flow] = outstanding[flow]
        total_out[flow] += outstanding[flow]

    results = []
    all_flows = sorted(set(counts.keys()) | set(outstanding.keys()))
    frame_count: dict[str, int] = {}
    for row in rows:
        flow = f"{row.src}→{row.dst}"
        frame_count[flow] = frame_count.get(flow, 0) + 1

    for flow in all_flows:
        fc = frame_count.get(flow, 1)
        results.append(
            {
                "flow": flow,
                "total_i": counts[flow].get("I", 0),
                "total_s": counts[flow].get("S", 0),
                "total_u": counts[flow].get("U", 0),
                "max_outstanding": max_out[flow],
                "avg_outstanding": round(total_out[flow] / max(fc, 1), 2),
            }
        )
    return results


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

        # --- Tab 5: Timeline / k-w analysis (§P4.E.3) ---
        timeline_widget = QWidget()
        timeline_layout = QVBoxLayout(timeline_widget)

        self._timeline_canvas = _TimelineWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._timeline_canvas)
        timeline_layout.addWidget(scroll, stretch=3)

        # k-w outstanding-window table (populated after load completes)
        kw_label = QLabel("k/w Window Analysis (per flow):")
        timeline_layout.addWidget(kw_label)
        self._kw_table = QTableWidget(0, 6)
        self._kw_table.setHorizontalHeaderLabels(
            ["Flow", "I-frames", "S-frames", "U-frames", "Max Outstanding", "Avg Outstanding"]
        )
        self._kw_table.horizontalHeader().setStretchLastSection(True)
        self._kw_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._kw_table.setMaximumHeight(160)
        timeline_layout.addWidget(self._kw_table)
        tabs.addTab(timeline_widget, "Timeline / k-w")

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
        self._timeline_canvas.append(row)

    def _on_load_finished(self, total: int) -> None:
        self._status_lbl.setText(f"Loaded {total} frames from {Path(self._path).name}")
        self._populate_stats()
        self._populate_kw_table()

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
    # Timeline / k-w tab (§P4.E.3)
    # ------------------------------------------------------------------

    def _populate_kw_table(self) -> None:
        rows = self._frames_model.all_rows()
        stats = _compute_kw_stats(rows)
        self._kw_table.setRowCount(0)
        for stat in stats:
            r = self._kw_table.rowCount()
            self._kw_table.setRowCount(r + 1)
            self._kw_table.setItem(r, 0, QTableWidgetItem(str(stat["flow"])))
            for col, key in enumerate(
                ("total_i", "total_s", "total_u", "max_outstanding", "avg_outstanding"), 1
            ):
                item = QTableWidgetItem(str(stat[key]))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._kw_table.setItem(r, col, item)

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
