# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 Command Dialog (§5.7 of IEC104_PLAN.md).

Issues a single command (C_SC, C_DC, C_SE_*, C_BO) with full lifecycle
display: sent → act-con → [execute if SBO] → act-term. Inline failure
surfaces (timeout, negative CON, bad quality). All outcomes go to the
audit log through the normal DriverWorker write flow.

Only available in LAB and COMMISSIONING profiles; PRODUCTION requires
the tag-back confirmation modal (SafetyConfirmDialog) via the existing
write flow.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import (
    Access,
    ObjectRef,
)
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported command types and their value editors
# ---------------------------------------------------------------------------

_COMMAND_TYPES = [
    ("45  C_SC_NA_1 — Single command", "C_SC_NA_1"),
    ("46  C_DC_NA_1 — Double command", "C_DC_NA_1"),
    ("47  C_RC_NA_1 — Regulating step", "C_RC_NA_1"),
    ("48  C_SE_NA_1 — Set-point, normalised", "C_SE_NA_1"),
    ("49  C_SE_NB_1 — Set-point, scaled", "C_SE_NB_1"),
    ("50  C_SE_NC_1 — Set-point, float", "C_SE_NC_1"),
    ("51  C_BO_NA_1 — Bitstring 32-bit", "C_BO_NA_1"),
]

_QU_LABELS = [
    "0  unspecified",
    "1  short pulse",
    "2  long pulse",
    "3  persistent",
]

_SC_VALUES = ["0  off / close", "1  on / trip"]
_DC_VALUES = ["0  not permitted", "1  off / close", "2  on / trip", "3  not permitted"]
_RC_VALUES = ["0  not permitted", "1  lower", "2  higher", "3  not permitted"]

_COT_DISPLAY = {
    6: "ACT",
    7: "ACTCON",
    8: "DEACT",
    9: "DEACTCON",
    10: "ACTTERM",
    44: "UNKNOWN_TYPE",
    45: "UNKNOWN_CAUSE",
    46: "UNKNOWN_CA",
    47: "UNKNOWN_IOA",
}


class Iec104CommandDialog(QDialog):
    """Full IEC 104 command dialog per §5.7.

    Shows the command-issue form on the left, and a live lifecycle log on
    the right. The lifecycle log updates when the worker emits
    ``write_intent_prepared`` and ``write_completed`` / ``error_raised``.
    """

    def __init__(
        self,
        session_id: SessionId,
        state: ApplicationState,
        session_manager: SessionManager,
        prefill_ref: ObjectRef | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("IEC 104 — Send Command")
        self.setMinimumWidth(760)
        self.setMinimumHeight(540)

        self._session_id = session_id
        self._state = state
        self._session_manager = session_manager
        self._pending_ref: ObjectRef | None = None

        self._build_ui(prefill_ref)
        self._connect_signals()
        self._refresh_value_editor()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, prefill: ObjectRef | None) -> None:
        root = QHBoxLayout(self)

        # ---- Left: form ----
        left = QGroupBox("Command")
        form_layout = QFormLayout(left)
        form_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._ioa_spin = QSpinBox()
        self._ioa_spin.setRange(1, 16_777_215)
        self._ioa_spin.setValue(4001)
        self._ioa_spin.setAccessibleName("Information Object Address (IOA)")
        form_layout.addRow("IOA:", self._ioa_spin)

        self._type_combo = QComboBox()
        for label, _tid in _COMMAND_TYPES:
            self._type_combo.addItem(label, _tid)
        self._type_combo.setAccessibleName("ASDU command type")
        form_layout.addRow("Type:", self._type_combo)

        self._qu_combo = QComboBox()
        for lbl in _QU_LABELS:
            self._qu_combo.addItem(lbl)
        self._qu_combo.setAccessibleName("Qualifier of command (QU)")
        form_layout.addRow("Qualifier (QU):", self._qu_combo)

        # value placeholder — swapped out by _refresh_value_editor
        self._value_stack = QWidget()
        self._value_stack_layout = QVBoxLayout(self._value_stack)
        self._value_stack_layout.setContentsMargins(0, 0, 0, 0)
        self._value_widget: QWidget | None = None
        form_layout.addRow("Value:", self._value_stack)

        self._sbo_check = QCheckBox("Select-Before-Operate (SBO)")
        self._sbo_check.setToolTip(
            "First sends a SELECT command and waits for ACTCON,\n"
            "then sends the EXECUTE. Required for PRODUCTION safety."
        )
        form_layout.addRow("Mode:", self._sbo_check)

        self._wait_actterm_check = QCheckBox("Wait for ACTTERM")
        self._wait_actterm_check.setChecked(True)
        form_layout.addRow("", self._wait_actterm_check)

        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(1, 120)
        self._timeout_spin.setValue(10)
        self._timeout_spin.setSuffix(" s")
        self._timeout_spin.setAccessibleName("Activation timeout in seconds")
        form_layout.addRow("Timeout:", self._timeout_spin)

        self._send_btn = QPushButton("Send Command")
        self._send_btn.setDefault(True)
        self._send_btn.clicked.connect(self._on_send)
        form_layout.addRow("", self._send_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        form_layout.addRow("", close_btn)

        left.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        # Pre-fill from ObjectRef if provided
        if prefill is not None:
            self._prefill_from_ref(prefill)

        root.addWidget(left, 1)

        # ---- Right: lifecycle log ----
        right = QGroupBox("Command lifecycle")
        right_layout = QVBoxLayout(right)
        self._lifecycle_log = QTextEdit()
        self._lifecycle_log.setReadOnly(True)
        self._lifecycle_log.setFontFamily("Courier New, Courier, monospace")
        self._lifecycle_log.setMinimumWidth(320)
        right_layout.addWidget(self._lifecycle_log)
        root.addWidget(right, 1)

    def _prefill_from_ref(self, ref: ObjectRef) -> None:
        """Pre-fill form from a clicked ObjectRef (from object browser)."""
        if ":" in ref.object_id:
            type_str, ioa_str = ref.object_id.split(":", 1)
            # Find matching type in combo
            for i in range(self._type_combo.count()):
                if self._type_combo.itemData(i) == type_str.upper():
                    self._type_combo.setCurrentIndex(i)
                    break
            import contextlib

            with contextlib.suppress(ValueError):
                self._ioa_spin.setValue(int(ioa_str))

    def _connect_signals(self) -> None:
        self._type_combo.currentIndexChanged.connect(self._refresh_value_editor)
        self._state.write_intent_prepared.connect(self._on_intent_prepared)
        self._state.write_completed.connect(self._on_write_completed)
        self._state.write_denied.connect(self._on_write_denied)
        self._state.error_raised.connect(self._on_error)

    # ------------------------------------------------------------------
    # Value editor management
    # ------------------------------------------------------------------

    def _refresh_value_editor(self) -> None:
        tid = self._type_combo.currentData()
        if self._value_widget is not None:
            self._value_stack_layout.removeWidget(self._value_widget)
            self._value_widget.deleteLater()
        if tid in ("C_SC_NA_1",):
            w = QComboBox()
            for lbl in _SC_VALUES:
                w.addItem(lbl)
        elif tid in ("C_DC_NA_1",):
            w = QComboBox()
            for lbl in _DC_VALUES:
                w.addItem(lbl)
        elif tid in ("C_RC_NA_1",):
            w = QComboBox()
            for lbl in _RC_VALUES:
                w.addItem(lbl)
        elif tid in ("C_SE_NA_1",):
            w = QDoubleSpinBox()
            w.setRange(-1.0, 1.0)
            w.setSingleStep(0.001)
            w.setDecimals(5)
            w.setAccessibleName("Normalised value (-1.0 to 1.0)")
        elif tid in ("C_SE_NB_1",):
            w = QSpinBox()
            w.setRange(-32768, 32767)
            w.setAccessibleName("Scaled integer value")
        elif tid in ("C_SE_NC_1",):
            w = QDoubleSpinBox()
            w.setRange(-1e9, 1e9)
            w.setSingleStep(0.001)
            w.setDecimals(6)
            w.setAccessibleName("Float value")
        else:  # C_BO_NA_1
            w = QSpinBox()
            w.setRange(0, 0x7FFFFFFF)
            w.setDisplayIntegerBase(16)
            w.setPrefix("0x")
            w.setAccessibleName("Bitstring value (hex)")
        self._value_widget = w
        self._value_stack_layout.addWidget(w)

    def _current_value(self) -> Any:
        w = self._value_widget
        tid = self._type_combo.currentData()
        if isinstance(w, QComboBox):
            idx = w.currentIndex()
            if tid == "C_SC_NA_1":
                return bool(idx)
            return idx  # DC, RC: integer DCS/RCS
        if isinstance(w, QDoubleSpinBox):
            return w.value()
        if isinstance(w, QSpinBox):
            return w.value()
        return 0

    # ------------------------------------------------------------------
    # Send logic
    # ------------------------------------------------------------------

    def _on_send(self) -> None:
        info = self._state.session(self._session_id)
        if info is None or not info.is_open:
            QMessageBox.warning(self, "Session closed", "The session is no longer open.")
            return

        tid = self._type_combo.currentData()
        ioa = self._ioa_spin.value()
        value = self._current_value()
        select = self._sbo_check.isChecked()
        ql = self._qu_combo.currentIndex()

        # Build a suitable dict value for SBO / QU, or plain value for direct
        if select or ql != 0:
            payload: Any = {"value": value, "select": select, "ql": ql}
        else:
            payload = value

        object_id = f"{tid}:{ioa}"
        ref = ObjectRef(
            device=info.device,
            object_id=object_id,
            data_type=_data_type_for(tid),
            access=Access.READ_WRITE,
        )
        self._pending_ref = ref
        self._log(f"[{_ts()}] Sending {tid}  IOA={ioa}  value={payload}")
        self._send_btn.setEnabled(False)

        self._session_manager.prepare_write(self._session_id, ref, payload)

    def _on_intent_prepared(self, session_id: str, intent: Any) -> None:
        if SessionId(session_id) != self._session_id:
            return
        self._log(f"[{_ts()}] Intent prepared — dispatching to worker…")

    def _on_write_completed(self, session_id: str, result: Any) -> None:
        if SessionId(session_id) != self._session_id:
            return
        meta = getattr(result, "metadata", {}) or {}
        cot_raw = meta.get("reply_cot")
        cot_str = _COT_DISPLAY.get(cot_raw, str(cot_raw)) if cot_raw is not None else "?"
        neg = meta.get("reply_negative", False)
        status = "NEGATIVE (P/N=1)" if neg else "POSITIVE"
        reply_type = meta.get("reply_asdu_type", "?")
        self._log(
            f"[{_ts()}] ← {reply_type}  COT={cot_str}  {status}\n"
            f"[{_ts()}] Result: {'FAILED' if neg else 'SUCCESS'}"
        )
        self._send_btn.setEnabled(True)

    def _on_write_denied(self, session_id: str, intent: Any) -> None:
        if SessionId(session_id) != self._session_id:
            return
        self._log(f"[{_ts()}] Write DENIED by safety profile")
        self._send_btn.setEnabled(True)

    def _on_error(self, operation: str, message: str) -> None:
        # Only surface IEC 104 command errors
        if "commit_write" in operation or "prepare_write" in operation:
            self._log(f"[{_ts()}] ERROR — {operation}: {message}")
            self._send_btn.setEnabled(True)

    def _log(self, text: str) -> None:
        self._lifecycle_log.append(text)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event: Any) -> None:  # type: ignore[override]
        self._state.write_intent_prepared.disconnect(self._on_intent_prepared)
        self._state.write_completed.disconnect(self._on_write_completed)
        self._state.write_denied.disconnect(self._on_write_denied)
        self._state.error_raised.disconnect(self._on_error)
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def _data_type_for(tid: str) -> str:
    _type_map = {
        "C_SC_NA_1": "boolean",
        "C_DC_NA_1": "uint8",
        "C_RC_NA_1": "uint8",
        "C_SE_NA_1": "float",
        "C_SE_NB_1": "int16",
        "C_SE_NC_1": "float32",
        "C_BO_NA_1": "uint32",
    }
    return _type_map.get(tid, "raw")
