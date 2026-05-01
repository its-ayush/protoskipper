# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""SafetyConfirmDialog - shown by GuiConfirmHandler from inside the SafetyContext.

This is the dialog that runs when a driver calls
``safety.require_write_authorization(intent)``. It is functionally
equivalent to the confirm page of :class:`WriteDialog` - same bytes,
same profile-aware confirmation - and exists as a separate dialog
because the call stack is different (driver -> safety -> dialog rather
than user -> dialog -> driver).

The two dialogs share a layout so operators recognise the moment of
authorisation regardless of which path got them here.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import SessionProfile, WriteIntent
from protoskipper.gui.theme import active_theme


class SafetyConfirmDialog(QDialog):
    """Final yes/no for a write that has already been prepared."""

    def __init__(
        self,
        intent: WriteIntent,
        profile: SessionProfile,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Authorise write")
        self.setMinimumWidth(560)

        self._intent = intent
        self._profile = profile

        layout = QVBoxLayout(self)

        chip = QLabel(f"<b>Profile:</b> {profile.value.upper()}")
        chip.setStyleSheet(f"color: {active_theme().profile_color(profile).name()};")
        layout.addWidget(chip)

        warning = QLabel(
            "<b>This write will be transmitted immediately on confirm.</b><br>"
            "It cannot be undone from ProtoSkipper."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #b45309;")
        layout.addWidget(warning)

        intent_box = QGroupBox("Write intent")
        form = QFormLayout(intent_box)
        form.addRow("Target:", QLabel(
            f"{intent.object_ref.device}  |  {intent.object_ref.object_id}"
        ))
        form.addRow("Description:", QLabel(intent.description))
        bytes_label = QLabel(_format_bytes(intent.encoded_bytes))
        bytes_label.setFont(QFont("monospace"))
        bytes_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Wire bytes:", bytes_label)
        layout.addWidget(intent_box)

        self._typed_tag_edit: QLineEdit | None = None
        if profile == SessionProfile.PRODUCTION:
            tag_box = QGroupBox("Production confirmation")
            tag_layout = QFormLayout(tag_box)
            tag_layout.addRow(QLabel(
                "Type the tag name to authorise this write."
            ))
            self._typed_tag_edit = QLineEdit(self)
            self._typed_tag_edit.textChanged.connect(self._update_buttons)
            tag_layout.addRow("Tag:", self._typed_tag_edit)
            layout.addWidget(tag_box)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Yes,
            parent=self,
        )
        yes_btn = self._buttons.button(QDialogButtonBox.StandardButton.Yes)
        yes_btn.setText("Authorise transmission")
        cancel_btn = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setText("Deny")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._update_buttons()

    def _update_buttons(self) -> None:
        yes_btn = self._buttons.button(QDialogButtonBox.StandardButton.Yes)
        if self._profile != SessionProfile.PRODUCTION:
            yes_btn.setEnabled(True)
            return
        expected = self._intent.object_ref.label or self._intent.object_ref.object_id
        if self._typed_tag_edit is None:
            yes_btn.setEnabled(False)
            return
        yes_btn.setEnabled(self._typed_tag_edit.text().strip() == expected)


def _format_bytes(payload: bytes) -> str:
    parts = [payload[i : i + 8].hex(" ") for i in range(0, len(payload), 8)]
    return "\n".join(parts) if parts else "(empty)"
