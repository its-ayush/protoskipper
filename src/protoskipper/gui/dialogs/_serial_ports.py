# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Utilities shared by GUI dialogs that need serial-port information."""

from __future__ import annotations


def list_serial_ports() -> list[tuple[str, str]]:
    """Return detected serial ports as (device_path, description) pairs.

    Wraps ``pyserial``'s ``list_ports.comports()``.  If pyserial is not
    installed, returns an empty list rather than raising an error.

    The description includes vendor + PID when available, e.g.::

        /dev/ttyUSB0  FT232R USB UART [Future Technology Devices International]
    """
    try:
        from serial.tools import list_ports  # type: ignore[import-untyped]

        ports = []
        for port in list_ports.comports():
            parts = [port.description or port.device]
            if port.manufacturer:
                parts.append(f"[{port.manufacturer}]")
            ports.append((port.device, " ".join(parts)))
        return ports
    except ImportError:
        return []
