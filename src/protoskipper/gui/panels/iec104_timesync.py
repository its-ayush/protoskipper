# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 Time-sync Panel (§5.9 of IEC104_PLAN.md).

One-shot clock-sync, periodic auto-sync, and a drift table showing
RTU vs laptop time with quality flags (IV, SU).

Production profile guard: clock-sync is enabled in LAB and COMMISSIONING;
in PRODUCTION the operator must manually approve via the existing
SafetyConfirm flow which is already baked into the write path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import Access, ObjectRef
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId

_logger = logging.getLogger(__name__)


class Iec104TimeSyncPanel(QWidget):
    """Time-sync panel for IEC 104 sessions (§5.9)."""

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._session_manager = session_manager
        self._session_id: SessionId | None = None
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._do_sync)
        self._build_ui()
        state.read_completed.connect(self._on_read_completed)
        state.write_completed.connect(self._on_write_completed)
        state.error_raised.connect(self._on_error)

    def set_session(self, session_id: str | None) -> None:
        self._session_id = SessionId(session_id) if session_id else None
        self._drift_table.setRowCount(0)
        self._status_lbl.setText("No session selected")
        self._auto_timer.stop()
        self._auto_check.setChecked(False)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # ---- One-shot sync ----
        oneshot_box = QGroupBox("One-shot Clock Synchronisation (C_CS_NA_1 type 103)")
        oneshot_layout = QHBoxLayout(oneshot_box)
        self._sync_btn = QPushButton("Sync Now (UTC)")
        self._sync_btn.setToolTip("Sends current laptop UTC as CP56Time2a to the RTU")
        self._sync_btn.clicked.connect(self._do_sync)
        oneshot_layout.addWidget(self._sync_btn)
        self._status_lbl = QLabel("—")
        oneshot_layout.addWidget(self._status_lbl, 1)
        layout.addWidget(oneshot_box)

        # ---- Periodic sync ----
        auto_box = QGroupBox("Periodic Auto-sync")
        auto_layout = QFormLayout(auto_box)
        self._auto_check = QCheckBox("Enable periodic sync")
        self._auto_check.toggled.connect(self._on_auto_toggled)
        auto_layout.addRow("", self._auto_check)
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 86400)
        self._interval_spin.setValue(60)
        self._interval_spin.setSuffix(" s")
        self._interval_spin.valueChanged.connect(self._update_timer_interval)
        auto_layout.addRow("Interval:", self._interval_spin)
        layout.addWidget(auto_box)

        # ---- Drift table ----
        drift_box = QGroupBox("RTU Timestamps (M_*_TB_1) — drift from laptop clock")
        drift_layout = QVBoxLayout(drift_box)
        self._drift_table = QTableWidget(0, 6)
        self._drift_table.setHorizontalHeaderLabels(
            ["IOA", "Type", "RTU Time", "Laptop Time", "Drift (ms)", "Quality"]
        )
        self._drift_table.verticalHeader().setVisible(False)
        self._drift_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._drift_table.setAlternatingRowColors(True)
        drift_layout.addWidget(self._drift_table)
        layout.addWidget(drift_box)
        layout.addStretch()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_session(self) -> SessionId | None:
        if self._session_id is None:
            QMessageBox.warning(self, "No session", "Select an open IEC 104 session first.")
            return None
        info = self._state.session(self._session_id)
        if info is None or not info.is_open:
            QMessageBox.warning(self, "Session closed", "The selected session is not open.")
            return None
        return self._session_id

    def _do_sync(self) -> None:
        sid = self._require_session()
        if sid is None:
            self._auto_timer.stop()
            self._auto_check.setChecked(False)
            return
        info = self._state.session(sid)
        assert info is not None
        now_utc = datetime.now(timezone.utc)
        ref = ObjectRef(
            device=info.device,
            object_id="C_CS_NA_1:0",
            data_type="datetime",
            access=Access.READ_WRITE,
        )
        self._session_manager.prepare_write(sid, ref, now_utc.isoformat())
        self._status_lbl.setText(f"Sent at {now_utc.strftime('%H:%M:%S.%f')[:-3]} UTC")

    def _on_auto_toggled(self, enabled: bool) -> None:
        if enabled:
            self._do_sync()
            self._update_timer_interval(self._interval_spin.value())
            self._auto_timer.start()
        else:
            self._auto_timer.stop()

    def _update_timer_interval(self, secs: int) -> None:
        self._auto_timer.setInterval(secs * 1000)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_read_completed(self, session_id: str, result: Any) -> None:
        if self._session_id and SessionId(session_id) != self._session_id:
            return
        # Only show time-tagged types (TB_ suffix)
        type_str = str(getattr(result, "asdu_type", "") or "")
        if "_TB_" not in type_str and "_TF_" not in type_str:
            return
        meta = getattr(result, "metadata", {}) or {}
        rtu_ts_raw = getattr(result, "timestamp", None) or meta.get("timestamp")
        if rtu_ts_raw is None:
            return
        rtu_ts: datetime | None = None
        if isinstance(rtu_ts_raw, datetime):
            rtu_ts = rtu_ts_raw.astimezone(timezone.utc)
        elif isinstance(rtu_ts_raw, str):
            import contextlib

            with contextlib.suppress(ValueError):
                rtu_ts = datetime.fromisoformat(rtu_ts_raw).astimezone(timezone.utc)
        if rtu_ts is None:
            return
        now = datetime.now(timezone.utc)
        drift_ms = (now - rtu_ts).total_seconds() * 1000
        ioa = str(meta.get("ioa", getattr(result, "ioa", "?")))
        quality = int(getattr(result, "quality", 0) or 0)
        q_str = _quality_str(quality)
        # Find existing row by IOA + type or add a new one
        row = self._find_or_add_drift_row(ioa, type_str)
        self._drift_table.setItem(row, 0, QTableWidgetItem(ioa))
        self._drift_table.setItem(row, 1, QTableWidgetItem(type_str))
        self._drift_table.setItem(row, 2, QTableWidgetItem(rtu_ts.strftime("%H:%M:%S.%f")[:-3]))
        self._drift_table.setItem(row, 3, QTableWidgetItem(now.strftime("%H:%M:%S.%f")[:-3]))
        drift_item = QTableWidgetItem(f"{drift_ms:.1f}")
        drift_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._drift_table.setItem(row, 4, drift_item)
        q_item = QTableWidgetItem(q_str)
        if quality & 0x80:  # IV flag
            q_item.setForeground(Qt.GlobalColor.red)
        self._drift_table.setItem(row, 5, q_item)

    def _find_or_add_drift_row(self, ioa: str, type_str: str) -> int:
        for i in range(self._drift_table.rowCount()):
            if (
                self._drift_table.item(i, 0) is not None
                and self._drift_table.item(i, 0).text() == ioa
                and self._drift_table.item(i, 1) is not None
                and self._drift_table.item(i, 1).text() == type_str
            ):
                return i
        row = self._drift_table.rowCount()
        self._drift_table.setRowCount(row + 1)
        return row

    def _on_write_completed(self, session_id: str, result: Any) -> None:
        if self._session_id and SessionId(session_id) != self._session_id:
            return
        meta = getattr(result, "metadata", {}) or {}
        asdu_type = str(meta.get("asdu_type", "?"))
        if "C_CS_NA_1" not in asdu_type:
            return
        neg = meta.get("reply_negative", False)
        self._status_lbl.setText(
            f"Sync {'FAILED (P/N=1)' if neg else 'accepted'} — "
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
        )

    def _on_error(self, operation: str, message: str) -> None:
        if "write" in operation:
            self._status_lbl.setText(f"ERROR: {message[:80]}")


def _quality_str(flags: int) -> str:
    _quality_flag_map = {0x01: "OV", 0x10: "BL", 0x20: "SB", 0x40: "NT", 0x80: "IV"}
    if flags == 0:
        return "OK"
    return " ".join(v for bit, v in sorted(_quality_flag_map.items()) if flags & bit)
