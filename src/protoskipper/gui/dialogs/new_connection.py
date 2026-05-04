# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""NewConnectionDialog - one-shot dialog for opening a new device session.

Single-page layout (no wizard) so all required information is visible
at once. Field engineers should not need to step through pages to know
what they're committing to. The dialog is *display* only - it returns
the operator's choices, and :class:`SessionManager` is responsible for
actually opening the connection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import SessionProfile
from protoskipper.core.plugin_loader import load_protocol_drivers
from protoskipper.gui.dialogs._serial_ports import list_serial_ports

_RECENT_KEY = "recent_connections"
_MAX_RECENT = 10
_ORG = "DataSailors"
_APP = "ProtoSkipper"


@dataclass(frozen=True)
class ConnectionRequest:
    """What the user chose. SessionManager turns this into an open call."""

    protocol_id: str
    address: str
    label: str
    profile: SessionProfile
    operator: str


class NewConnectionDialog(QDialog):
    """Collect the parameters for a new session.

    The dialog never opens a session itself; it is purely a form. On
    Accepted, callers retrieve the user's choice via :meth:`request`.
    """

    def __init__(self, default_operator: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("New Connection"))
        self.setMinimumWidth(520)

        self._protocol_combo = QComboBox(self)
        self._protocol_combo.setAccessibleName(self.tr("Protocol"))
        self._populate_protocols()

        # ---- TCP transport fields ----------------------------------------
        self._host_edit = QLineEdit(self)
        self._host_edit.setPlaceholderText(self.tr("IP address or hostname"))
        self._host_edit.setAccessibleName(self.tr("Gateway host or IP address"))

        self._tcp_port_spin = QSpinBox(self)
        self._tcp_port_spin.setRange(1, 65535)
        self._tcp_port_spin.setValue(502)
        self._tcp_port_spin.setAccessibleName(self.tr("TCP port number"))

        # ---- RTU transport fields ----------------------------------------
        self._serial_port_combo = QComboBox(self)
        self._serial_port_combo.setEditable(True)
        self._serial_port_combo.lineEdit().setPlaceholderText(  # type: ignore[union-attr]
            self.tr("Select or type port path")
        )
        self._serial_port_combo.setAccessibleName(self.tr("Serial port device"))
        self._populate_serial_ports()
        self._serial_port_combo.currentTextChanged.connect(self._update_buttons)

        self._baud_combo = QComboBox(self)
        for _baud in ("1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"):
            self._baud_combo.addItem(_baud)
        self._baud_combo.setCurrentText("9600")
        self._baud_combo.setAccessibleName(self.tr("Baud rate"))

        # ---- Common transport field: Modbus unit / IEC 104 common address
        # The same spinbox is reused with its label/range/text reskinned
        # depending on the selected protocol.  See :meth:`_on_protocol_changed`.
        self._unit_id_spin = QSpinBox(self)
        self._unit_id_spin.setRange(1, 247)
        self._unit_id_spin.setValue(1)
        self._unit_id_spin.setAccessibleName(self.tr("Modbus unit ID (slave address 1\u2013247)"))
        # Device ID spinbox reused for BACnet (0 = any device on that IP).
        self._device_id_spin = QSpinBox(self)
        self._device_id_spin.setRange(0, 4194302)
        self._device_id_spin.setValue(0)
        self._device_id_spin.setSpecialValueText(self.tr("0 (auto / broadcast)"))
        self._device_id_spin.setAccessibleName(self.tr("BACnet Device Instance (0 = any)"))
        self._label_edit = QLineEdit(self)
        self._label_edit.setPlaceholderText(self.tr("Optional friendly name (e.g. 'Feeder-1 RTU')"))
        self._label_edit.setAccessibleName(self.tr("Session label (optional friendly name)"))

        self._profile_group = QButtonGroup(self)
        self._profile_radios = {
            SessionProfile.LAB: QRadioButton(
                self.tr("LAB - single-click confirm. For benchtop simulators.")
            ),
            SessionProfile.COMMISSIONING: QRadioButton(
                self.tr("COMMISSIONING - confirm dialog before each write.")
            ),
            SessionProfile.PRODUCTION: QRadioButton(
                self.tr("PRODUCTION - type the tag name to confirm each write.")
            ),
        }
        for profile, radio in self._profile_radios.items():
            self._profile_group.addButton(radio)
            radio.setProperty("profile", profile.value)
        self._profile_radios[SessionProfile.LAB].setChecked(True)

        self._operator_edit = QLineEdit(self)
        self._operator_edit.setText(default_operator)
        self._operator_edit.setPlaceholderText(
            self.tr("Your name or email - recorded in the audit log")
        )
        self._operator_edit.setAccessibleName(self.tr("Operator name or email"))

        self._build_layout()

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Connect"))
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        self.layout().addWidget(self._buttons)

        # Track validity so the Connect button enables only when all
        # required fields are filled in.
        self._host_edit.textChanged.connect(self._update_buttons)
        self._operator_edit.textChanged.connect(self._update_buttons)
        self._protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        self._update_buttons()
        self._on_protocol_changed(self._protocol_combo.currentIndex())
        # Populate host-field completions from QSettings recent list.
        self._load_recent_completions()

    # ---- layout ----------------------------------------------------------

    def _populate_protocols(self) -> None:
        drivers = load_protocol_drivers()
        if not drivers:
            self._protocol_combo.addItem("(no protocol drivers installed)", None)
            self._protocol_combo.setEnabled(False)
            return
        for proto_id, cls in sorted(drivers.items()):
            label = f"{getattr(cls, 'DISPLAY_NAME', proto_id)}  -  {proto_id}"
            self._protocol_combo.addItem(label, proto_id)

    def _populate_serial_ports(self) -> None:
        """Fill the serial-port combo from pyserial (best-effort)."""
        self._serial_port_combo.clear()
        self._serial_port_combo.addItem("", "")  # blank top entry
        for device, desc in list_serial_ports():
            self._serial_port_combo.addItem(f"{device}  \u2014  {desc}", device)

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        form = QFormLayout()
        form.addRow(self.tr("Protocol:"), self._protocol_combo)

        # TCP-specific rows — label widgets are stored so we can show/hide the pair.
        self._host_row_label = QLabel(self.tr("Host:"), self)
        form.addRow(self._host_row_label, self._host_edit)
        self._tcp_port_row_label = QLabel(self.tr("Port:"), self)
        form.addRow(self._tcp_port_row_label, self._tcp_port_spin)

        # RTU-specific rows
        self._serial_port_row_label = QLabel(self.tr("Serial port:"), self)
        form.addRow(self._serial_port_row_label, self._serial_port_combo)
        self._baud_row_label = QLabel(self.tr("Baud rate:"), self)
        form.addRow(self._baud_row_label, self._baud_combo)

        # BACnet-specific Device ID row
        self._device_id_row_label = QLabel(self.tr("Device ID:"), self)
        form.addRow(self._device_id_row_label, self._device_id_spin)

        # Common rows
        self._unit_id_row_label = QLabel(self.tr("Unit ID:"), self)
        form.addRow(self._unit_id_row_label, self._unit_id_spin)
        form.addRow(self.tr("Label:"), self._label_edit)
        outer.addLayout(form)

        profile_box = QGroupBox(self.tr("Session profile"))
        profile_layout = QVBoxLayout(profile_box)
        for profile in (
            SessionProfile.LAB,
            SessionProfile.COMMISSIONING,
            SessionProfile.PRODUCTION,
        ):
            profile_layout.addWidget(self._profile_radios[profile])
        profile_layout.addWidget(
            QLabel(self.tr("<i>Profile cannot be relaxed for the life of the session.</i>"))
        )
        outer.addWidget(profile_box)

        ops_form = QFormLayout()
        ops_form.addRow(self.tr("Operator:"), self._operator_edit)
        outer.addLayout(ops_form)

    # ---- validation ------------------------------------------------------

    def _on_protocol_changed(self, _index: int) -> None:
        """Switch transport-field visibility when the protocol changes.

        TCP and RTU have completely separate widgets, so there is no risk
        of a serial-port path bleeding into the host field or vice versa.
        """
        proto_id: str | None = self._protocol_combo.currentData()
        is_rtu = proto_id is not None and "rtu" in proto_id.lower()
        is_iec104 = proto_id is not None and proto_id.startswith("iec104")
        is_bacnet = proto_id is not None and proto_id.startswith("bacnet")
        is_iec61850 = proto_id is not None and proto_id.startswith("iec61850")

        # TCP-specific widgets
        for w in (
            self._host_row_label,
            self._host_edit,
            self._tcp_port_row_label,
            self._tcp_port_spin,
        ):
            w.setVisible(not is_rtu)

        # RTU-specific widgets
        for w in (
            self._serial_port_row_label,
            self._serial_port_combo,
            self._baud_row_label,
            self._baud_combo,
        ):
            w.setVisible(is_rtu)

        # BACnet Device ID replaces Modbus/IEC104 Unit ID
        self._device_id_row_label.setVisible(is_bacnet)
        self._device_id_spin.setVisible(is_bacnet)
        # IEC 61850 needs no unit/slave ID — MMS connects directly to the IED.
        show_unit_id = not is_bacnet and not is_iec61850
        self._unit_id_row_label.setVisible(show_unit_id)
        self._unit_id_spin.setVisible(show_unit_id)

        # Reskin the unit/CA spinbox per protocol.
        if is_bacnet:
            if self._tcp_port_spin.value() in (502, 2404):
                self._tcp_port_spin.setValue(47808)
        elif is_iec104:
            self._unit_id_row_label.setText(self.tr("Common address:"))
            self._unit_id_spin.setRange(1, 65535)
            self._unit_id_spin.setAccessibleName(
                self.tr("IEC 60870-5-104 common address (1\u201365534)")
            )
            if self._tcp_port_spin.value() in (502, 47808):
                self._tcp_port_spin.setValue(2404)
        elif is_iec61850:
            # MMS standard port (IANA-assigned).
            if self._tcp_port_spin.value() in (502, 2404, 47808):
                self._tcp_port_spin.setValue(102)
        else:
            self._unit_id_row_label.setText(self.tr("Unit ID:"))
            if self._unit_id_spin.value() > 247:
                self._unit_id_spin.setValue(1)
            self._unit_id_spin.setRange(1, 247)
            self._unit_id_spin.setAccessibleName(
                self.tr("Modbus unit ID (slave address 1\u2013247)")
            )
            if not is_rtu and self._tcp_port_spin.value() in (2404, 47808, 102):
                self._tcp_port_spin.setValue(502)

        # Pre-fill the serial port for RTU if none is selected yet.
        if is_rtu and not self._serial_port_combo.currentText().strip():
            first_device = self._serial_port_combo.itemData(1)
            if first_device:
                self._serial_port_combo.setCurrentText(str(first_device))

        self._update_buttons()
        # Refresh host-field completions for the new protocol.
        self._load_recent_completions()

    def _update_buttons(self) -> None:
        proto_id: str | None = self._protocol_combo.currentData()
        is_rtu = proto_id is not None and "rtu" in proto_id.lower()
        if is_rtu:
            transport_ok = bool(self._serial_port_combo.currentText().strip())
        else:
            transport_ok = bool(self._host_edit.text().strip())
        ok = proto_id is not None and transport_ok and bool(self._operator_edit.text().strip())
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def _on_accept(self) -> None:
        proto_id: str | None = self._protocol_combo.currentData()
        if proto_id is None:
            return
        is_rtu = "rtu" in proto_id.lower()
        if is_rtu and not self._serial_port_combo.currentText().strip():
            return
        if not is_rtu and not self._host_edit.text().strip():
            return
        if not self._operator_edit.text().strip():
            return
        self._save_recent_connection()
        self.accept()

    # ---- P3.A.1 recent connections (QSettings-backed QCompleter) ---------

    def _load_recent_completions(self) -> None:
        """Rebuild the QCompleter on the host field from QSettings (TCP only)."""
        proto_id: str | None = self._protocol_combo.currentData()
        is_rtu = proto_id is not None and "rtu" in proto_id.lower()
        if is_rtu:
            self._host_edit.setCompleter(None)
            return
        s = QSettings(_ORG, _APP)
        try:
            recent: list[dict] = json.loads(s.value(_RECENT_KEY, "[]", str))
        except (json.JSONDecodeError, TypeError):
            recent = []
        hosts_raw = [
            entry["host"]
            for entry in recent
            if isinstance(entry, dict)
            and entry.get("protocol") == proto_id
            and isinstance(entry.get("host"), str)
        ]
        seen: set[str] = set()
        hosts: list[str] = []
        for h in hosts_raw:
            if h not in seen:
                seen.add(h)
                hosts.append(h)
        self._host_edit.setCompleter(QCompleter(hosts, self))

    def _save_recent_connection(self) -> None:
        """Prepend the current connection to the QSettings recent list (max 10)."""
        proto_id: str | None = self._protocol_combo.currentData()
        if not proto_id:
            return
        is_rtu = "rtu" in proto_id.lower()
        label = self._label_edit.text().strip()
        if is_rtu:
            serial_port = self._serial_port_combo.currentText().strip()
            if not serial_port:
                return
            entry: dict = {
                "protocol": proto_id,
                "serial_port": serial_port,
                "baud": self._baud_combo.currentText(),
                "unit": self._unit_id_spin.value(),
                "label": label,
            }
        else:
            host = self._host_edit.text().strip()
            if not host:
                return
            entry = {
                "protocol": proto_id,
                "host": host,
                "tcp_port": self._tcp_port_spin.value(),
                "unit": self._unit_id_spin.value(),
                "label": label,
            }
        s = QSettings(_ORG, _APP)
        try:
            recent: list[dict] = json.loads(s.value(_RECENT_KEY, "[]", str))
        except (json.JSONDecodeError, TypeError):
            recent = []
        # Remove any existing identical entry so we don't accumulate duplicates.
        if is_rtu:
            key_field = "serial_port"
            key_val = entry["serial_port"]
        else:
            key_field = "host"
            key_val = entry["host"]
        recent = [
            r for r in recent if not (r.get("protocol") == proto_id and r.get(key_field) == key_val)
        ]
        recent.insert(0, entry)
        s.setValue(_RECENT_KEY, json.dumps(recent[:_MAX_RECENT]))

    # ---- pre-fill API (used by ProbeNetworkDialog handoff) --------------

    def set_protocol(self, protocol_id: str) -> None:
        """Pre-select a protocol by id. No-op if the id is not registered."""
        for i in range(self._protocol_combo.count()):
            if self._protocol_combo.itemData(i) == protocol_id:
                self._protocol_combo.setCurrentIndex(i)
                return

    def set_address(self, address: str) -> None:
        """Populate the transport fields by parsing an address string.

        For TCP: ``host:port/unit=N``, ``host:port/ca=N``, or ``host:port/dev=N``
        → fills Host, Port, and the relevant ID spinbox.
        For RTU: the raw address string is placed in the serial-port field.
        """
        import re

        proto_id: str | None = self._protocol_combo.currentData()
        is_rtu = proto_id is not None and "rtu" in proto_id.lower()
        is_bacnet = proto_id is not None and proto_id.startswith("bacnet")
        address = address.strip()
        if is_rtu:
            self._serial_port_combo.setCurrentText(address)
        else:
            m = re.match(
                r"^(?:bacnet://)?(?P<host>[^\s:/]+)(?::(?P<port>\d+))?"
                r"(?:/dev=(?P<dev>\d+))?(?:/(?:unit|ca)=(?P<unit>\d+))?$",
                address,
            )
            if m:
                self._host_edit.setText(m.group("host"))
                if m.group("port"):
                    self._tcp_port_spin.setValue(int(m.group("port")))
                if is_bacnet and m.group("dev"):
                    self._device_id_spin.setValue(int(m.group("dev")))
                elif not is_bacnet and m.group("unit"):
                    self._unit_id_spin.setValue(int(m.group("unit")))
            else:
                self._host_edit.setText(address)

    def set_label(self, label: str) -> None:
        self._label_edit.setText(label)

    # ---- public API ------------------------------------------------------

    def request(self) -> ConnectionRequest:
        """Read the user's choices. Only call after :meth:`exec` returned Accepted."""
        proto_id: str = self._protocol_combo.currentData()
        is_rtu = "rtu" in proto_id.lower()
        is_iec104 = proto_id.startswith("iec104")
        is_bacnet = proto_id.startswith("bacnet")
        is_iec61850 = proto_id.startswith("iec61850")
        unit = self._unit_id_spin.value()
        if is_rtu:
            port = self._serial_port_combo.currentText().strip()
            baud = self._baud_combo.currentText()
            address = f"{port}@{baud},N,1/unit={unit}"
        elif is_bacnet:
            host = self._host_edit.text().strip()
            tcp_port = self._tcp_port_spin.value()
            dev_id = self._device_id_spin.value()
            address = f"{host}:{tcp_port}"
            if dev_id > 0:
                address += f"/dev={dev_id}"
        elif is_iec61850:
            # MMS: plain host:port — no unit/slave addressing in IEC 61850.
            host = self._host_edit.text().strip()
            tcp_port = self._tcp_port_spin.value()
            address = f"{host}:{tcp_port}"
        else:
            host = self._host_edit.text().strip()
            tcp_port = self._tcp_port_spin.value()
            if is_iec104:
                address = f"{host}:{tcp_port}/ca={unit}"
            else:
                address = f"{host}:{tcp_port}/unit={unit}"
        profile_value = self._profile_group.checkedButton().property("profile")
        profile = SessionProfile(profile_value)
        return ConnectionRequest(
            protocol_id=proto_id,
            address=address,
            label=self._label_edit.text().strip(),
            profile=profile,
            operator=self._operator_edit.text().strip(),
        )
