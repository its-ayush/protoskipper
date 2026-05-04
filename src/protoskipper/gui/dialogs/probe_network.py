# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""ProbeNetworkDialog - find devices on a network or serial bus.

Modal dialog that runs a discovery scan against the chosen protocol and
streams results into a table as they arrive. The operator can cancel
mid-scan, pick a discovered device, and dispatch a connection - the
dialog returns the operator's selection (if any) so the main window can
open the New Connection flow with the device pre-filled.

Architecture
------------

The dialog does not touch drivers directly. It calls
:meth:`SessionManager.start_discovery` and listens to
:attr:`ApplicationState.device_discovered` and
:attr:`ApplicationState.discovery_finished`. Results stream in via Qt
signals, so the UI thread stays responsive while the worker thread
hammers the network.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import DeviceRef
from protoskipper.core.plugin_loader import load_protocol_drivers
from protoskipper.gui.dialogs._serial_ports import list_serial_ports
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId

# Placeholder + example helper text per protocol.
_TARGET_HINTS = {
    "modbus.tcp": (
        "Examples: 10.0.0.5  |  10.0.0.5:502/unit=1  |  "
        "192.168.1.0/24/units=1-10  |  10.0.0.5,10.0.0.7/units=1-247"
    ),
    "modbus.rtu": (
        "Examples: /dev/ttyUSB0  |  /dev/ttyUSB0@9600,N,1/units=1-32  |  COM4@19200,E,1/unit=3"
    ),
    "bacnet.ip": (
        "Examples: broadcast  |  192.168.1.255  |  10.0.0.5  |  "
        "low=1,high=1000  |  broadcast,low=1,high=4194302"
    ),
}


@dataclass(frozen=True)
class ProbeSelection:
    """What the operator picked from the result list (if anything)."""

    device: DeviceRef
    protocol_id: str


class ProbeNetworkDialog(QDialog):
    """Live-streaming network/bus probe dialog."""

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Probe Network")
        self.resize(820, 540)

        self._state = state
        self._sm = session_manager
        self._discovery_id: SessionId | None = None
        self._selection: ProbeSelection | None = None
        self._discovered_devices: list[DeviceRef] = []
        self._scan_active = False

        self._build_ui()
        self._connect_signals()
        self._update_buttons()
        self._update_target_hint()
        self._on_protocol_changed(self._protocol_combo.currentIndex())

    # ---- ui construction -------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        # Top form: protocol + target.
        form_box = QFormLayout()
        self._protocol_combo = QComboBox(self)
        self._populate_protocols()
        self._target_edit = QLineEdit(self)
        self._target_edit.setPlaceholderText("Address / range / bus")
        form_box.addRow("Protocol:", self._protocol_combo)
        form_box.addRow("Target:", self._target_edit)

        # Serial-port dropdown (shown only for RTU-type protocols).
        self._port_combo = QComboBox(self)
        self._port_combo.setEditable(True)
        self._port_combo.lineEdit().setPlaceholderText(  # type: ignore[union-attr]
            "Select or type port path"
        )
        self._port_combo_label = QLabel("Serial port:", self)
        self._populate_serial_ports()
        self._port_combo.currentTextChanged.connect(self._on_port_selected)
        form_box.addRow(self._port_combo_label, self._port_combo)
        outer.addLayout(form_box)

        self._hint_label = QLabel("", self)
        self._hint_label.setWordWrap(True)
        self._hint_label.setStyleSheet("color: #6b7280;")
        outer.addWidget(self._hint_label)

        # Action row: Start / Cancel buttons + status.
        actions = QHBoxLayout()
        self._start_button = QPushButton("Start probe", self)
        self._cancel_button = QPushButton("Cancel scan", self)
        self._cancel_button.setEnabled(False)
        self._clear_button = QPushButton("Clear results", self)
        actions.addWidget(self._start_button)
        actions.addWidget(self._cancel_button)
        actions.addWidget(self._clear_button)
        actions.addStretch()
        self._status_label = QLabel("Ready.", self)
        actions.addWidget(self._status_label)
        outer.addLayout(actions)

        # Results table.
        self._table = QTableWidget(0, 4, self)
        self._table.setHorizontalHeaderLabels(["Address", "Label", "Vendor", "Notes"])
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        outer.addWidget(self._table, stretch=1)

        # Bottom row: Close + "Use selected for connection".
        self._buttons = QDialogButtonBox(self)
        self._connect_button = self._buttons.addButton(
            "Use selected for connection",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self._buttons.addButton(QDialogButtonBox.StandardButton.Close)
        outer.addWidget(self._buttons)

    def _populate_protocols(self) -> None:
        for proto_id, cls in sorted(load_protocol_drivers().items()):
            label = f"{getattr(cls, 'DISPLAY_NAME', proto_id)}  -  {proto_id}"
            self._protocol_combo.addItem(label, proto_id)

    def _populate_serial_ports(self) -> None:
        """Fill the serial-port combo from pyserial (best-effort)."""
        self._port_combo.clear()
        self._port_combo.addItem("", "")
        for device, desc in list_serial_ports():
            self._port_combo.addItem(f"{device}  —  {desc}", device)

    def _connect_signals(self) -> None:
        self._protocol_combo.currentIndexChanged.connect(self._update_target_hint)
        self._protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        self._start_button.clicked.connect(self._on_start)
        self._cancel_button.clicked.connect(self._on_cancel)
        self._clear_button.clicked.connect(self._on_clear)
        self._target_edit.returnPressed.connect(self._on_start)
        self._table.itemSelectionChanged.connect(self._update_buttons)
        self._connect_button.clicked.connect(self._on_use_selected)
        self._buttons.rejected.connect(self.reject)

        self._state.device_discovered.connect(self._on_device_discovered)
        self._state.discovery_finished.connect(self._on_discovery_finished)
        self._state.error_raised.connect(self._on_error)

    # ---- helpers ---------------------------------------------------------

    def _on_protocol_changed(self, _index: int) -> None:
        """Show or hide the serial-port combo based on the selected protocol."""
        proto_id: str | None = self._protocol_combo.currentData()
        is_rtu = proto_id is not None and "rtu" in proto_id.lower()
        self._port_combo.setVisible(is_rtu)
        self._port_combo_label.setVisible(is_rtu)

    def _on_port_selected(self, text: str) -> None:
        """Mirror the selected port device path to the target field."""
        device = self._port_combo.currentData()
        if device:
            self._target_edit.setText(str(device))
        elif text.strip():
            self._target_edit.setText(text.strip())

    # ---- helpers ---------------------------------------------------------

    def _update_target_hint(self) -> None:
        proto = self._protocol_combo.currentData()
        self._hint_label.setText(
            _TARGET_HINTS.get(proto, "Address format depends on the protocol driver.")
        )

    def _update_buttons(self) -> None:
        has_selection = bool(self._table.selectionModel().selectedRows())
        self._connect_button.setEnabled(has_selection and not self._scan_active)
        self._start_button.setEnabled(not self._scan_active)
        self._cancel_button.setEnabled(self._scan_active)

    # ---- handlers --------------------------------------------------------

    def _on_start(self) -> None:
        target = self._target_edit.text().strip()
        if not target:
            self._status_label.setText("Enter a target.")
            return
        protocol = self._protocol_combo.currentData()
        if protocol is None:
            self._status_label.setText("No protocol available.")
            return

        # Reset previous results so the user does not confuse stale rows
        # with the ones from this scan.
        self._on_clear()
        self._scan_active = True
        try:
            self._discovery_id = self._sm.start_discovery(protocol, target)
        except Exception as exc:
            self._scan_active = False
            self._status_label.setText(f"Could not start scan: {exc}")
            self._update_buttons()
            return
        self._status_label.setText(f"Scanning {target}…")
        self._update_buttons()

    def _on_cancel(self) -> None:
        if self._discovery_id is None:
            return
        self._sm.cancel_discovery(self._discovery_id)
        self._status_label.setText("Cancelling…")

    def _on_clear(self) -> None:
        self._table.setRowCount(0)
        self._discovered_devices.clear()
        self._update_buttons()

    def _on_device_discovered(self, device: DeviceRef) -> None:
        # Filter to the protocol we're scanning - the dialog may share the
        # ApplicationState with the main window, which sees devices from
        # other sessions too.
        if not self._scan_active:
            return
        protocol = self._protocol_combo.currentData()
        if device.protocol != protocol:
            return
        self._discovered_devices.append(device)
        row = self._table.rowCount()
        self._table.insertRow(row)
        meta = device.metadata or {}
        notes_parts = []
        # Modbus / generic fields
        if "unit_id" in meta:
            notes_parts.append(f"unit_id={meta['unit_id']}")
        if "product_code" in meta:
            notes_parts.append(f"product={meta['product_code']}")
        if "revision" in meta:
            notes_parts.append(f"rev={meta['revision']}")
        if "baudrate" in meta:
            notes_parts.append(
                f"{meta['baudrate']},{meta.get('parity', 'N')},{meta.get('stopbits', 1)}"
            )
        # BACnet-specific fields
        if "device_id" in meta and meta["device_id"] is not None:
            notes_parts.append(f"dev={meta['device_id']}")
        if "vendor_id" in meta and meta["vendor_id"] is not None:
            notes_parts.append(f"vendor_id={meta['vendor_id']}")
        if "max_apdu" in meta and meta["max_apdu"] is not None:
            notes_parts.append(f"max_apdu={meta['max_apdu']}")
        notes = "  ".join(notes_parts)

        self._table.setItem(row, 0, QTableWidgetItem(device.address))
        self._table.setItem(row, 1, QTableWidgetItem(device.label or ""))
        self._table.setItem(row, 2, QTableWidgetItem(str(meta.get("vendor_name", ""))))
        self._table.setItem(row, 3, QTableWidgetItem(notes))
        self._status_label.setText(f"Scanning… {self._table.rowCount()} device(s) found so far.")

    def _on_discovery_finished(self, protocol_id: str, count: int) -> None:
        if not self._scan_active:
            return
        if protocol_id != self._protocol_combo.currentData():
            return
        self._scan_active = False
        self._discovery_id = None
        n_found = self._table.rowCount()
        self._status_label.setText(f"Done. {n_found} device(s) found ({count} probe response(s)).")
        self._update_buttons()

    def _on_error(self, operation: str, message: str) -> None:
        if self._scan_active and operation == "discover":
            self._status_label.setText(f"Scan error: {message}")

    def _on_use_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        if idx < 0 or idx >= len(self._discovered_devices):
            return
        device = self._discovered_devices[idx]
        self._selection = ProbeSelection(
            device=device,
            protocol_id=device.protocol,
        )
        self.accept()

    # ---- public API ------------------------------------------------------

    def selection(self) -> ProbeSelection | None:
        """The device the operator chose to use, or None if Close was clicked."""
        return self._selection

    def closeEvent(self, event) -> None:
        # Cancel any in-flight scan so we don't leave a worker thread running
        # when the dialog disappears.
        if self._discovery_id is not None and self._scan_active:
            self._sm.cancel_discovery(self._discovery_id)
        super().closeEvent(event)
