# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for serial-port dropdown population in connection dialogs (P1.D.1)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QObject

from protoskipper.gui.dialogs._serial_ports import list_serial_ports

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# list_serial_ports helper
# ---------------------------------------------------------------------------


def test_list_serial_ports_returns_empty_when_pyserial_missing() -> None:
    """When pyserial is not importable, list_serial_ports must not raise."""
    with patch.dict(
        "sys.modules",
        {"serial": None, "serial.tools": None, "serial.tools.list_ports": None},
    ):
        result = list_serial_ports()
    assert result == []


def test_list_serial_ports_returns_device_and_description() -> None:
    """With a mock comports(), each port's device and desc are returned."""
    fake_port = MagicMock()
    fake_port.device = "/dev/ttyUSB0"
    fake_port.description = "FT232R USB UART"
    fake_port.manufacturer = "FTDI"

    with patch("protoskipper.gui.dialogs._serial_ports.list_serial_ports") as mock_fn:
        mock_fn.return_value = [("/dev/ttyUSB0", "FT232R USB UART [FTDI]")]
        result = mock_fn()

    assert len(result) == 1
    device, desc = result[0]
    assert device == "/dev/ttyUSB0"
    assert "FT232R" in desc


def test_list_serial_ports_includes_manufacturer_in_desc() -> None:
    """Manufacturer is appended in brackets when available."""
    import importlib
    from types import ModuleType

    # Build a minimal mock of serial.tools.list_ports.comports
    fake_port = MagicMock()
    fake_port.device = "/dev/ttyACM0"
    fake_port.description = "USB Serial"
    fake_port.manufacturer = "Arduino"

    mock_list_ports = MagicMock()
    mock_list_ports.comports.return_value = [fake_port]

    mock_serial_tools = ModuleType("serial.tools")
    mock_serial_tools.list_ports = mock_list_ports  # type: ignore[attr-defined]

    mock_serial = ModuleType("serial")
    mock_serial.tools = mock_serial_tools  # type: ignore[attr-defined]

    with patch.dict(
        "sys.modules",
        {
            "serial": mock_serial,
            "serial.tools": mock_serial_tools,
            "serial.tools.list_ports": mock_list_ports,
        },
    ):
        # Re-import to pick up the patched modules.
        import importlib

        import protoskipper.gui.dialogs._serial_ports as sp_mod

        importlib.reload(sp_mod)
        result = sp_mod.list_serial_ports()

    assert any("[Arduino]" in desc for _, desc in result)


# ---------------------------------------------------------------------------
# NewConnectionDialog serial-port combo visibility
# ---------------------------------------------------------------------------


@pytest.fixture()
def state(qapp: QObject) -> object:
    from protoskipper.gui.services.app_state import ApplicationState

    return ApplicationState()


def test_new_connection_port_combo_hidden_for_tcp(qtbot: object, state: object) -> None:
    """Serial-port combo is hidden when a TCP protocol is selected."""
    from protoskipper.gui.dialogs.new_connection import NewConnectionDialog

    dlg = NewConnectionDialog()
    qtbot.addWidget(dlg)  # type: ignore[union-attr]

    # Select modbus.tcp
    for i in range(dlg._protocol_combo.count()):
        if dlg._protocol_combo.itemData(i) == "modbus.tcp":
            dlg._protocol_combo.setCurrentIndex(i)
            break

    assert dlg._port_combo.isHidden()


def test_new_connection_port_combo_shown_for_rtu(qtbot: object) -> None:
    """Serial-port combo is shown when an RTU protocol is selected."""
    from protoskipper.gui.dialogs.new_connection import NewConnectionDialog

    dlg = NewConnectionDialog()
    qtbot.addWidget(dlg)  # type: ignore[union-attr]

    # Select modbus.rtu
    for i in range(dlg._protocol_combo.count()):
        if dlg._protocol_combo.itemData(i) == "modbus.rtu":
            dlg._protocol_combo.setCurrentIndex(i)
            break

    assert not dlg._port_combo.isHidden()


def test_new_connection_port_combo_empty_when_no_ports(qtbot: object) -> None:
    """When no serial ports are detected, the combo has only the blank entry."""
    from protoskipper.gui.dialogs.new_connection import NewConnectionDialog

    with patch("protoskipper.gui.dialogs.new_connection.list_serial_ports", return_value=[]):
        dlg = NewConnectionDialog()
        qtbot.addWidget(dlg)  # type: ignore[union-attr]

    # Only the blank placeholder item.
    assert dlg._port_combo.count() == 1


def test_new_connection_port_combo_lists_detected_ports(qtbot: object) -> None:
    """Detected ports are listed in the combo with vendor annotation."""
    from protoskipper.gui.dialogs.new_connection import NewConnectionDialog

    fake_ports = [
        ("/dev/ttyUSB0", "FT232R USB UART [FTDI]"),
        ("/dev/ttyUSB1", "USB Serial [Arduino LLC]"),
    ]
    with patch(
        "protoskipper.gui.dialogs.new_connection.list_serial_ports", return_value=fake_ports
    ):
        dlg = NewConnectionDialog()
        qtbot.addWidget(dlg)  # type: ignore[union-attr]

    # Blank + 2 real entries.
    assert dlg._port_combo.count() == 3
    assert "/dev/ttyUSB0" in dlg._port_combo.itemText(1)
    assert "/dev/ttyUSB1" in dlg._port_combo.itemText(2)
