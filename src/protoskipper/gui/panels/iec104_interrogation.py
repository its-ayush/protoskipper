# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 Interrogation Panel (§5.8 of IEC104_PLAN.md).

Provides purpose-built buttons for every IEC 104 ceremony command:
General Interrogation, Counter Interrogation, Read (single IOA),
Test command, Reset Process (LAB-only), Delay Acquisition.

Each ceremony emits a write_intent → write_completed pair through the
normal DriverWorker flow so outcomes land in the audit log.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import Access, ObjectRef, SessionProfile
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId

_logger = logging.getLogger(__name__)

_GI_GROUPS = ["Global (QOI=20)"] + [f"Group {i} (QOI={20 + i})" for i in range(1, 17)]
_CI_GROUPS = ["Global (QCC RQT=5)"] + [f"Group {i} (QCC RQT={i})" for i in range(1, 5)]
_CI_FREEZE = [
    "1  Read (no freeze)",
    "2  Freeze without reset",
    "3  Freeze and reset",
    "4  Reset only",
]


class Iec104InterrogationPanel(QWidget):
    """Panel hosting GI, CI, Read, Test, Reset, Delay buttons per §5.8."""

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
        self._build_ui()
        state.write_completed.connect(self._on_write_completed)
        state.error_raised.connect(self._on_error)

    def set_session(self, session_id: str | None) -> None:
        self._session_id = SessionId(session_id) if session_id else None
        self._log.clear()
        self._refresh_buttons()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # ---- GI ----
        gi_box = QGroupBox("General Interrogation (C_IC_NA_1 type 100)")
        gi_layout = QFormLayout(gi_box)
        self._gi_group_combo = QComboBox()
        for g in _GI_GROUPS:
            self._gi_group_combo.addItem(g)
        gi_layout.addRow("Group:", self._gi_group_combo)
        self._gi_btn = QPushButton("Send GI")
        self._gi_btn.clicked.connect(self._on_gi)
        gi_layout.addRow("", self._gi_btn)
        layout.addWidget(gi_box)

        # ---- CI ----
        ci_box = QGroupBox("Counter Interrogation (C_CI_NA_1 type 101)")
        ci_layout = QFormLayout(ci_box)
        self._ci_group_combo = QComboBox()
        for g in _CI_GROUPS:
            self._ci_group_combo.addItem(g)
        ci_layout.addRow("Group:", self._ci_group_combo)
        self._ci_freeze_combo = QComboBox()
        for f in _CI_FREEZE:
            self._ci_freeze_combo.addItem(f)
        ci_layout.addRow("Freeze:", self._ci_freeze_combo)
        self._ci_btn = QPushButton("Send CI")
        self._ci_btn.clicked.connect(self._on_ci)
        ci_layout.addRow("", self._ci_btn)
        layout.addWidget(ci_box)

        # ---- Read ----
        rd_box = QGroupBox("Read Command (C_RD_NA_1 type 102)")
        rd_layout = QFormLayout(rd_box)
        self._rd_ioa_spin = QSpinBox()
        self._rd_ioa_spin.setRange(1, 16_777_215)
        self._rd_ioa_spin.setValue(1001)
        self._rd_ioa_spin.setAccessibleName("IOA to read")
        rd_layout.addRow("IOA:", self._rd_ioa_spin)
        self._rd_btn = QPushButton("Send Read")
        self._rd_btn.clicked.connect(self._on_read)
        rd_layout.addRow("", self._rd_btn)
        layout.addWidget(rd_box)

        # ---- Test / Reset / Delay (all in one box) ----
        misc_box = QGroupBox("System commands")
        misc_layout = QHBoxLayout(misc_box)

        self._test_btn = QPushButton("Test (C_TS_NA_1)")
        self._test_btn.setToolTip("Issues C_TS_NA_1 type 104 — protocol health check")
        self._test_btn.clicked.connect(self._on_test)
        misc_layout.addWidget(self._test_btn)

        self._reset_btn = QPushButton("Reset process (C_RP_NA_1)")
        self._reset_btn.setToolTip("Issues C_RP_NA_1 type 105. LAB/COMMISSIONING only.")
        self._reset_btn.clicked.connect(self._on_reset)
        misc_layout.addWidget(self._reset_btn)

        self._delay_btn = QPushButton("Delay acq. (C_CD_NA_1)")
        self._delay_btn.setToolTip("Issues C_CD_NA_1 type 106 — delay acquisition command")
        self._delay_btn.clicked.connect(self._on_delay)
        misc_layout.addWidget(self._delay_btn)
        layout.addWidget(misc_box)

        # ---- Log ----
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(160)
        self._log.setFontFamily("Courier New, Courier, monospace")
        layout.addWidget(self._log)
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

    def _ref(self, type_str: str, ioa: int = 0) -> ObjectRef | None:
        sid = self._require_session()
        if sid is None:
            return None
        info = self._state.session(sid)
        assert info is not None
        return ObjectRef(
            device=info.device,
            object_id=f"{type_str}:{ioa}",
            data_type="system",
            access=Access.READ_WRITE,
        )

    def _send(self, type_str: str, ioa: int, value: Any) -> None:
        sid = self._require_session()
        if sid is None:
            return
        ref = self._ref(type_str, ioa)
        if ref is None:
            return
        self._append_log(f"→ {type_str} IOA={ioa} value={value}")
        self._session_manager.prepare_write(sid, ref, value)

    def _refresh_buttons(self) -> None:
        """Enable/disable buttons based on session + profile."""
        sid = self._session_id
        info = self._state.session(sid) if sid else None
        active = info is not None and info.is_open
        for btn in (self._gi_btn, self._ci_btn, self._rd_btn, self._test_btn, self._delay_btn):
            btn.setEnabled(active)
        # Reset process: LAB + COMMISSIONING only
        is_lab_or_commissioning = (
            active
            and info is not None
            and info.profile in (SessionProfile.LAB, SessionProfile.COMMISSIONING)
        )
        self._reset_btn.setEnabled(is_lab_or_commissioning)

    def _append_log(self, msg: str) -> None:
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        self._log.append(f"[{ts}] {msg}")

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _on_gi(self) -> None:
        idx = self._gi_group_combo.currentIndex()
        qoi = 20 + idx  # 20=global, 21..36=group 1..16
        self._send("C_IC_NA_1", 0, qoi)

    def _on_ci(self) -> None:
        g_idx = self._ci_group_combo.currentIndex()
        frz_idx = self._ci_freeze_combo.currentIndex() + 1  # 1..4
        # QCC = FRZ<<6 | RQT
        rqt = 5 if g_idx == 0 else g_idx  # global = 5
        qcc = (frz_idx << 6) | rqt
        self._send("C_CI_NA_1", 0, qcc)

    def _on_read(self) -> None:
        ioa = self._rd_ioa_spin.value()
        self._send("C_RD_NA_1", ioa, 0)

    def _on_test(self) -> None:
        self._send("C_TS_NA_1", 0, 0)

    def _on_reset(self) -> None:
        self._send("C_RP_NA_1", 0, 1)

    def _on_delay(self) -> None:
        self._send("C_CD_NA_1", 0, 0)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_write_completed(self, session_id: str, result: Any) -> None:
        if self._session_id and SessionId(session_id) != self._session_id:
            return
        meta = getattr(result, "metadata", {}) or {}
        asdu_type = meta.get("asdu_type", "?")
        cot = meta.get("reply_cot", "?")
        neg = meta.get("reply_negative", False)
        self._append_log(f"← {asdu_type} COT={cot} {'NEGATIVE' if neg else 'OK'}")

    def _on_error(self, operation: str, message: str) -> None:
        if "write" in operation:
            self._append_log(f"ERROR {operation}: {message}")
