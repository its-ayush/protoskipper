# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""NewConnectionDialog - one-shot dialog for opening a new device session.

Single-page layout (no wizard) so all required information is visible
at once. Field engineers should not need to step through pages to know
what they're committing to. The dialog is *display* only - it returns
the operator's choices, and :class:`SessionManager` is responsible for
actually opening the connection.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
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
        self.setWindowTitle("New Connection")
        self.setMinimumWidth(520)

        self._protocol_combo = QComboBox(self)
        self._populate_protocols()

        self._address_edit = QLineEdit(self)
        self._address_edit.setPlaceholderText("e.g. 10.0.0.5:502/unit=1")
        self._label_edit = QLineEdit(self)
        self._label_edit.setPlaceholderText("Optional friendly name (e.g. 'Feeder-1 RTU')")

        self._profile_group = QButtonGroup(self)
        self._profile_radios = {
            SessionProfile.LAB: QRadioButton(
                "LAB - single-click confirm. For benchtop simulators."
            ),
            SessionProfile.COMMISSIONING: QRadioButton(
                "COMMISSIONING - confirm dialog before each write."
            ),
            SessionProfile.PRODUCTION: QRadioButton(
                "PRODUCTION - type the tag name to confirm each write."
            ),
        }
        for profile, radio in self._profile_radios.items():
            self._profile_group.addButton(radio)
            radio.setProperty("profile", profile.value)
        self._profile_radios[SessionProfile.LAB].setChecked(True)

        self._operator_edit = QLineEdit(self)
        self._operator_edit.setText(default_operator)
        self._operator_edit.setPlaceholderText("Your name or email - recorded in the audit log")

        self._build_layout()

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        self.layout().addWidget(self._buttons)

        # Track validity so the Connect button enables only when all
        # required fields are filled in.
        self._address_edit.textChanged.connect(self._update_buttons)
        self._operator_edit.textChanged.connect(self._update_buttons)
        self._update_buttons()

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

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        protocol_form = QFormLayout()
        protocol_form.addRow("Protocol:", self._protocol_combo)
        protocol_form.addRow("Address:", self._address_edit)
        protocol_form.addRow("Label:", self._label_edit)
        outer.addLayout(protocol_form)

        profile_box = QGroupBox("Session profile")
        profile_layout = QVBoxLayout(profile_box)
        for profile in (
            SessionProfile.LAB,
            SessionProfile.COMMISSIONING,
            SessionProfile.PRODUCTION,
        ):
            profile_layout.addWidget(self._profile_radios[profile])
        profile_layout.addWidget(
            QLabel("<i>Profile cannot be relaxed for the life of the session.</i>")
        )
        outer.addWidget(profile_box)

        ops_form = QFormLayout()
        ops_form.addRow("Operator:", self._operator_edit)
        outer.addLayout(ops_form)

    # ---- validation ------------------------------------------------------

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
        self.accept()

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
