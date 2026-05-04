# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""PreferencesDialog — Tools → Preferences…

Collects application-wide defaults that survive across sessions. All values
are persisted in QSettings under the "DataSailors"/"ProtoSkipper" org/app
keys. Static helpers expose individual values so other modules (MainWindow,
NewConnectionDialog) can read them without constructing the dialog.

The dialog uses three tabs:
  • General   — operator, audit dir, session profile, theme, density
  • BACnet    — per-protocol defaults (UDP port, APDU timeouts, COV lifetime …)
  • IEC 61850 — per-protocol defaults (network interface, AP-Title, SCL paths)
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import SessionProfile

_ORG = "DataSailors"
_APP = "ProtoSkipper"
_DEFAULT_AUDIT_DIR = str(Path.home() / ".protoskipper" / "audit")

# BACnet defaults (used both in the dialog and by static helpers)
_BACNET_DEFAULT_PORT = 47808
_BACNET_DEFAULT_APDU_TIMEOUT_MS = 3000
_BACNET_DEFAULT_APDU_RETRIES = 3
_BACNET_DEFAULT_COV_LIFETIME_S = 300
_BACNET_DEFAULT_RPM_BATCH_SIZE = 16
_BACNET_DEFAULT_VENDOR_ID = 0

# IEC 61850 defaults
_IEC61850_DEFAULT_IFACE = ""
_IEC61850_DEFAULT_AP_TITLE = "1,3,9999,33"


class PreferencesDialog(QDialog):
    """Tools → Preferences… — persistent default settings.

    Reads current values from QSettings on open and writes them back on OK.
    All visible strings are wrapped in ``self.tr()`` for future i18n.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Preferences"))
        self.setMinimumWidth(520)

        s = QSettings(_ORG, _APP)

        # ================================================================
        # Tab 1 — General
        # ================================================================

        # ---- default operator -------------------------------------------
        self._operator_edit = QLineEdit(s.value("defaults/operator", "", str), self)
        self._operator_edit.setPlaceholderText(self.tr("Your name or email"))
        self._operator_edit.setAccessibleName(self.tr("Default operator email"))

        # ---- audit log directory ----------------------------------------
        self._audit_dir_edit = QLineEdit(
            s.value("defaults/audit_dir", _DEFAULT_AUDIT_DIR, str), self
        )
        self._audit_dir_edit.setAccessibleName(self.tr("Audit log directory path"))

        self._audit_browse = QPushButton(self.tr("Browse…"), self)
        self._audit_browse.setAccessibleName(self.tr("Browse for audit log directory"))
        self._audit_browse.clicked.connect(self._browse_audit_dir)

        audit_widget = QWidget(self)
        audit_row = QHBoxLayout(audit_widget)
        audit_row.setContentsMargins(0, 0, 0, 0)
        audit_row.addWidget(self._audit_dir_edit)
        audit_row.addWidget(self._audit_browse)

        # ---- default session profile ------------------------------------
        self._profile_combo = QComboBox(self)
        for p in SessionProfile:
            self._profile_combo.addItem(p.value.capitalize(), p)
        saved_profile_str = s.value("defaults/profile", SessionProfile.LAB.value, str)
        try:
            saved_profile = SessionProfile(saved_profile_str)
        except ValueError:
            saved_profile = SessionProfile.LAB
        idx = next(
            (
                i
                for i in range(self._profile_combo.count())
                if self._profile_combo.itemData(i) == saved_profile
            ),
            0,
        )
        self._profile_combo.setCurrentIndex(idx)
        self._profile_combo.setAccessibleName(self.tr("Default session profile"))

        # ---- theme -------------------------------------------------------
        self._theme_combo = QComboBox(self)
        self._theme_combo.addItems([self.tr("Light"), self.tr("Dark")])
        saved_theme = s.value("theme", "Light", str)
        self._theme_combo.setCurrentText(self.tr(saved_theme))
        self._theme_combo.setAccessibleName(self.tr("Application colour theme"))

        # ---- density -----------------------------------------------------
        self._density_combo = QComboBox(self)
        self._density_combo.addItems([self.tr("Comfortable"), self.tr("Compact")])
        saved_density = s.value("density", "Comfortable", str)
        self._density_combo.setCurrentText(self.tr(saved_density))
        self._density_combo.setAccessibleName(self.tr("Interface density"))

        general_form = QFormLayout()
        general_form.addRow(self.tr("Default operator:"), self._operator_edit)
        general_form.addRow(self.tr("Audit log directory:"), audit_widget)
        general_form.addRow(self.tr("Default session profile:"), self._profile_combo)
        general_form.addRow(self.tr("Theme:"), self._theme_combo)
        general_form.addRow(self.tr("Density:"), self._density_combo)

        general_tab = QWidget(self)
        general_layout = QVBoxLayout(general_tab)
        general_layout.addLayout(general_form)
        general_layout.addStretch()

        # ================================================================
        # Tab 2 — BACnet
        # ================================================================

        # --- local UDP port ---------------------------------------------
        self._bacnet_port_spin = QSpinBox(self)
        self._bacnet_port_spin.setRange(1, 65535)
        self._bacnet_port_spin.setValue(
            int(s.value("bacnet/local_port", _BACNET_DEFAULT_PORT, int))
        )
        self._bacnet_port_spin.setAccessibleName(self.tr("BACnet local UDP port"))

        # --- APDU timeout -----------------------------------------------
        self._bacnet_apdu_timeout_spin = QSpinBox(self)
        self._bacnet_apdu_timeout_spin.setRange(500, 30000)
        self._bacnet_apdu_timeout_spin.setSingleStep(500)
        self._bacnet_apdu_timeout_spin.setSuffix(self.tr(" ms"))
        self._bacnet_apdu_timeout_spin.setValue(
            int(s.value("bacnet/apdu_timeout_ms", _BACNET_DEFAULT_APDU_TIMEOUT_MS, int))
        )
        self._bacnet_apdu_timeout_spin.setAccessibleName(self.tr("APDU timeout in milliseconds"))

        # --- APDU retries -----------------------------------------------
        self._bacnet_apdu_retries_spin = QSpinBox(self)
        self._bacnet_apdu_retries_spin.setRange(0, 7)
        self._bacnet_apdu_retries_spin.setValue(
            int(s.value("bacnet/apdu_retries", _BACNET_DEFAULT_APDU_RETRIES, int))
        )
        self._bacnet_apdu_retries_spin.setAccessibleName(self.tr("Number of APDU retries"))

        # --- COV default lifetime ----------------------------------------
        self._bacnet_cov_lifetime_spin = QSpinBox(self)
        self._bacnet_cov_lifetime_spin.setRange(0, 86400)
        self._bacnet_cov_lifetime_spin.setSingleStep(60)
        self._bacnet_cov_lifetime_spin.setSuffix(self.tr(" s  (0 = indefinite)"))
        self._bacnet_cov_lifetime_spin.setValue(
            int(s.value("bacnet/cov_lifetime_s", _BACNET_DEFAULT_COV_LIFETIME_S, int))
        )
        self._bacnet_cov_lifetime_spin.setAccessibleName(
            self.tr("Default COV subscription lifetime in seconds")
        )

        # --- RPM batch size ----------------------------------------------
        self._bacnet_rpm_batch_spin = QSpinBox(self)
        self._bacnet_rpm_batch_spin.setRange(1, 200)
        self._bacnet_rpm_batch_spin.setValue(
            int(s.value("bacnet/rpm_batch_size", _BACNET_DEFAULT_RPM_BATCH_SIZE, int))
        )
        self._bacnet_rpm_batch_spin.setAccessibleName(
            self.tr("Number of properties per ReadPropertyMultiple request")
        )

        # --- vendor ID ---------------------------------------------------
        self._bacnet_vendor_id_spin = QSpinBox(self)
        self._bacnet_vendor_id_spin.setRange(0, 65535)
        self._bacnet_vendor_id_spin.setValue(
            int(s.value("bacnet/vendor_id", _BACNET_DEFAULT_VENDOR_ID, int))
        )
        self._bacnet_vendor_id_spin.setAccessibleName(
            self.tr("Vendor ID presented in I-Am responses (0 = ASHRAE)")
        )

        # --- Who-Is range ------------------------------------------------
        self._bacnet_who_is_range_edit = QLineEdit(self)
        self._bacnet_who_is_range_edit.setText(s.value("bacnet/who_is_range", "", str))
        self._bacnet_who_is_range_edit.setPlaceholderText(
            self.tr("e.g. 1-1000,2000  (empty = all 0-4194302)")
        )
        self._bacnet_who_is_range_edit.setAccessibleName(self.tr("Default Who-Is device ID range"))

        bacnet_form = QFormLayout()
        bacnet_form.addRow(self.tr("Local UDP port:"), self._bacnet_port_spin)
        bacnet_form.addRow(self.tr("APDU timeout:"), self._bacnet_apdu_timeout_spin)
        bacnet_form.addRow(self.tr("APDU retries:"), self._bacnet_apdu_retries_spin)
        bacnet_form.addRow(self.tr("COV default lifetime:"), self._bacnet_cov_lifetime_spin)
        bacnet_form.addRow(self.tr("RPM batch size:"), self._bacnet_rpm_batch_spin)
        bacnet_form.addRow(self.tr("Vendor ID (I-Am):"), self._bacnet_vendor_id_spin)
        bacnet_form.addRow(self.tr("Who-Is range:"), self._bacnet_who_is_range_edit)

        bacnet_tab = QWidget(self)
        bacnet_layout = QVBoxLayout(bacnet_tab)
        bacnet_layout.addWidget(
            QLabel(
                self.tr("These values are used as defaults when opening new BACnet sessions."),
                self,
            )
        )
        bacnet_layout.addSpacing(8)
        bacnet_layout.addLayout(bacnet_form)
        bacnet_layout.addStretch()

        # ================================================================
        # Tab widget
        # ================================================================
        self._tabs = QTabWidget(self)
        self._tabs.addTab(general_tab, self.tr("General"))
        self._tabs.addTab(bacnet_tab, self.tr("BACnet"))
        self._tabs.addTab(self._build_iec61850_tab(s), self.tr("IEC 61850"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs)
        layout.addWidget(buttons)

    # ---- private handlers -----------------------------------------------

    def _browse_audit_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            self.tr("Choose audit log directory"),
            self._audit_dir_edit.text(),
        )
        if chosen:
            self._audit_dir_edit.setText(chosen)

    def _browse_dir(self, line_edit: QLineEdit, title: str) -> None:
        """Generic directory-picker helper shared between IEC 61850 path fields."""
        chosen = QFileDialog.getExistingDirectory(self, self.tr(title), line_edit.text())
        if chosen:
            line_edit.setText(chosen)

    def _build_iec61850_tab(self, s: QSettings) -> QWidget:
        """Construct and return the IEC 61850 preferences tab."""
        # ---- default network interface ----------------------------------
        self._iec61850_iface_edit = QLineEdit(self)
        self._iec61850_iface_edit.setText(
            s.value("iec61850/default_iface", _IEC61850_DEFAULT_IFACE, str)
        )
        self._iec61850_iface_edit.setPlaceholderText(self.tr("e.g. eth0  (empty = auto)"))
        self._iec61850_iface_edit.setAccessibleName(
            self.tr("Default network interface for GOOSE/SV")
        )

        # ---- default AP-Title -------------------------------------------
        self._iec61850_ap_title_edit = QLineEdit(self)
        self._iec61850_ap_title_edit.setText(
            s.value("iec61850/ap_title", _IEC61850_DEFAULT_AP_TITLE, str)
        )
        self._iec61850_ap_title_edit.setPlaceholderText(self.tr("e.g. 1,3,9999,33"))
        self._iec61850_ap_title_edit.setAccessibleName(self.tr("Local AP-Title OID"))

        # ---- SCL search path --------------------------------------------
        self._iec61850_scl_path_edit = QLineEdit(self)
        self._iec61850_scl_path_edit.setText(s.value("iec61850/scl_search_path", "", str))
        self._iec61850_scl_path_edit.setPlaceholderText(
            self.tr("Directory to search for .icd/.cid/.scd files")
        )
        self._iec61850_scl_path_edit.setAccessibleName(self.tr("SCL file search path"))

        scl_browse = QPushButton(self.tr("Browse…"), self)
        scl_browse.setAccessibleName(self.tr("Browse for SCL search directory"))
        scl_browse.clicked.connect(
            lambda: self._browse_dir(self._iec61850_scl_path_edit, "Choose SCL search directory")
        )

        scl_widget = QWidget(self)
        scl_row = QHBoxLayout(scl_widget)
        scl_row.setContentsMargins(0, 0, 0, 0)
        scl_row.addWidget(self._iec61850_scl_path_edit)
        scl_row.addWidget(scl_browse)

        # ---- vendor profile library path --------------------------------
        self._iec61850_vendor_path_edit = QLineEdit(self)
        self._iec61850_vendor_path_edit.setText(s.value("iec61850/vendor_profile_path", "", str))
        self._iec61850_vendor_path_edit.setPlaceholderText(
            self.tr("Directory containing vendor .json profile files")
        )
        self._iec61850_vendor_path_edit.setAccessibleName(self.tr("Vendor profile library path"))

        vendor_browse = QPushButton(self.tr("Browse…"), self)
        vendor_browse.setAccessibleName(self.tr("Browse for vendor profile library directory"))
        vendor_browse.clicked.connect(
            lambda: self._browse_dir(
                self._iec61850_vendor_path_edit, "Choose vendor profile directory"
            )
        )

        vendor_widget = QWidget(self)
        vendor_row = QHBoxLayout(vendor_widget)
        vendor_row.setContentsMargins(0, 0, 0, 0)
        vendor_row.addWidget(self._iec61850_vendor_path_edit)
        vendor_row.addWidget(vendor_browse)

        # ---- form -------------------------------------------------------
        iec_form = QFormLayout()
        iec_form.addRow(self.tr("Default network interface:"), self._iec61850_iface_edit)
        iec_form.addRow(self.tr("Local AP-Title:"), self._iec61850_ap_title_edit)
        iec_form.addRow(self.tr("SCL search path:"), scl_widget)
        iec_form.addRow(self.tr("Vendor profile path:"), vendor_widget)

        tab = QWidget(self)
        tab_layout = QVBoxLayout(tab)
        tab_layout.addWidget(
            QLabel(
                self.tr("These values are used as defaults when opening new IEC 61850 sessions."),
                self,
            )
        )
        tab_layout.addSpacing(8)
        tab_layout.addLayout(iec_form)
        tab_layout.addStretch()
        return tab

    def _save_and_accept(self) -> None:
        s = QSettings(_ORG, _APP)

        # General
        s.setValue("defaults/operator", self._operator_edit.text().strip())
        s.setValue("defaults/audit_dir", self._audit_dir_edit.text().strip())
        s.setValue("defaults/profile", self._profile_combo.currentData().value)
        theme_idx = self._theme_combo.currentIndex()
        s.setValue("theme", "Dark" if theme_idx == 1 else "Light")
        density_idx = self._density_combo.currentIndex()
        s.setValue("density", "Compact" if density_idx == 1 else "Comfortable")

        # BACnet
        s.setValue("bacnet/local_port", self._bacnet_port_spin.value())
        s.setValue("bacnet/apdu_timeout_ms", self._bacnet_apdu_timeout_spin.value())
        s.setValue("bacnet/apdu_retries", self._bacnet_apdu_retries_spin.value())
        s.setValue("bacnet/cov_lifetime_s", self._bacnet_cov_lifetime_spin.value())
        s.setValue("bacnet/rpm_batch_size", self._bacnet_rpm_batch_spin.value())
        s.setValue("bacnet/vendor_id", self._bacnet_vendor_id_spin.value())
        s.setValue("bacnet/who_is_range", self._bacnet_who_is_range_edit.text().strip())

        # IEC 61850
        s.setValue("iec61850/default_iface", self._iec61850_iface_edit.text().strip())
        s.setValue("iec61850/ap_title", self._iec61850_ap_title_edit.text().strip())
        s.setValue("iec61850/scl_search_path", self._iec61850_scl_path_edit.text().strip())
        s.setValue("iec61850/vendor_profile_path", self._iec61850_vendor_path_edit.text().strip())

        self.accept()

    # ---- static helpers (General) ---------------------------------------

    @staticmethod
    def default_operator() -> str:
        """Return the saved default operator string, or empty string."""
        return QSettings(_ORG, _APP).value("defaults/operator", "", str)

    @staticmethod
    def saved_theme() -> str:
        """Return "Light" or "Dark"."""
        return QSettings(_ORG, _APP).value("theme", "Light", str)

    @staticmethod
    def saved_density() -> str:
        """Return "Comfortable" or "Compact"."""
        return QSettings(_ORG, _APP).value("density", "Comfortable", str)

    @staticmethod
    def saved_audit_dir() -> Path:
        """Return the saved audit log directory path."""
        return Path(QSettings(_ORG, _APP).value("defaults/audit_dir", _DEFAULT_AUDIT_DIR, str))

    @staticmethod
    def saved_profile() -> SessionProfile:
        """Return the saved default SessionProfile."""
        raw = QSettings(_ORG, _APP).value("defaults/profile", SessionProfile.LAB.value, str)
        try:
            return SessionProfile(raw)
        except ValueError:
            return SessionProfile.LAB

    # ---- static helpers (BACnet) ----------------------------------------

    @staticmethod
    def bacnet_local_port() -> int:
        """Return the saved BACnet local UDP port (default 47808)."""
        return int(QSettings(_ORG, _APP).value("bacnet/local_port", _BACNET_DEFAULT_PORT, int))

    @staticmethod
    def bacnet_apdu_timeout_ms() -> int:
        """Return the saved APDU timeout in milliseconds (default 3000)."""
        return int(
            QSettings(_ORG, _APP).value(
                "bacnet/apdu_timeout_ms", _BACNET_DEFAULT_APDU_TIMEOUT_MS, int
            )
        )

    @staticmethod
    def bacnet_apdu_retries() -> int:
        """Return the saved number of APDU retries (default 3)."""
        return int(
            QSettings(_ORG, _APP).value("bacnet/apdu_retries", _BACNET_DEFAULT_APDU_RETRIES, int)
        )

    @staticmethod
    def bacnet_cov_lifetime_s() -> int:
        """Return the saved COV subscription lifetime in seconds (default 300)."""
        return int(
            QSettings(_ORG, _APP).value(
                "bacnet/cov_lifetime_s", _BACNET_DEFAULT_COV_LIFETIME_S, int
            )
        )

    @staticmethod
    def bacnet_rpm_batch_size() -> int:
        """Return the saved RPM batch size (default 16)."""
        return int(
            QSettings(_ORG, _APP).value(
                "bacnet/rpm_batch_size", _BACNET_DEFAULT_RPM_BATCH_SIZE, int
            )
        )

    @staticmethod
    def bacnet_vendor_id() -> int:
        """Return the saved vendor ID for I-Am responses (default 0 = ASHRAE)."""
        return int(QSettings(_ORG, _APP).value("bacnet/vendor_id", _BACNET_DEFAULT_VENDOR_ID, int))

    @staticmethod
    def bacnet_who_is_range() -> str:
        """Return the saved Who-Is range string (empty = all devices)."""
        return QSettings(_ORG, _APP).value("bacnet/who_is_range", "", str)

    # ---- static helpers (IEC 61850) -------------------------------------

    @staticmethod
    def iec61850_default_iface() -> str:
        """Return the saved default network interface (empty = auto-select)."""
        return QSettings(_ORG, _APP).value("iec61850/default_iface", _IEC61850_DEFAULT_IFACE, str)

    @staticmethod
    def iec61850_ap_title() -> str:
        """Return the saved local AP-Title OID string (default '1,3,9999,33')."""
        return QSettings(_ORG, _APP).value("iec61850/ap_title", _IEC61850_DEFAULT_AP_TITLE, str)

    @staticmethod
    def iec61850_scl_search_path() -> str:
        """Return the saved SCL file search path (empty = not configured)."""
        return QSettings(_ORG, _APP).value("iec61850/scl_search_path", "", str)

    @staticmethod
    def iec61850_vendor_profile_path() -> str:
        """Return the saved vendor profile library path (empty = not configured)."""
        return QSettings(_ORG, _APP).value("iec61850/vendor_profile_path", "", str)
