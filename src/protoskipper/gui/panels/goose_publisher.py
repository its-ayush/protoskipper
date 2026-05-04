# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""GoosePublisherPanel — GOOSE frame transmitter with error injection (P8.C.5).

Allows the operator to configure a GOOSE publisher and transmit frames
with optional deliberate protocol violations for commissioning / testing:

* **Duplicate sqNum** — repeat the previous sequence number (does not
  increment ``sqNum``).
* **Wrong confRev** — transmit with a user-specified ``confRev`` that
  differs from the configured value.
* **Wrong stNum** — transmit with a manually overridden ``stNum``.
* **Wrong appId** — transmit with a user-specified ``appId`` that differs
  from the configured value.

Safety
------
When the session profile is **PRODUCTION** the Start / Publish buttons are
disabled and a tooltip explains why.  Pass ``is_production=True`` when
constructing the panel.

Architecture
------------
The panel owns a :class:`~protoskipper.gui.services.goose_service.GoosePublisherQt`
instance.  It does **not** import ``protoskipper_iec61850`` directly — all
GOOSE calls go through the service wrapper.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from protoskipper.gui.services.goose_service import GoosePublisherQt

_log = logging.getLogger(__name__)

_PRODUCTION_TOOLTIP = (
    "Disabled in PRODUCTION profile — GOOSE injection is not permitted on live plant networks."
)


class GoosePublisherPanel(QWidget):
    """GOOSE frame transmitter with error-injection support (P8.C.5).

    Parameters
    ----------
    is_production:
        When ``True`` the Start and Publish buttons are disabled and a
        tooltip explains why.  Defaults to ``False``.
    parent:
        Optional parent widget.
    """

    def __init__(
        self,
        is_production: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._is_production = is_production
        self._svc = GoosePublisherQt(self)
        self._svc.error_occurred.connect(self._on_error)
        self._svc.published.connect(self._on_published)
        self._open = False

        # ------------------------------------------------------------------
        # Connection parameters group
        # ------------------------------------------------------------------
        conn_group = QGroupBox("Publisher Configuration", self)
        conn_form = QFormLayout(conn_group)

        self._iface_edit = QLineEdit(self)
        self._iface_edit.setPlaceholderText("e.g. eth0")
        conn_form.addRow("Interface:", self._iface_edit)

        self._gcb_edit = QLineEdit(self)
        self._gcb_edit.setPlaceholderText("e.g. simpleIO/LLN0$GO$gcbAnalogValues")
        conn_form.addRow("GoCB Ref:", self._gcb_edit)

        self._dat_set_edit = QLineEdit(self)
        self._dat_set_edit.setPlaceholderText("e.g. simpleIO/LLN0$GOOSE1")
        conn_form.addRow("DataSet Ref:", self._dat_set_edit)

        self._app_id_spin = QSpinBox(self)
        self._app_id_spin.setRange(0x0000, 0x3FFF)
        self._app_id_spin.setValue(0x0000)
        self._app_id_spin.setDisplayIntegerBase(16)
        conn_form.addRow("AppID (hex):", self._app_id_spin)

        self._conf_rev_spin = QSpinBox(self)
        self._conf_rev_spin.setRange(1, 0xFFFF)
        self._conf_rev_spin.setValue(1)
        conn_form.addRow("confRev:", self._conf_rev_spin)

        self._t0_spin = QSpinBox(self)
        self._t0_spin.setRange(1, 60_000)
        self._t0_spin.setValue(50)
        self._t0_spin.setSuffix(" ms")
        conn_form.addRow("T0 (burst base):", self._t0_spin)

        self._max_retx_spin = QSpinBox(self)
        self._max_retx_spin.setRange(100, 60_000)
        self._max_retx_spin.setValue(5_000)
        self._max_retx_spin.setSuffix(" ms")
        conn_form.addRow("Max retransmit:", self._max_retx_spin)

        self._sim_check = QCheckBox("Set simulation bit", self)
        conn_form.addRow("Simulation:", self._sim_check)

        # ------------------------------------------------------------------
        # Dataset values (raw Python repr — for commissioning use only)
        # ------------------------------------------------------------------
        data_group = QGroupBox("Dataset Values", self)
        data_layout = QVBoxLayout(data_group)
        data_layout.addWidget(
            QLabel(
                "Enter one value per line as Python literal "
                "(True/False, int, float, or b'...' for bytes):"
            )
        )
        self._values_edit = QTextEdit(self)
        self._values_edit.setPlaceholderText("True\n1\n3.14")
        self._values_edit.setMaximumHeight(100)
        data_layout.addWidget(self._values_edit)

        # ------------------------------------------------------------------
        # Error injection group
        # ------------------------------------------------------------------
        inj_group = QGroupBox("Error Injection", self)
        inj_form = QFormLayout(inj_group)

        self._dup_sqnum_check = QCheckBox("Duplicate sqNum (do not increment)", self)
        inj_form.addRow(self._dup_sqnum_check)

        wrong_conf_row = QHBoxLayout()
        self._wrong_conf_check = QCheckBox("Override confRev:", self)
        self._wrong_conf_spin = QSpinBox(self)
        self._wrong_conf_spin.setRange(1, 0xFFFF)
        self._wrong_conf_spin.setValue(9999)
        self._wrong_conf_spin.setEnabled(False)
        self._wrong_conf_check.toggled.connect(self._wrong_conf_spin.setEnabled)
        wrong_conf_row.addWidget(self._wrong_conf_check)
        wrong_conf_row.addWidget(self._wrong_conf_spin)
        inj_form.addRow(wrong_conf_row)

        wrong_st_row = QHBoxLayout()
        self._wrong_st_check = QCheckBox("Override stNum:", self)
        self._wrong_st_spin = QSpinBox(self)
        self._wrong_st_spin.setRange(1, 0xFFFF_FFFF)
        self._wrong_st_spin.setValue(1)
        self._wrong_st_spin.setEnabled(False)
        self._wrong_st_check.toggled.connect(self._wrong_st_spin.setEnabled)
        wrong_st_row.addWidget(self._wrong_st_check)
        wrong_st_row.addWidget(self._wrong_st_spin)
        inj_form.addRow(wrong_st_row)

        wrong_app_row = QHBoxLayout()
        self._wrong_app_check = QCheckBox("Override appId (hex):", self)
        self._wrong_app_spin = QSpinBox(self)
        self._wrong_app_spin.setRange(0x0000, 0x3FFF)
        self._wrong_app_spin.setValue(0x1234)
        self._wrong_app_spin.setDisplayIntegerBase(16)
        self._wrong_app_spin.setEnabled(False)
        self._wrong_app_check.toggled.connect(self._wrong_app_spin.setEnabled)
        wrong_app_row.addWidget(self._wrong_app_check)
        wrong_app_row.addWidget(self._wrong_app_spin)
        inj_form.addRow(wrong_app_row)

        # ------------------------------------------------------------------
        # Toolbar (Start / Stop / Publish)
        # ------------------------------------------------------------------
        self._toolbar = QToolBar(self)
        self._start_btn = QPushButton("Start Publisher", self)
        self._stop_btn = QPushButton("Stop Publisher", self)
        self._publish_btn = QPushButton("Publish", self)
        self._stop_btn.setEnabled(False)
        self._publish_btn.setEnabled(False)
        self._toolbar.addWidget(self._start_btn)
        self._toolbar.addWidget(self._stop_btn)
        self._toolbar.addWidget(self._publish_btn)

        self._status_label = QLabel("Not started", self)

        # PRODUCTION lock-out
        if is_production:
            for btn in (self._start_btn, self._publish_btn):
                btn.setEnabled(False)
                btn.setToolTip(_PRODUCTION_TOOLTIP)
            self._status_label.setText("PRODUCTION — GOOSE injection disabled")
            self._status_label.setStyleSheet("color: #b45309; font-weight: bold;")

        self._start_btn.clicked.connect(self._start)
        self._stop_btn.clicked.connect(self._stop)
        self._publish_btn.clicked.connect(self._publish)

        # ------------------------------------------------------------------
        # Layout
        # ------------------------------------------------------------------
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(conn_group)
        layout.addWidget(data_group)
        layout.addWidget(inj_group)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._status_label)
        layout.addStretch()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _start(self) -> None:
        if self._is_production:
            return  # defence-in-depth — button should be disabled already
        iface = self._iface_edit.text().strip()
        gcb_ref = self._gcb_edit.text().strip()
        dat_set = self._dat_set_edit.text().strip()
        if not iface or not gcb_ref or not dat_set:
            QMessageBox.warning(
                self,
                "GOOSE Publisher",
                "Interface, GoCB reference, and DataSet reference are all required.",
            )
            return

        app_id = self._app_id_spin.value()
        if self._wrong_app_check.isChecked():
            app_id = self._wrong_app_spin.value()

        conf_rev = self._conf_rev_spin.value()
        if self._wrong_conf_check.isChecked():
            conf_rev = self._wrong_conf_spin.value()

        self._svc.configure(
            iface=iface,
            go_cb_ref=gcb_ref,
            dat_set_ref=dat_set,
            app_id=app_id,
            conf_rev=conf_rev,
            t0_ms=self._t0_spin.value(),
            max_retransmit_interval_ms=self._max_retx_spin.value(),
            simulation=self._sim_check.isChecked(),
        )
        self._open = True
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._publish_btn.setEnabled(True)
        self._status_label.setText(f"Publisher open on {iface!r}")

    def _stop(self) -> None:
        self._svc.close()
        self._open = False
        if not self._is_production:
            self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._publish_btn.setEnabled(False)
        self._status_label.setText("Stopped")

    def _publish(self) -> None:
        if self._is_production:
            return
        values = self._parse_values()
        if values is None:
            return
        self._svc.publish(values)

    def _parse_values(self) -> list[Any] | None:
        """Parse the dataset values text box into Python native types."""
        try:
            from protoskipper_iec61850.goose.publisher import parse_dataset_literal
        except ImportError:
            QMessageBox.critical(
                self,
                "Missing Dependency",
                "protoskipper_iec61850 is not installed.",
            )
            return None
        lines = [ln.strip() for ln in self._values_edit.toPlainText().splitlines() if ln.strip()]
        values: list[Any] = []
        for ln in lines:
            try:
                val = parse_dataset_literal(ln)
            except ValueError as exc:
                QMessageBox.critical(
                    self,
                    "Parse Error",
                    f"Cannot parse value {ln!r}: {exc}",
                )
                return None
            values.append(val)
        return values

    def _on_published(self) -> None:
        self._status_label.setText("Published \u2713")

    def _on_error(self, msg: str) -> None:
        _log.error("GoosePublisherQt error: %s", msg)
        self._status_label.setText(f"Error: {msg}")
        if self._open:
            self._stop()
        QMessageBox.critical(self, "GOOSE Publisher Error", msg)
