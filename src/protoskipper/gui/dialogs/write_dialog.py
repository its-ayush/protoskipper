# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""WriteDialog - the operator-facing entry point for writing to a device.

Two phases:

* **Phase 1 (Prepare)**: operator enters a value. The dialog asks
  :class:`SessionManager` to call ``prepare_write()`` and waits for the
  resulting :class:`WriteIntent`.
* **Phase 2 (Confirm)**: the dialog displays the encoded bytes alongside
  the human-readable description and asks for explicit authorisation.
  This phase is functionally the same as
  :class:`SafetyConfirmDialog` and behaves identically per profile.

Splitting the two phases is what lets the operator see real bytes
(encoded by the driver, not approximated by the GUI) before authorising
transmission. It is the architectural feature the audit story is built
on.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import (
    Access,
    ObjectRef,
    ReadResult,
    SessionProfile,
    WriteIntent,
)


class WriteDialog(QDialog):
    """Two-phase write dialog (prepare → confirm)."""

    def __init__(
        self,
        ref: ObjectRef,
        profile: SessionProfile,
        last_known: ReadResult | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Write to {ref.object_id}")
        self.setMinimumWidth(560)

        self._ref = ref
        self._profile = profile
        self._intent: WriteIntent | None = None
        self._typed_value: Any = None

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._build_prepare_page(ref, last_known))
        self._stack.addWidget(self._build_confirm_page())

        layout = QVBoxLayout(self)
        layout.addWidget(self._stack)

        self._buttons = QDialogButtonBox(self)
        self._cancel = self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._next = self._buttons.addButton(
            "Prepare write",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self._confirm = self._buttons.addButton(
            "Confirm write",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self._confirm.setVisible(False)
        layout.addWidget(self._buttons)

        self._cancel.clicked.connect(self.reject)
        self._next.clicked.connect(self._on_prepare_clicked)
        self._confirm.clicked.connect(self.accept)

        if ref.access == Access.READ_ONLY:
            self._next.setEnabled(False)
            self._next.setToolTip("This object is read-only.")

    # ---- prepare page ----------------------------------------------------

    def _build_prepare_page(
        self,
        ref: ObjectRef,
        last_known: ReadResult | None,
    ) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        info = QGroupBox("Target")
        form = QFormLayout(info)
        form.addRow("Device:", QLabel(str(ref.device)))
        form.addRow("Object:", QLabel(ref.object_id))
        form.addRow("Type:", QLabel(ref.data_type))
        form.addRow("Unit:", QLabel(ref.unit or "(none)"))
        form.addRow("Access:", QLabel(ref.access.value))
        if last_known is not None:
            form.addRow(
                "Current value:", QLabel(f"{last_known.value}  ({last_known.quality.value})")
            )
        layout.addWidget(info)

        value_box = QGroupBox("New value")
        v = QFormLayout(value_box)
        self._value_edit = QLineEdit(page)
        self._value_edit.setPlaceholderText(_placeholder_for(ref.data_type))
        v.addRow("Value:", self._value_edit)
        layout.addWidget(value_box)

        self._error_label = QLabel("", page)
        self._error_label.setStyleSheet("color: #b91c1c;")
        layout.addWidget(self._error_label)

        return page

    def _on_prepare_clicked(self) -> None:
        text = self._value_edit.text().strip()
        if not text:
            self._error_label.setText("Enter a value.")
            return
        try:
            self._typed_value = _coerce_value(text, self._ref.data_type)
        except Exception as exc:
            self._error_label.setText(f"Cannot interpret value: {exc}")
            return
        self._error_label.setText("")
        # Caller is expected to call set_intent() once the worker has
        # produced the WriteIntent. Until then, switch to a "preparing..."
        # state.
        self._next.setEnabled(False)
        self._next.setText("Preparing...")

    def typed_value(self) -> Any:
        """Return the value the operator typed - used by the controller
        after :meth:`_on_prepare_clicked` to dispatch ``prepare_write``."""
        return self._typed_value

    # ---- confirm page ----------------------------------------------------

    def _build_confirm_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        warning = QLabel(
            "<b>This write will be transmitted immediately on confirm.</b><br>"
            "It cannot be undone from ProtoSkipper."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #b45309;")
        layout.addWidget(warning)

        self._intent_box = QGroupBox("Write intent")
        v = QFormLayout(self._intent_box)
        self._intent_target_label = QLabel("")
        self._intent_value_label = QLabel("")
        self._intent_bytes_label = QLabel("")
        bytes_font = QFont("monospace")
        self._intent_bytes_label.setFont(bytes_font)
        self._intent_bytes_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        v.addRow("Target:", self._intent_target_label)
        v.addRow("Value:", self._intent_value_label)
        v.addRow("Wire bytes:", self._intent_bytes_label)
        layout.addWidget(self._intent_box)

        # Profile-specific extra confirmation widget.
        self._typed_tag_edit: QLineEdit | None = None
        if self._profile == SessionProfile.PRODUCTION:
            tag_box = QGroupBox("Production confirmation")
            tag_layout = QFormLayout(tag_box)
            tag_layout.addRow(QLabel("Type the tag name shown above to authorise this write."))
            self._typed_tag_edit = QLineEdit(page)
            self._typed_tag_edit.textChanged.connect(self._update_confirm_enabled)
            tag_layout.addRow("Tag:", self._typed_tag_edit)
            layout.addWidget(tag_box)

        layout.addStretch()
        return page

    def set_intent(self, intent: WriteIntent) -> None:
        """Called by the controller once the WriteIntent has come back from
        the worker. Switches to the confirm page."""
        self._intent = intent
        self._intent_target_label.setText(
            f"{intent.object_ref.device}  |  {intent.object_ref.object_id}"
        )
        self._intent_value_label.setText(intent.description)
        self._intent_bytes_label.setText(_format_bytes(intent.encoded_bytes))

        self._stack.setCurrentIndex(1)
        self._next.setVisible(False)
        self._confirm.setVisible(True)
        self._update_confirm_enabled()

    def report_prepare_failed(self, message: str) -> None:
        """Called if the worker raised on prepare_write."""
        self._error_label.setText(message)
        self._next.setEnabled(True)
        self._next.setText("Prepare write")

    def intent(self) -> WriteIntent | None:
        return self._intent

    def _update_confirm_enabled(self) -> None:
        if self._profile != SessionProfile.PRODUCTION or self._typed_tag_edit is None:
            self._confirm.setEnabled(self._intent is not None)
            return
        expected = self._ref.label or self._ref.object_id
        match = self._typed_tag_edit.text().strip() == expected
        self._confirm.setEnabled(self._intent is not None and match)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _placeholder_for(data_type: str) -> str:
    if data_type.startswith(("uint", "int")):
        return "integer (decimal or 0x-hex)"
    if data_type.startswith("float"):
        return "floating-point"
    if data_type == "boolean":
        return "true / false / 1 / 0"
    if data_type == "string":
        return "text"
    return ""


def _coerce_value(text: str, data_type: str) -> Any:
    if data_type.startswith(("uint", "int")):
        return int(text, 0)
    if data_type.startswith("float"):
        return float(text)
    if data_type == "boolean":
        lowered = text.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"not a boolean: {text!r}")
    return text


def _format_bytes(payload: bytes) -> str:
    """Hex-format the wire bytes in chunks of 8 for readability."""
    parts = [payload[i : i + 8].hex(" ") for i in range(0, len(payload), 8)]
    return "\n".join(parts) if parts else "(empty)"
