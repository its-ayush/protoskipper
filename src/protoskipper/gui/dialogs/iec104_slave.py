# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""New IEC 104 Slave (simulator) setup dialog (§5.2 of IEC104_PLAN.md).

Collects bind address, port, CA, TLS settings, point-list file, and
spontaneous-event options.  Returns a :class:`SlaveRequest` dataclass;
the caller (MainWindow) creates the actual :class:`Iec104SlaveServer`.

Production profile guard: slave simulators are disabled in PRODUCTION to
prevent accidentally impersonating an RTU on a live network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import SessionProfile

_logger = logging.getLogger(__name__)

_SPONTANEOUS_OPTIONS = [
    ("Off — no spontaneous events", "off"),
    ("Periodic — fixed interval", "periodic"),
    ("Random — random interval", "random"),
]


@dataclass(frozen=True)
class SlaveRequest:
    """Validated parameters from the slave-setup dialog."""

    bind_address: str
    port: int
    ca: int
    max_clients: int
    tls: bool
    tls_cert_path: str  # path to combined PEM (cert + key)
    tls_ca_path: str  # trust roots for client-cert verification
    require_client_cert: bool
    gi_on_receive: bool
    auto_clock_sync: bool
    spontaneous_mode: str  # "off" | "periodic" | "random"
    spontaneous_interval_ms: int
    point_list_path: str


class NewSlaveDialog(QDialog):
    """Dialog for starting an IEC 104 slave (simulator) server.

    Per §5.2: in PRODUCTION profile this dialog refuses to open.
    """

    def __init__(
        self,
        profile: SessionProfile = SessionProfile.LAB,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("IEC 104 — New Slave Simulator")
        self.setMinimumWidth(520)
        self._profile = profile
        self._req: SlaveRequest | None = None
        self._build_ui()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        if self._profile is SessionProfile.PRODUCTION:
            outer.addWidget(
                QLabel(
                    "<b>Slave simulator is disabled in PRODUCTION profile.</b><br>"
                    "Never impersonate an RTU on a live network."
                )
            )
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(self.reject)
            outer.addWidget(bb)
            return

        # ---- Network ----
        net_box = QGroupBox("Network")
        net_form = QFormLayout(net_box)

        self._bind_edit = QLineEdit("0.0.0.0")
        self._bind_edit.setAccessibleName("Bind address (IPv4/IPv6 or 0.0.0.0 for all interfaces)")
        net_form.addRow("Bind address:", self._bind_edit)

        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(2404)
        net_form.addRow("Port:", self._port_spin)

        self._ca_spin = QSpinBox()
        self._ca_spin.setRange(1, 65534)
        self._ca_spin.setValue(1)
        self._ca_spin.setAccessibleName("Common Address of ASDU (1-65534)")
        net_form.addRow("Common Address (CA):", self._ca_spin)

        self._max_clients_spin = QSpinBox()
        self._max_clients_spin.setRange(1, 64)
        self._max_clients_spin.setValue(4)
        net_form.addRow("Max concurrent masters:", self._max_clients_spin)
        outer.addWidget(net_box)

        # ---- TLS ----
        tls_box = QGroupBox("TLS (IEC 62351-3)")
        tls_form = QFormLayout(tls_box)

        self._tls_check = QCheckBox("Enable TLS")
        tls_form.addRow("", self._tls_check)

        self._tls_cert_edit = QLineEdit()
        self._tls_cert_edit.setPlaceholderText("server_cert_and_key.pem")
        self._tls_cert_btn = QPushButton("Browse…")
        self._tls_cert_btn.clicked.connect(self._browse_server_cert)
        cert_row = QHBoxLayout()
        cert_row.addWidget(self._tls_cert_edit, 1)
        cert_row.addWidget(self._tls_cert_btn)
        tls_form.addRow("Server cert+key (PEM):", cert_row)

        self._require_client_cert = QCheckBox("Require client certificate (mutual TLS)")
        tls_form.addRow("", self._require_client_cert)

        self._tls_ca_edit = QLineEdit()
        self._tls_ca_edit.setPlaceholderText("ca_bundle.pem (for client cert verification)")
        self._tls_ca_btn = QPushButton("Browse…")
        self._tls_ca_btn.clicked.connect(self._browse_tls_ca)
        ca_row = QHBoxLayout()
        ca_row.addWidget(self._tls_ca_edit, 1)
        ca_row.addWidget(self._tls_ca_btn)
        tls_form.addRow("Trust roots (PEM):", ca_row)

        self._tls_check.toggled.connect(self._update_tls_visibility)
        self._update_tls_visibility(False)
        outer.addWidget(tls_box)

        # ---- Behaviour ----
        beh_box = QGroupBox("Behaviour")
        beh_form = QFormLayout(beh_box)

        self._gi_check = QCheckBox("Respond to General Interrogation")
        self._gi_check.setChecked(True)
        self._gi_check.setToolTip("Uncheck to test how the master handles GI timeout")
        beh_form.addRow("", self._gi_check)

        self._clock_sync_check = QCheckBox("Auto-respond to Clock Synchronisation")
        self._clock_sync_check.setChecked(True)
        beh_form.addRow("", self._clock_sync_check)

        self._spont_radios: dict[str, QRadioButton] = {}
        for label, key in _SPONTANEOUS_OPTIONS:
            rb = QRadioButton(label)
            self._spont_radios[key] = rb
            beh_form.addRow("", rb)
        self._spont_radios["off"].setChecked(True)

        self._spont_interval_spin = QSpinBox()
        self._spont_interval_spin.setRange(100, 600_000)
        self._spont_interval_spin.setValue(5000)
        self._spont_interval_spin.setSuffix(" ms")
        beh_form.addRow("Spontaneous interval:", self._spont_interval_spin)
        outer.addWidget(beh_box)

        # ---- Point list ----
        pt_box = QGroupBox("Point list (CSV / XLSX)")
        pt_form = QFormLayout(pt_box)

        self._point_list_edit = QLineEdit()
        self._point_list_edit.setPlaceholderText("Optional — leave blank for empty data model")
        self._point_list_btn = QPushButton("Browse…")
        self._point_list_btn.clicked.connect(self._browse_point_list)
        pt_row = QHBoxLayout()
        pt_row.addWidget(self._point_list_edit, 1)
        pt_row.addWidget(self._point_list_btn)
        pt_form.addRow("File:", pt_row)
        outer.addWidget(pt_box)

        # ---- Buttons ----
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Start Slave")
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)

    # ------------------------------------------------------------------
    def _update_tls_visibility(self, enabled: bool) -> None:
        for w in (
            self._tls_cert_edit,
            self._tls_cert_btn,
            self._require_client_cert,
            self._tls_ca_edit,
            self._tls_ca_btn,
        ):
            w.setEnabled(enabled)

    def _browse_server_cert(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Server Certificate + Key (PEM)",
            str(Path.home()),
            "PEM files (*.pem *.crt *.cer);;All files (*)",
        )
        if path:
            self._tls_cert_edit.setText(path)

    def _browse_tls_ca(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "CA Bundle (PEM)",
            str(Path.home()),
            "PEM files (*.pem *.crt *.cer);;All files (*)",
        )
        if path:
            self._tls_ca_edit.setText(path)

    def _browse_point_list(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Point List", str(Path.home()), "CSV / Excel (*.csv *.xlsx *.xls);;All files (*)"
        )
        if path:
            self._point_list_edit.setText(path)

    def _on_accept(self) -> None:
        spont_mode = "off"
        for key, rb in self._spont_radios.items():
            if rb.isChecked():
                spont_mode = key
                break

        self._req = SlaveRequest(
            bind_address=self._bind_edit.text().strip() or "0.0.0.0",
            port=self._port_spin.value(),
            ca=self._ca_spin.value(),
            max_clients=self._max_clients_spin.value(),
            tls=self._tls_check.isChecked(),
            tls_cert_path=self._tls_cert_edit.text().strip(),
            tls_ca_path=self._tls_ca_edit.text().strip(),
            require_client_cert=self._require_client_cert.isChecked(),
            gi_on_receive=self._gi_check.isChecked(),
            auto_clock_sync=self._clock_sync_check.isChecked(),
            spontaneous_mode=spont_mode,
            spontaneous_interval_ms=self._spont_interval_spin.value(),
            point_list_path=self._point_list_edit.text().strip(),
        )
        self.accept()

    def request(self) -> SlaveRequest | None:
        return self._req
