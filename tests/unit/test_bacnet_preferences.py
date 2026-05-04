# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for P7.I.2: BACnet preferences static helpers in PreferencesDialog.

These tests exercise only the static helper methods (which read from QSettings).
They do not construct the full dialog widget, so no QApplication is needed.
"""

from __future__ import annotations

import pytest

# QSettings requires an application to exist for some backends.
# Use conftest's QApplication fixture (or create a minimal one here).
from PySide6.QtCore import QCoreApplication, QSettings

# Ensure a QCoreApplication exists for the whole test module.
# conftest.py at the unit level does not create one, so we do it here.
if QCoreApplication.instance() is None:
    _app = QCoreApplication([])

from protoskipper.gui.dialogs.preferences import (
    _APP,
    _BACNET_DEFAULT_APDU_RETRIES,
    _BACNET_DEFAULT_APDU_TIMEOUT_MS,
    _BACNET_DEFAULT_COV_LIFETIME_S,
    _BACNET_DEFAULT_PORT,
    _BACNET_DEFAULT_RPM_BATCH_SIZE,
    _BACNET_DEFAULT_VENDOR_ID,
    _ORG,
    PreferencesDialog,
)

_SETTINGS_KEYS = [
    "bacnet/local_port",
    "bacnet/apdu_timeout_ms",
    "bacnet/apdu_retries",
    "bacnet/cov_lifetime_s",
    "bacnet/rpm_batch_size",
    "bacnet/vendor_id",
    "bacnet/who_is_range",
]


@pytest.fixture(autouse=True)
def _clean_bacnet_settings():
    """Remove BACnet keys from QSettings before and after each test."""
    s = QSettings(_ORG, _APP)
    for key in _SETTINGS_KEYS:
        s.remove(key)
    s.sync()
    yield
    s = QSettings(_ORG, _APP)
    for key in _SETTINGS_KEYS:
        s.remove(key)
    s.sync()


class TestBacnetPreferencesDefaults:
    def test_local_port_default(self) -> None:
        assert PreferencesDialog.bacnet_local_port() == _BACNET_DEFAULT_PORT

    def test_apdu_timeout_default(self) -> None:
        assert PreferencesDialog.bacnet_apdu_timeout_ms() == _BACNET_DEFAULT_APDU_TIMEOUT_MS

    def test_apdu_retries_default(self) -> None:
        assert PreferencesDialog.bacnet_apdu_retries() == _BACNET_DEFAULT_APDU_RETRIES

    def test_cov_lifetime_default(self) -> None:
        assert PreferencesDialog.bacnet_cov_lifetime_s() == _BACNET_DEFAULT_COV_LIFETIME_S

    def test_rpm_batch_size_default(self) -> None:
        assert PreferencesDialog.bacnet_rpm_batch_size() == _BACNET_DEFAULT_RPM_BATCH_SIZE

    def test_vendor_id_default(self) -> None:
        assert PreferencesDialog.bacnet_vendor_id() == _BACNET_DEFAULT_VENDOR_ID

    def test_who_is_range_default(self) -> None:
        assert PreferencesDialog.bacnet_who_is_range() == ""


class TestBacnetPreferencesSaved:
    def test_local_port_saved(self) -> None:
        QSettings(_ORG, _APP).setValue("bacnet/local_port", 47809)
        assert PreferencesDialog.bacnet_local_port() == 47809

    def test_apdu_timeout_saved(self) -> None:
        QSettings(_ORG, _APP).setValue("bacnet/apdu_timeout_ms", 5000)
        assert PreferencesDialog.bacnet_apdu_timeout_ms() == 5000

    def test_apdu_retries_saved(self) -> None:
        QSettings(_ORG, _APP).setValue("bacnet/apdu_retries", 5)
        assert PreferencesDialog.bacnet_apdu_retries() == 5

    def test_cov_lifetime_saved(self) -> None:
        QSettings(_ORG, _APP).setValue("bacnet/cov_lifetime_s", 600)
        assert PreferencesDialog.bacnet_cov_lifetime_s() == 600

    def test_rpm_batch_size_saved(self) -> None:
        QSettings(_ORG, _APP).setValue("bacnet/rpm_batch_size", 32)
        assert PreferencesDialog.bacnet_rpm_batch_size() == 32

    def test_vendor_id_saved(self) -> None:
        QSettings(_ORG, _APP).setValue("bacnet/vendor_id", 999)
        assert PreferencesDialog.bacnet_vendor_id() == 999

    def test_who_is_range_saved(self) -> None:
        QSettings(_ORG, _APP).setValue("bacnet/who_is_range", "1-1000,2000")
        assert PreferencesDialog.bacnet_who_is_range() == "1-1000,2000"


class TestBacnetPreferencesReturnTypes:
    """All numeric helpers must return int, not str (QSettings can serialize as str)."""

    def test_local_port_is_int(self) -> None:
        assert isinstance(PreferencesDialog.bacnet_local_port(), int)

    def test_apdu_timeout_is_int(self) -> None:
        assert isinstance(PreferencesDialog.bacnet_apdu_timeout_ms(), int)

    def test_apdu_retries_is_int(self) -> None:
        assert isinstance(PreferencesDialog.bacnet_apdu_retries(), int)

    def test_cov_lifetime_is_int(self) -> None:
        assert isinstance(PreferencesDialog.bacnet_cov_lifetime_s(), int)

    def test_rpm_batch_size_is_int(self) -> None:
        assert isinstance(PreferencesDialog.bacnet_rpm_batch_size(), int)

    def test_vendor_id_is_int(self) -> None:
        assert isinstance(PreferencesDialog.bacnet_vendor_id(), int)

    def test_who_is_range_is_str(self) -> None:
        assert isinstance(PreferencesDialog.bacnet_who_is_range(), str)


class TestBacnetPreferencesRanges:
    """Saved values round-trip correctly for boundary values."""

    def test_cov_lifetime_zero(self) -> None:
        """0 = indefinite subscription, must be allowed."""
        QSettings(_ORG, _APP).setValue("bacnet/cov_lifetime_s", 0)
        assert PreferencesDialog.bacnet_cov_lifetime_s() == 0

    def test_vendor_id_max(self) -> None:
        """65535 is the max unsigned 16-bit vendor ID."""
        QSettings(_ORG, _APP).setValue("bacnet/vendor_id", 65535)
        assert PreferencesDialog.bacnet_vendor_id() == 65535

    def test_port_non_standard(self) -> None:
        """A non-standard port (e.g. 47900) must be preserved."""
        QSettings(_ORG, _APP).setValue("bacnet/local_port", 47900)
        assert PreferencesDialog.bacnet_local_port() == 47900
