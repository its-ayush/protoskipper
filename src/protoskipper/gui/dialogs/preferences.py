# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""PreferencesDialog — Tools → Preferences…

Collects application-wide defaults that survive across sessions. All values
are persisted in QSettings under the "DataSailors"/"ProtoSkipper" org/app
keys. Static helpers expose individual values so other modules (MainWindow,
NewConnectionDialog) can read them without constructing the dialog.
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
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import SessionProfile

_ORG = "DataSailors"
_APP = "ProtoSkipper"
_DEFAULT_AUDIT_DIR = str(Path.home() / ".protoskipper" / "audit")


class PreferencesDialog(QDialog):
    """Tools → Preferences… — persistent default settings.

    Reads current values from QSettings on open and writes them back on OK.
    All visible strings are wrapped in ``self.tr()`` for future i18n.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Preferences"))
        self.setMinimumWidth(480)

        s = QSettings(_ORG, _APP)

        # ---- default operator --------------------------------------------
        self._operator_edit = QLineEdit(s.value("defaults/operator", "", str), self)
        self._operator_edit.setPlaceholderText(self.tr("Your name or email"))
        self._operator_edit.setAccessibleName(self.tr("Default operator email"))

        # ---- audit log directory -----------------------------------------
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

        # ---- layout ------------------------------------------------------
        form = QFormLayout()
        form.addRow(self.tr("Default operator:"), self._operator_edit)
        form.addRow(self.tr("Audit log directory:"), audit_widget)
        form.addRow(self.tr("Default session profile:"), self._profile_combo)
        form.addRow(self.tr("Theme:"), self._theme_combo)
        form.addRow(self.tr("Density:"), self._density_combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
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

    def _save_and_accept(self) -> None:
        s = QSettings(_ORG, _APP)
        s.setValue("defaults/operator", self._operator_edit.text().strip())
        s.setValue("defaults/audit_dir", self._audit_dir_edit.text().strip())
        s.setValue("defaults/profile", self._profile_combo.currentData().value)
        # Store canonical English name regardless of display locale.
        theme_idx = self._theme_combo.currentIndex()
        s.setValue("theme", "Dark" if theme_idx == 1 else "Light")
        density_idx = self._density_combo.currentIndex()
        s.setValue("density", "Compact" if density_idx == 1 else "Comfortable")
        self.accept()

    # ---- static helpers (used by MainWindow / NewConnectionDialog) -------

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
