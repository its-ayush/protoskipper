# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit-level checks on the Modbus driver: contract conformance and parsing.

End-to-end Modbus tests against a simulator live under ``tests/integration/``
once the simulator harness is wired up. These tests intentionally avoid
opening sockets so they run fast and offline.
"""
from __future__ import annotations

import pytest

from protoskipper.builtin_drivers.modbus.driver import (
    DEFAULT_TCP_PORT,
    DEFAULT_UNIT,
    ModbusRtuDriver,
    ModbusTcpDriver,
    _parse_object_id,
    _parse_tcp_address,
)
from protoskipper.core.driver import ProtocolDriver
from protoskipper.core.errors import EncodingError, UnsupportedOperation


def test_modbus_tcp_driver_conforms_to_abc() -> None:
    drv = ModbusTcpDriver()
    assert isinstance(drv, ProtocolDriver)
    assert drv.PROTOCOL_ID == "modbus.tcp"
    assert drv.DISPLAY_NAME


def test_modbus_rtu_driver_conforms_to_abc() -> None:
    drv = ModbusRtuDriver()
    assert isinstance(drv, ProtocolDriver)
    assert drv.PROTOCOL_ID == "modbus.rtu"


def test_modbus_rtu_connect_is_unsupported_for_now() -> None:
    drv = ModbusRtuDriver()
    with pytest.raises(UnsupportedOperation):
        drv.connect(drv.parse_address("/dev/ttyUSB0/unit=1"), safety=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "address,expected",
    [
        ("10.0.0.5", ("10.0.0.5", DEFAULT_TCP_PORT, DEFAULT_UNIT)),
        ("10.0.0.5:1502", ("10.0.0.5", 1502, DEFAULT_UNIT)),
        ("10.0.0.5/unit=7", ("10.0.0.5", DEFAULT_TCP_PORT, 7)),
        ("10.0.0.5:1502/unit=12", ("10.0.0.5", 1502, 12)),
    ],
)
def test_parse_tcp_address(address: str, expected: tuple[str, int, int]) -> None:
    assert _parse_tcp_address(address) == expected


def test_parse_tcp_address_rejects_garbage() -> None:
    with pytest.raises(EncodingError):
        _parse_tcp_address("not an address")


@pytest.mark.parametrize(
    "object_id,expected",
    [
        ("holding:0", ("holding", 0, 1)),
        ("holding:40000", ("holding", 40000, 1)),
        ("holding:0:2", ("holding", 0, 2)),
        ("input:5", ("input", 5, 1)),
        ("coils:10", ("coils", 10, 1)),
        ("discrete:3", ("discrete", 3, 1)),
        ("HOLDING:0", ("holding", 0, 1)),  # case-insensitive
    ],
)
def test_parse_object_id(object_id: str, expected: tuple[str, int, int]) -> None:
    assert _parse_object_id(object_id) == expected


@pytest.mark.parametrize(
    "object_id",
    [
        "",
        "holding",
        "holding:abc",
        "register:0",  # unknown table
        "holding:0:1:1",  # too many parts
    ],
)
def test_parse_object_id_rejects_bad_input(object_id: str) -> None:
    with pytest.raises(EncodingError):
        _parse_object_id(object_id)
