# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""GUI smoke tests for IEC 104 in NewConnectionDialog (P4.F).

Verifies that selecting the ``iec104.tcp`` driver reskins the dialog so
it asks for a Common Address (CA) instead of a Modbus Unit ID, defaults
to TCP port 2404, and produces an address string with the ``/ca=N``
suffix that :func:`parse_address` understands.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from protoskipper.builtin_drivers.iec104.driver import parse_address
from protoskipper.core.driver import SessionProfile
from protoskipper.gui.dialogs.new_connection import NewConnectionDialog

pytestmark = pytest.mark.gui


@pytest.fixture(autouse=True)
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _select_protocol(dlg: NewConnectionDialog, proto_id: str) -> bool:
    combo = dlg._protocol_combo  # type: ignore[attr-defined]
    for i in range(combo.count()):
        if combo.itemData(i) == proto_id:
            combo.setCurrentIndex(i)
            return True
    return False


def test_iec104_protocol_changes_address_label_and_default_port() -> None:
    dlg = NewConnectionDialog()
    if not _select_protocol(dlg, "iec104.tcp"):
        pytest.skip("iec104.tcp driver not registered")
    assert dlg._unit_id_row_label.text() == "Common address:"  # type: ignore[attr-defined]
    assert dlg._tcp_port_spin.value() == 2404  # type: ignore[attr-defined]
    assert dlg._unit_id_spin.maximum() >= 65535  # type: ignore[attr-defined]


def test_iec104_request_yields_ca_address_format() -> None:
    dlg = NewConnectionDialog()
    if not _select_protocol(dlg, "iec104.tcp"):
        pytest.skip("iec104.tcp driver not registered")
    dlg._host_edit.setText("10.0.0.5")  # type: ignore[attr-defined]
    dlg._tcp_port_spin.setValue(2404)  # type: ignore[attr-defined]
    dlg._unit_id_spin.setValue(7)  # type: ignore[attr-defined]
    dlg._operator_edit.setText("alice@example.com")  # type: ignore[attr-defined]
    dlg._profile_radios[SessionProfile.LAB].setChecked(True)  # type: ignore[attr-defined]
    req = dlg.request()
    assert req.protocol_id == "iec104.tcp"
    assert req.address == "10.0.0.5:2404/ca=7"
    # And the IEC 104 driver's parser accepts what we built.
    host, port, ca, _oa = parse_address(req.address)
    assert (host, port, ca) == ("10.0.0.5", 2404, 7)


def test_modbus_then_iec104_then_modbus_resets_port_and_label() -> None:
    dlg = NewConnectionDialog()
    if not _select_protocol(dlg, "modbus.tcp"):
        pytest.skip("modbus.tcp driver not registered")
    assert dlg._tcp_port_spin.value() == 502  # type: ignore[attr-defined]
    assert dlg._unit_id_row_label.text() == "Unit ID:"  # type: ignore[attr-defined]
    if not _select_protocol(dlg, "iec104.tcp"):
        pytest.skip("iec104.tcp driver not registered")
    assert dlg._tcp_port_spin.value() == 2404  # type: ignore[attr-defined]
    if not _select_protocol(dlg, "modbus.tcp"):
        return
    assert dlg._tcp_port_spin.value() == 502  # type: ignore[attr-defined]
    assert dlg._unit_id_row_label.text() == "Unit ID:"  # type: ignore[attr-defined]
