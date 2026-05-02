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

        self._address_edit = QLineEdit(self)
        self._address_edit.setPlaceholderText(self.tr("e.g. 10.0.0.5:502/unit=1"))
        self._address_edit.setAccessibleName(self.tr("Device address"))

        # Serial-port dropdown (shown only for RTU-type protocols).
        self._port_combo = QComboBox(self)
        self._port_combo.setEditable(True)
        self._port_combo.lineEdit().setPlaceholderText(  # type: ignore[union-attr]
            "Select or type port path"
        )
        self._port_combo.setVisible(False)
        self._populate_serial_ports()
        self._port_combo.currentTextChanged.connect(self._on_port_selected)
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
        self._address_edit.textChanged.connect(self._update_buttons)
        self._operator_edit.textChanged.connect(self._update_buttons)
        self._protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        self._update_buttons()
        self._on_protocol_changed(self._protocol_combo.currentIndex())
        # P3.A.1: Populate address-field completions from QSettings recent list.
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
        self._port_combo.clear()
        self._port_combo.addItem("", "")  # blank top entry
        for device, desc in list_serial_ports():
            self._port_combo.addItem(f"{device}  —  {desc}", device)

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        protocol_form = QFormLayout()
        protocol_form.addRow(self.tr("Protocol:"), self._protocol_combo)
        protocol_form.addRow(self.tr("Address:"), self._address_edit)
        self._port_combo_row_label = QLabel(self.tr("Serial port:"), self)
        protocol_form.addRow(self._port_combo_row_label, self._port_combo)
        protocol_form.addRow(self.tr("Label:"), self._label_edit)
        outer.addLayout(protocol_form)

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
        """Show or hide the serial-port combo based on the selected protocol."""
        proto_id: str | None = self._protocol_combo.currentData()
        is_rtu = proto_id is not None and "rtu" in proto_id.lower()
        self._port_combo.setVisible(is_rtu)
        self._port_combo_row_label.setVisible(is_rtu)
        if is_rtu and not self._address_edit.text().strip():
            # Pre-fill the address with the first discovered port (if any).
            first_device = self._port_combo.itemData(1)
            if first_device:
                self._address_edit.setText(str(first_device))
        self._update_buttons()
        # Refresh address completions for the new protocol.
        self._load_recent_completions()

    def _on_port_selected(self, text: str) -> None:
        """When a port is chosen from the dropdown, mirror it to the address field."""
        device = self._port_combo.currentData()
        if device:
            self._address_edit.setText(str(device))
        elif text.strip():
            # Editable combo — user typed a custom path.
            self._address_edit.setText(text.strip())

    def _update_buttons(self) -> None:
        ok = (
            self._protocol_combo.currentData() is not None
            and bool(self._address_edit.text().strip())
            and bool(self._operator_edit.text().strip())
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def _on_accept(self) -> None:
        if self._protocol_combo.currentData() is None:
            return
        if not self._address_edit.text().strip():
            return
        if not self._operator_edit.text().strip():
            return
        self._save_recent_connection()
        self.accept()

    # ---- P3.A.1 recent connections (QSettings-backed QCompleter) ---------

    def _load_recent_completions(self) -> None:
        """Rebuild the QCompleter on the address field from QSettings."""
        proto_id: str | None = self._protocol_combo.currentData()
        s = QSettings(_ORG, _APP)
        try:
            recent: list[dict] = json.loads(s.value(_RECENT_KEY, "[]", str))
        except (json.JSONDecodeError, TypeError):
            recent = []
        addresses = [
            entry["address"]
            for entry in recent
            if isinstance(entry, dict)
            and entry.get("protocol") == proto_id
            and isinstance(entry.get("address"), str)
        ]
        completer = QCompleter(addresses, self)
        self._address_edit.setCompleter(completer)

    def _save_recent_connection(self) -> None:
        """Prepend the current connection to the QSettings recent list (max 10)."""
        proto_id: str | None = self._protocol_combo.currentData()
        address = self._address_edit.text().strip()
        label = self._label_edit.text().strip()
        if not proto_id or not address:
            return
        s = QSettings(_ORG, _APP)
        try:
            recent: list[dict] = json.loads(s.value(_RECENT_KEY, "[]", str))
        except (json.JSONDecodeError, TypeError):
            recent = []
        entry = {"protocol": proto_id, "address": address, "label": label}
        # Remove any existing identical entry so we don't accumulate duplicates.
        recent = [r for r in recent if r.get("address") != address or r.get("protocol") != proto_id]
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
        self._address_edit.setText(address)

    def set_label(self, label: str) -> None:
        self._label_edit.setText(label)

    # ---- public API ------------------------------------------------------

    def request(self) -> ConnectionRequest:
        """Read the user's choices. Only call after :meth:`exec` returned Accepted."""
        profile_value = self._profile_group.checkedButton().property("profile")
        profile = SessionProfile(profile_value)
        return ConnectionRequest(
            protocol_id=self._protocol_combo.currentData(),
            address=self._address_edit.text().strip(),
            label=self._label_edit.text().strip(),
            profile=profile,
            operator=self._operator_edit.text().strip(),
        )
