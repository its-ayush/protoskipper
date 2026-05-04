# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""IED Simulator wizard dialog — P8.G.2.

Wraps the :class:`~protoskipper_iec61850.simulator.IedSimulator` core
with a simple GUI that lets an operator:

1. Choose an SCL file and the IED name to simulate.
2. Configure the MMS listen address and port.
3. Browse the registered data-attribute points and set initial values.
4. Start and stop the simulator with one click.

Design notes
------------
* **No driver calls in the GUI layer** — the dialog only imports
  ``protoskipper_iec61850.simulator`` and ``protoskipper_iec61850.scl``;
  both are lazy-imported so the dialog can be loaded without the plugin
  installed.
* **Thread safety** — :class:`~protoskipper_iec61850.simulator.IedSimulator`
  documents that all control calls must come from the same thread.
  Because ``IedServer_start()`` is non-blocking (libiec61850 spawns its
  own C threads for clients), the simulator is safe to drive from the
  Qt main thread.
* **PRODUCTION profile guard** — starting the simulator in PRODUCTION
  profile requires a re-confirmation via :class:`QMessageBox` every time.

The ``protoskipper_iec61850`` plugin must be installed at runtime.  If it
is absent, all loading actions show an actionable :class:`QMessageBox`.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from protoskipper.core.driver import SessionProfile

_log = logging.getLogger(__name__)

_PLUGIN_INSTALL_HINT = (
    "The protoskipper-iec61850 plugin must be installed:\n  pip install protoskipper-iec61850[scl]"
)

# Column indices for the DA table
_COL_REF = 0
_COL_FC = 1
_COL_TYPE = 2
_COL_VALUE = 3


class IedSimulatorDialog(QDialog):
    """Wizard-style dialog for configuring and running the IED simulator.

    The dialog keeps one :class:`~protoskipper_iec61850.simulator.IedSimulator`
    instance alive for as long as it is open (if the user clicks *Start*).
    Closing the dialog stops any running simulator automatically.

    Parameters
    ----------
    parent:
        Parent widget.
    profile:
        Current session profile (LAB / COMMISSIONING / PRODUCTION).
        PRODUCTION requires extra confirmation before starting.
    scl_path:
        Optional pre-populated SCL file path (e.g. from the main connection
        dialog or a recently opened file).
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        profile: SessionProfile | None = None,
        scl_path: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("IED Simulator"))
        self.setMinimumSize(780, 580)

        self._profile = profile
        self._sim: Any = None  # IedSimulator | None
        self._scl_doc: Any = None  # SclDocument | None
        self._da_refs: list[str] = []  # ordered list of point refs

        self._build_ui(scl_path)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, scl_path: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(splitter, stretch=1)

        splitter.addWidget(self._build_config_panel(scl_path))
        splitter.addWidget(self._build_da_panel())
        splitter.setSizes([220, 360])

        self._status_lbl = QLabel(self.tr("Not started"))
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._status_lbl)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.reject)
        root.addWidget(btn_box)

    def _build_config_panel(self, scl_path: str) -> QWidget:
        """Upper half: SCL file, IED selection, network settings."""
        group = QGroupBox(self.tr("Configuration"))
        form = QFormLayout(group)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        # SCL path row
        scl_row = QWidget()
        scl_hl = QHBoxLayout(scl_row)
        scl_hl.setContentsMargins(0, 0, 0, 0)
        self._scl_edit = QLineEdit(scl_path)
        self._scl_edit.setPlaceholderText(self.tr("Path to .icd / .cid / .scd file"))
        self._scl_edit.setReadOnly(True)
        scl_browse = QPushButton(self.tr("Browse…"))
        scl_browse.clicked.connect(self._browse_scl)
        self._load_btn = QPushButton(self.tr("Load IED"))
        self._load_btn.clicked.connect(self._load_scl)
        scl_hl.addWidget(self._scl_edit, stretch=1)
        scl_hl.addWidget(scl_browse)
        scl_hl.addWidget(self._load_btn)
        form.addRow(self.tr("SCL file:"), scl_row)

        # IED name
        self._ied_combo = QComboBox()
        self._ied_combo.setEnabled(False)
        self._ied_combo.currentTextChanged.connect(self._on_ied_selected)
        form.addRow(self.tr("IED name:"), self._ied_combo)

        # Network settings
        net_row = QWidget()
        net_hl = QHBoxLayout(net_row)
        net_hl.setContentsMargins(0, 0, 0, 0)
        self._host_edit = QLineEdit("0.0.0.0")
        self._host_edit.setMaximumWidth(180)
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(102)
        net_hl.addWidget(QLabel(self.tr("Host:")))
        net_hl.addWidget(self._host_edit)
        net_hl.addSpacing(12)
        net_hl.addWidget(QLabel(self.tr("Port:")))
        net_hl.addWidget(self._port_spin)
        net_hl.addStretch()
        form.addRow(self.tr("Listen:"), net_row)

        # Start / Stop
        ctrl_row = QWidget()
        ctrl_hl = QHBoxLayout(ctrl_row)
        ctrl_hl.setContentsMargins(0, 0, 0, 0)
        self._start_btn = QPushButton(self.tr("Start Simulator"))
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._on_start)
        self._stop_btn = QPushButton(self.tr("Stop Simulator"))
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        ctrl_hl.addWidget(self._start_btn)
        ctrl_hl.addWidget(self._stop_btn)
        ctrl_hl.addStretch()
        form.addRow("", ctrl_row)

        return group

    def _build_da_panel(self) -> QWidget:
        """Lower half: table of data-attribute points."""
        group = QGroupBox(self.tr("Data Attribute Points"))
        vl = QVBoxLayout(group)

        self._da_table = QTableWidget()
        self._da_table.setColumnCount(4)
        self._da_table.setHorizontalHeaderLabels(
            [
                self.tr("Reference"),
                self.tr("FC"),
                self.tr("Type"),
                self.tr("Initial value"),
            ]
        )
        self._da_table.horizontalHeader().setSectionResizeMode(
            _COL_REF, QHeaderView.ResizeMode.Stretch
        )
        self._da_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._da_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self._da_table.itemChanged.connect(self._on_value_edited)
        vl.addWidget(self._da_table)

        hint = QLabel(
            self.tr(
                "Double-click the <i>Initial value</i> column to pre-set a value "
                "before starting, or edit it while the simulator is running."
            )
        )
        hint.setWordWrap(True)
        vl.addWidget(hint)

        return group

    # ------------------------------------------------------------------
    # SCL loading
    # ------------------------------------------------------------------

    def _browse_scl(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Open SCL File"),
            self._scl_edit.text() or str(Path.home()),
            self.tr("SCL Files (*.icd *.cid *.scd *.iid);;All Files (*)"),
        )
        if path:
            self._scl_edit.setText(path)
            self._scl_doc = None
            self._ied_combo.setEnabled(False)
            self._ied_combo.clear()
            self._start_btn.setEnabled(False)
            self._da_table.setRowCount(0)

    def _load_scl(self) -> None:
        path = self._scl_edit.text().strip()
        if not path:
            QMessageBox.warning(self, self.tr("No File"), self.tr("Select an SCL file first."))
            return
        try:
            from protoskipper_iec61850.scl import parse
        except ImportError:
            QMessageBox.critical(self, self.tr("Plugin not installed"), _PLUGIN_INSTALL_HINT)
            return
        try:
            doc = parse(path)
        except Exception as exc:
            QMessageBox.critical(
                self,
                self.tr("SCL Parse Error"),
                self.tr("Could not parse %1:\n%2").arg(path).arg(str(exc)),
            )
            return

        self._scl_doc = doc
        self._ied_combo.clear()
        for ied in doc.ieds:
            self._ied_combo.addItem(ied.name)
        self._ied_combo.setEnabled(len(doc.ieds) > 0)
        if doc.ieds:
            self._on_ied_selected(doc.ieds[0].name)

    def _on_ied_selected(self, ied_name: str) -> None:
        if self._scl_doc is None or not ied_name:
            return
        try:
            from protoskipper_iec61850.simulator import (
                _collect_points,
                _ied_for_name,
            )
        except ImportError:
            return

        try:
            ied = _ied_for_name(self._scl_doc, ied_name)
        except Exception:
            return

        points = _collect_points(ied)
        self._da_refs = sorted(points.keys())
        self._populate_da_table(points)
        self._start_btn.setEnabled(True)

    def _populate_da_table(self, points: dict) -> None:
        self._da_table.blockSignals(True)
        self._da_table.setRowCount(len(self._da_refs))
        for row, ref in enumerate(self._da_refs):
            pt = points[ref]
            # Reference (read-only)
            ref_item = QTableWidgetItem(ref)
            ref_item.setFlags(ref_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._da_table.setItem(row, _COL_REF, ref_item)
            # FC (read-only)
            fc_item = QTableWidgetItem(pt.fc)
            fc_item.setFlags(fc_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._da_table.setItem(row, _COL_FC, fc_item)
            # Type (read-only, human-readable)
            type_item = QTableWidgetItem(_mms_type_name(pt.mms_type))
            type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._da_table.setItem(row, _COL_TYPE, type_item)
            # Value (editable)
            val_str = "" if pt.value is None else str(pt.value)
            self._da_table.setItem(row, _COL_VALUE, QTableWidgetItem(val_str))
        self._da_table.blockSignals(False)

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        ied_name = self._ied_combo.currentText()
        if not ied_name or self._scl_doc is None:
            return
        if self._sim is not None and getattr(self._sim, "is_running", False):
            return

        # PRODUCTION guard
        from protoskipper.core.driver import SessionProfile

        if self._profile is SessionProfile.PRODUCTION:
            ans = QMessageBox.question(
                self,
                self.tr("Production Profile"),
                self.tr(
                    "Starting the IED simulator in PRODUCTION profile will "
                    "expose a live MMS server on the network.\n\n"
                    "Are you sure you want to continue?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return

        try:
            from protoskipper_iec61850.simulator import (
                IedSimulator,
                SimulatorConfig,
            )
        except ImportError:
            QMessageBox.critical(self, self.tr("Plugin not installed"), _PLUGIN_INSTALL_HINT)
            return

        cfg = SimulatorConfig(
            ied_name=ied_name,
            port=self._port_spin.value(),
            host=self._host_edit.text().strip() or "0.0.0.0",
        )

        # Apply any initial values from the table
        initial_values = self._collect_initial_values()

        try:
            sim = IedSimulator(cfg, self._scl_doc)
            for ref, val in initial_values.items():
                with contextlib.suppress(Exception):
                    sim.update_da(ref, val)
            sim.start()
        except Exception as exc:
            QMessageBox.critical(
                self,
                self.tr("Simulator Error"),
                self.tr("Failed to start simulator:\n%1").arg(str(exc)),
            )
            return

        self._sim = sim
        self._set_running(running=True)

    def _on_stop(self) -> None:
        if self._sim is not None:
            try:
                self._sim.stop()
            except Exception:
                _log.exception("Error stopping simulator")
            self._sim = None
        self._set_running(running=False)

    def _set_running(self, *, running: bool) -> None:
        self._start_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._ied_combo.setEnabled(not running)
        self._load_btn.setEnabled(not running)
        self._host_edit.setReadOnly(running)
        self._port_spin.setEnabled(not running)

        if running:
            ied = self._ied_combo.currentText()
            port = self._port_spin.value()
            self._status_lbl.setText(
                self.tr("Running — IED: %1 — port: %2").arg(ied).arg(str(port))
            )
        else:
            self._status_lbl.setText(self.tr("Stopped"))

    # ------------------------------------------------------------------
    # Live value editing
    # ------------------------------------------------------------------

    def _on_value_edited(self, item: QTableWidgetItem) -> None:
        if item.column() != _COL_VALUE:
            return
        row = item.row()
        if row < 0 or row >= len(self._da_refs):
            return
        ref = self._da_refs[row]
        raw = item.text().strip()
        if self._sim is None or not getattr(self._sim, "is_running", False):
            return
        # Coerce the string to a Python value and push to the running server
        value = _coerce_value(raw)
        try:
            self._sim.update_da(ref, value)
        except Exception:
            _log.warning("Failed to update DA %s = %r", ref, value)

    def _collect_initial_values(self) -> dict[str, Any]:
        """Return a dict of {ref: coerced_value} from the table (non-empty only)."""
        result: dict[str, Any] = {}
        for row, ref in enumerate(self._da_refs):
            item = self._da_table.item(row, _COL_VALUE)
            if item is None:
                continue
            raw = item.text().strip()
            if raw:
                result[ref] = _coerce_value(raw)
        return result

    # ------------------------------------------------------------------
    # Close / cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event: Any) -> None:
        self._on_stop()
        super().closeEvent(event)

    def reject(self) -> None:
        self._on_stop()
        super().reject()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MMS_TYPE_NAMES: dict[int, str] = {
    1: "STRUCTURE",
    2: "BOOLEAN",
    3: "BIT_STRING",
    4: "INTEGER",
    5: "UNSIGNED",
    6: "FLOAT32",
    7: "OCTET_STRING",
    8: "VISIBLE_STRING",
    14: "UTC_TIME",
}


def _mms_type_name(mms_type: int) -> str:
    return _MMS_TYPE_NAMES.get(mms_type, f"MMS_{mms_type}")


def _coerce_value(raw: str) -> float | bool | int | str:
    """Best-effort coercion of a user-entered string to a Python value."""
    low = raw.lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    try:
        if "." in raw or "e" in low:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw
