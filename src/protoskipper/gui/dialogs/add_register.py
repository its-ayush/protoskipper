# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""AddRegisterDialog — manually add a Modbus register to an open session.

Modbus has no self-description protocol, so the object browser starts empty
after a connection is established.  This dialog lets the operator define a
register by specifying the table, wire address, data type, encoding options,
optional scale/offset, and a friendly label.

Design notes
------------
* The dialog is purely a form.  It builds and returns an :class:`ObjectRef`;
  the caller (ObjectBrowserPanel → SessionManager) is responsible for adding
  it to the session's object list.
* All field interactions are local (no network I/O).
* Scale/offset are stored in ``ObjectRef.metadata`` and used by the Modbus
  driver's ``_apply_read_scale`` / ``_apply_write_scale`` helpers.
* The "Modbus notation" hint (40101-style) is purely informational.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import Access, DeviceRef, ObjectRef

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TABLES = [
    ("Holding registers  (FC03 read / FC16 write)", "holding"),
    ("Input registers  (FC04 read, read-only)", "input"),
    ("Coils  (FC01 read / FC05/FC15 write)", "coils"),
    ("Discrete inputs  (FC02 read, read-only)", "discrete"),
]

_DATA_TYPES_REGISTER = [
    ("boolean — 1 coil / discrete bit", "boolean"),
    ("uint16  — 1 register", "uint16"),
    ("int16   — 1 register", "int16"),
    ("uint32  — 2 registers", "uint32"),
    ("int32   — 2 registers", "int32"),
    ("float32 — 2 registers (IEEE-754)", "float32"),
    ("uint64  — 4 registers", "uint64"),
    ("int64   — 4 registers", "int64"),
    ("float64 — 4 registers (IEEE-754)", "float64"),
    ("ascii   — N registers (2 chars each)", "ascii"),
    ("utf16   — N registers (1 char each)", "utf16"),
]

# How many 16-bit registers each fixed-width type uses.
_REGISTER_COUNTS: dict[str, int] = {
    "uint16": 1,
    "int16": 1,
    "uint32": 2,
    "int32": 2,
    "float32": 2,
    "uint64": 4,
    "int64": 4,
    "float64": 4,
    "ascii": 0,  # variable — user sets count
    "utf16": 0,  # variable — user sets count
    "boolean": 1,
}

# 5-digit Modbus notation offsets.
_TABLE_NOTATION_OFFSET = {
    "coils": 1,
    "discrete": 10001,
    "input": 30001,
    "holding": 40001,
}


class AddRegisterDialog(QDialog):
    """Form for manually defining a Modbus register to add to a session."""

    def __init__(self, device: DeviceRef, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._device = device
        self.setWindowTitle(self.tr("Add Register"))
        self.setMinimumWidth(480)

        self._user_set_count = False  # True once the user manually edits count

        # ---- Table -------------------------------------------------------
        self._table_combo = QComboBox(self)
        self._table_combo.setAccessibleName(self.tr("Modbus table (function code group)"))
        for label, val in _TABLES:
            self._table_combo.addItem(label, val)

        # ---- Address -----------------------------------------------------
        self._address_spin = QSpinBox(self)
        self._address_spin.setRange(0, 65535)
        self._address_spin.setValue(0)
        self._address_spin.setAccessibleName(self.tr("Register address (0-based, wire format)"))

        self._notation_label = QLabel(self)  # live hint e.g. "40001"
        self._notation_label.setStyleSheet("color: grey; font-size: 11px;")

        addr_row = QHBoxLayout()
        addr_row.addWidget(self._address_spin)
        addr_row.addWidget(self._notation_label)
        addr_row.addStretch()
        self._addr_container = QWidget(self)
        self._addr_container.setLayout(addr_row)

        # ---- Data type ---------------------------------------------------
        self._dtype_combo = QComboBox(self)
        self._dtype_combo.setAccessibleName(self.tr("Data type"))
        for label, val in _DATA_TYPES_REGISTER:
            self._dtype_combo.addItem(label, val)

        # ---- Count -------------------------------------------------------
        self._count_spin = QSpinBox(self)
        self._count_spin.setRange(1, 120)
        self._count_spin.setValue(1)
        self._count_spin.setAccessibleName(
            self.tr("Number of 16-bit registers (auto-set from data type)")
        )

        # ---- Byte/Word order ---------------------------------------------
        self._byte_order_combo = QComboBox(self)
        self._byte_order_combo.addItem(
            self.tr("Big-endian  (standard Modbus, high byte first)"), "big"
        )
        self._byte_order_combo.addItem(self.tr("Little-endian  (low byte first)"), "little")
        self._byte_order_combo.setAccessibleName(self.tr("Byte order within each register"))

        self._word_order_combo = QComboBox(self)
        self._word_order_combo.addItem(
            self.tr("Big-endian  (MSW at lower address, ABCD, standard)"), "big"
        )
        self._word_order_combo.addItem(
            self.tr("Little-endian  (LSW at lower address, CDAB)"), "little"
        )
        self._word_order_combo.setAccessibleName(
            self.tr("Word order across registers (multi-register types only)")
        )

        # ---- Label / unit ------------------------------------------------
        self._label_edit = QLineEdit(self)
        self._label_edit.setPlaceholderText(self.tr("e.g. Voltage L1"))
        self._label_edit.setAccessibleName(self.tr("Friendly tag label (optional)"))

        self._unit_edit = QLineEdit(self)
        self._unit_edit.setPlaceholderText(self.tr("e.g. V, A, kWh, degC"))
        self._unit_edit.setAccessibleName(self.tr("Engineering unit (optional)"))

        # ---- Scale / offset ----------------------------------------------
        self._scale_spin = QDoubleSpinBox(self)
        self._scale_spin.setRange(-1e9, 1e9)
        self._scale_spin.setDecimals(6)
        self._scale_spin.setValue(1.0)
        self._scale_spin.setStepType(QDoubleSpinBox.StepType.AdaptiveDecimalStepType)
        self._scale_spin.setAccessibleName(
            self.tr("Scale factor: displayed_value = raw x scale + offset")
        )

        self._offset_spin = QDoubleSpinBox(self)
        self._offset_spin.setRange(-1e9, 1e9)
        self._offset_spin.setDecimals(6)
        self._offset_spin.setValue(0.0)
        self._offset_spin.setStepType(QDoubleSpinBox.StepType.AdaptiveDecimalStepType)
        self._offset_spin.setAccessibleName(self.tr("Offset added after scale"))

        # ---- Access ------------------------------------------------------
        self._access_group = QButtonGroup(self)
        self._access_ro = QRadioButton(self.tr("Read-only"), self)
        self._access_rw = QRadioButton(self.tr("Read-Write"), self)
        self._access_group.addButton(self._access_ro)
        self._access_group.addButton(self._access_rw)
        self._access_rw.setChecked(True)

        access_row = QHBoxLayout()
        access_row.addWidget(self._access_rw)
        access_row.addWidget(self._access_ro)
        access_row.addStretch()
        self._access_container = QWidget(self)
        self._access_container.setLayout(access_row)

        # ---- Preview label -----------------------------------------------
        self._preview_label = QLabel(self)
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self._preview_label.setStyleSheet("font-family: monospace; color: grey;")

        # ---- Buttons -----------------------------------------------------
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Add Register"))
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        # ---- Layout ------------------------------------------------------
        self._build_layout()

        # ---- Wire signals ------------------------------------------------
        self._table_combo.currentIndexChanged.connect(self._on_table_changed)
        self._dtype_combo.currentIndexChanged.connect(self._on_dtype_changed)
        self._address_spin.valueChanged.connect(self._update_notation)
        self._count_spin.valueChanged.connect(self._on_count_edited)
        self._count_spin.editingFinished.connect(lambda: setattr(self, "_user_set_count", True))

        # Initial state
        self._on_table_changed(0)

    # ---- layout ----------------------------------------------------------

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        form.addRow(self.tr("Table:"), self._table_combo)
        form.addRow(self.tr("Address:"), self._addr_container)
        form.addRow(self.tr("Data type:"), self._dtype_combo)

        self._count_row_label = QLabel(self.tr("Count (registers):"), self)
        form.addRow(self._count_row_label, self._count_spin)

        self._byte_order_label = QLabel(self.tr("Byte order:"), self)
        form.addRow(self._byte_order_label, self._byte_order_combo)
        self._word_order_label = QLabel(self.tr("Word order:"), self)
        form.addRow(self._word_order_label, self._word_order_combo)

        outer.addLayout(form)

        meta_box = QGroupBox(self.tr("Tag metadata (optional)"))
        meta_form = QFormLayout(meta_box)
        meta_form.addRow(self.tr("Label:"), self._label_edit)
        meta_form.addRow(self.tr("Engineering unit:"), self._unit_edit)

        self._scale_row_label = QLabel(self.tr("Scale  (displayed = raw x scale + offset):"), self)
        meta_form.addRow(self._scale_row_label, self._scale_spin)
        self._offset_row_label = QLabel(self.tr("Offset:"), self)
        meta_form.addRow(self._offset_row_label, self._offset_spin)

        outer.addWidget(meta_box)

        access_box = QGroupBox(self.tr("Access"))
        QVBoxLayout(access_box).addWidget(self._access_container)
        outer.addWidget(access_box)

        outer.addWidget(self._preview_label)
        outer.addWidget(self._buttons)

    # ---- dynamic behaviour ----------------------------------------------

    def _current_table(self) -> str:
        return self._table_combo.currentData() or "holding"

    def _current_dtype(self) -> str:
        return self._dtype_combo.currentData() or "uint16"

    def _on_table_changed(self, _index: int) -> None:
        table = self._current_table()
        is_bit_table = table in ("coils", "discrete")
        is_readonly_table = table in ("discrete", "input")

        # Bit tables: lock data type to boolean.
        if is_bit_table:
            self._dtype_combo.setEnabled(False)
            for i in range(self._dtype_combo.count()):
                if self._dtype_combo.itemData(i) == "boolean":
                    self._dtype_combo.setCurrentIndex(i)
                    break
        else:
            self._dtype_combo.setEnabled(True)
            # Switch away from boolean if currently on it.
            if self._current_dtype() == "boolean":
                # Select uint16 as default for register tables.
                for i in range(self._dtype_combo.count()):
                    if self._dtype_combo.itemData(i) == "uint16":
                        self._dtype_combo.setCurrentIndex(i)
                        break

        # Read-only tables.
        self._access_rw.setEnabled(not is_readonly_table)
        if is_readonly_table:
            self._access_ro.setChecked(True)

        self._update_notation()
        self._on_dtype_changed(self._dtype_combo.currentIndex())

    def _on_dtype_changed(self, _index: int) -> None:
        dtype = self._current_dtype()
        fixed_count = _REGISTER_COUNTS.get(dtype, 0)
        is_variable = fixed_count == 0 and dtype != "boolean"
        is_boolean = dtype == "boolean"
        is_multi_reg = not is_variable and not is_boolean and fixed_count > 1

        # Auto-fill count if not overridden by user.
        if not self._user_set_count and fixed_count > 0:
            self._count_spin.setValue(fixed_count)

        # Count editable only for string types.
        self._count_spin.setEnabled(is_variable)
        self._count_row_label.setEnabled(is_variable)

        # Byte/word order — hidden for boolean and coils/discrete tables.
        table = self._current_table()
        show_encoding = not is_boolean and table not in ("coils", "discrete")
        self._byte_order_label.setVisible(show_encoding)
        self._byte_order_combo.setVisible(show_encoding)
        self._word_order_label.setVisible(show_encoding and is_multi_reg)
        self._word_order_combo.setVisible(show_encoding and is_multi_reg)

        # Scale/offset — hidden for boolean and string types.
        show_scale = not is_boolean and dtype not in ("ascii", "utf16")
        self._scale_row_label.setVisible(show_scale)
        self._scale_spin.setVisible(show_scale)
        self._offset_row_label.setVisible(show_scale)
        self._offset_spin.setVisible(show_scale)

        self._update_preview()

    def _on_count_edited(self) -> None:
        self._update_preview()

    def _update_notation(self) -> None:
        table = self._current_table()
        address = self._address_spin.value()
        offset = _TABLE_NOTATION_OFFSET.get(table, 1)
        self._notation_label.setText(self.tr("Modbus notation: {n}").format(n=offset + address))
        self._update_preview()

    def _update_preview(self) -> None:
        table = self._current_table()
        address = self._address_spin.value()
        dtype = self._current_dtype()
        count = self._count_spin.value()
        fixed = _REGISTER_COUNTS.get(dtype, 0)
        effective_count = count if fixed == 0 else fixed
        if effective_count == 1:
            oid = f"{table}:{address}"
        else:
            oid = f"{table}:{address}:{effective_count}"
        self._preview_label.setText(self.tr("object_id = \u201c{oid}\u201d").format(oid=oid))

    # ---- public API ------------------------------------------------------

    def object_ref(self) -> ObjectRef:
        """Return the :class:`ObjectRef` for the register the user defined.

        Only call after the dialog has been accepted.
        """
        table = self._current_table()
        address = self._address_spin.value()
        dtype = self._current_dtype()
        fixed = _REGISTER_COUNTS.get(dtype, 0)
        # For fixed-width types, count is authoritative from the codec.
        # For variable types (ascii/utf16), use the user-supplied count.
        effective_count = fixed if fixed > 0 else self._count_spin.value()

        if effective_count == 1:
            object_id = f"{table}:{address}"
        else:
            object_id = f"{table}:{address}:{effective_count}"

        label = self._label_edit.text().strip() or None
        unit = self._unit_edit.text().strip() or None

        access: Access
        if table in ("discrete", "input") or self._access_ro.isChecked():
            access = Access.READ_ONLY
        else:
            access = Access.READ_WRITE

        metadata: dict[str, Any] = {}
        if table not in ("coils", "discrete") and dtype != "boolean":
            bo = self._byte_order_combo.currentData() or "big"
            metadata["byte_order"] = bo
            if effective_count > 1 and dtype not in ("ascii", "utf16"):
                wo = self._word_order_combo.currentData() or "big"
                metadata["word_order"] = wo

        scale = self._scale_spin.value()
        offset = self._offset_spin.value()
        if scale != 1.0:
            metadata["scale"] = scale
        if offset != 0.0:
            metadata["offset"] = offset

        return ObjectRef(
            device=self._device,
            object_id=object_id,
            data_type=dtype,
            access=access,
            label=label,
            unit=unit,
            metadata=metadata,
        )
