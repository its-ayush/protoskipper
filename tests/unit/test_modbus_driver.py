# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit-level checks on the Modbus driver: contract conformance + parsers.

End-to-end Modbus tests against a simulator live in ``tests/integration/``;
these tests run fast and offline.
"""

from __future__ import annotations

import pytest

from protoskipper.builtin_drivers.modbus.driver import (
    DEFAULT_TCP_PORT,
    DEFAULT_UNIT,
    ModbusRtuDriver,
    ModbusTcpDriver,
    RtuConfig,
    _parse_object_id,
    _parse_tcp_address,
    parse_probe_target,
    parse_rtu_address,
)
from protoskipper.core.driver import ProtocolDriver
from protoskipper.core.errors import ConnectionFailure, EncodingError

# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------


def test_modbus_tcp_driver_conforms_to_abc() -> None:
    drv = ModbusTcpDriver()
    assert isinstance(drv, ProtocolDriver)
    assert drv.PROTOCOL_ID == "modbus.tcp"
    assert drv.DISPLAY_NAME


def test_modbus_rtu_driver_conforms_to_abc() -> None:
    drv = ModbusRtuDriver()
    assert isinstance(drv, ProtocolDriver)
    assert drv.PROTOCOL_ID == "modbus.rtu"


def test_modbus_rtu_connect_fails_cleanly_on_missing_port() -> None:
    """Connecting to a serial port that does not exist must raise
    :class:`ConnectionFailure`, not a bare pyserial error or a generic
    Exception. This is what the GUI catches to show 'could not open port'."""
    drv = ModbusRtuDriver()
    device = drv.parse_address("/dev/this-port-does-not-exist@9600,N,1/unit=1")
    with pytest.raises(ConnectionFailure):
        drv.connect(device, safety=None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Single-host TCP address parsing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Probe-target parsing (CIDR, comma list, units range)
# ---------------------------------------------------------------------------


def test_probe_target_single_host() -> None:
    pt = parse_probe_target("10.0.0.5")
    assert pt.hosts == (("10.0.0.5", DEFAULT_TCP_PORT),)
    assert pt.units == (DEFAULT_UNIT,)


def test_probe_target_single_host_explicit_port_and_unit() -> None:
    pt = parse_probe_target("10.0.0.5:1502/unit=7")
    assert pt.hosts == (("10.0.0.5", 1502),)
    assert pt.units == (7,)


def test_probe_target_units_range() -> None:
    pt = parse_probe_target("10.0.0.5/units=1-5")
    assert pt.hosts == (("10.0.0.5", DEFAULT_TCP_PORT),)
    assert pt.units == (1, 2, 3, 4, 5)


def test_probe_target_units_single() -> None:
    pt = parse_probe_target("10.0.0.5/units=3")
    assert pt.units == (3,)


def test_probe_target_cidr() -> None:
    pt = parse_probe_target("10.0.0.0/30")
    # /30 = 4 addresses, 2 usable hosts (.1 and .2) for IPv4
    assert len(pt.hosts) == 2
    assert all(h.startswith("10.0.0.") for h, _p in pt.hosts)


def test_probe_target_cidr_with_port_and_units() -> None:
    pt = parse_probe_target("192.168.1.0/30:1502/units=1-3")
    assert all(p == 1502 for _h, p in pt.hosts)
    assert pt.units == (1, 2, 3)


def test_probe_target_comma_list() -> None:
    pt = parse_probe_target("10.0.0.5,10.0.0.7,10.0.0.9")
    hosts = [h for h, _p in pt.hosts]
    assert hosts == ["10.0.0.5", "10.0.0.7", "10.0.0.9"]


def test_probe_target_refuses_huge_cidr() -> None:
    with pytest.raises(EncodingError, match="Refusing to probe"):
        parse_probe_target("10.0.0.0/8")


def test_probe_target_rejects_bad_units() -> None:
    with pytest.raises(EncodingError):
        parse_probe_target("10.0.0.5/units=200-10")  # lo > hi
    with pytest.raises(EncodingError):
        parse_probe_target("10.0.0.5/units=0-10")  # below 1


def test_probe_target_rejects_empty() -> None:
    with pytest.raises(EncodingError):
        parse_probe_target("")


# ---------------------------------------------------------------------------
# RTU address parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address,expected",
    [
        ("/dev/ttyUSB0", RtuConfig(port="/dev/ttyUSB0")),
        ("COM3", RtuConfig(port="COM3")),
        ("/dev/ttyUSB0@19200", RtuConfig(port="/dev/ttyUSB0", baudrate=19200)),
        ("/dev/ttyUSB0@9600,E", RtuConfig(port="/dev/ttyUSB0", baudrate=9600, parity="E")),
        (
            "/dev/ttyUSB0@9600,N,1/unit=3",
            RtuConfig(port="/dev/ttyUSB0", baudrate=9600, parity="N", stopbits=1, unit=3),
        ),
        (
            "/dev/ttyUSB0@9600,N,2/unit=15",
            RtuConfig(port="/dev/ttyUSB0", baudrate=9600, parity="N", stopbits=2, unit=15),
        ),
    ],
)
def test_parse_rtu_address(address: str, expected: RtuConfig) -> None:
    got = parse_rtu_address(address)
    assert got.port == expected.port
    assert got.baudrate == expected.baudrate
    assert got.parity == expected.parity
    assert got.stopbits == expected.stopbits
    assert got.unit == expected.unit


def test_parse_rtu_address_units_range() -> None:
    cfg = parse_rtu_address("/dev/ttyUSB0@9600,N,1/units=1-10")
    assert cfg.units_range == tuple(range(1, 11))
    assert cfg.unit == 1  # first of the range, used by connect() if called


def test_parse_rtu_address_rejects_bad_input() -> None:
    with pytest.raises(EncodingError):
        parse_rtu_address("")
    with pytest.raises(EncodingError):
        parse_rtu_address("/dev/ttyUSB0@bad-baud")
    with pytest.raises(EncodingError):
        parse_rtu_address("/dev/ttyUSB0@9600,N,3")  # stopbits not in {1, 2}


# ---------------------------------------------------------------------------
# object_id parsing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# read_many contiguous-range optimisation (P1.C.1)
# ---------------------------------------------------------------------------


class _FakeRegistersResponse:
    """Minimal pymodbus response stub for register reads."""

    def __init__(self, registers: list[int]) -> None:
        self.registers = registers

    def isError(self) -> bool:
        return False


class _FakeErrorResponse:
    def isError(self) -> bool:
        return True

    def __str__(self) -> str:
        return "simulated error"


def _make_session(client: object) -> object:
    """Construct a _ModbusSession with a mock client (no real socket)."""
    from unittest.mock import MagicMock

    from protoskipper.builtin_drivers.modbus.driver import _ModbusSession
    from protoskipper.core.driver import DeviceRef

    device = DeviceRef(
        protocol="modbus.tcp",
        address="127.0.0.1:502",
        label="test",
        metadata={},
    )
    return _ModbusSession(
        client=client,  # type: ignore[arg-type]
        unit=1,
        device=device,
        safety=MagicMock(),
    )


def _make_ref(object_id: str, dtype: str = "uint16") -> object:
    from protoskipper.core.driver import Access, DeviceRef, ObjectRef

    device = DeviceRef(
        protocol="modbus.tcp",
        address="127.0.0.1:502",
        label="test",
        metadata={},
    )
    return ObjectRef(
        device=device,
        object_id=object_id,
        data_type=dtype,
        access=Access.READ_ONLY,
        unit=None,
        label=object_id,
    )


def test_read_many_contiguous_holding_issues_one_request() -> None:
    """10 uint16 refs at holding:0..holding:9 → exactly 1 pymodbus call."""
    from unittest.mock import MagicMock

    client = MagicMock()
    client.read_holding_registers.return_value = _FakeRegistersResponse(list(range(10)))

    session = _make_session(client)
    refs = [_make_ref(f"holding:{i}") for i in range(10)]
    results = session.read_many(refs)

    assert client.read_holding_registers.call_count == 1
    call_kwargs = client.read_holding_registers.call_args
    assert call_kwargs.kwargs["address"] == 0
    assert call_kwargs.kwargs["count"] == 10

    assert len(results) == 10
    assert [r.value for r in results] == list(range(10))


def test_read_many_two_disjoint_blocks_issues_two_requests() -> None:
    """holding:0-2 and holding:10-12 are disjoint → 2 wire requests."""
    from unittest.mock import MagicMock

    client = MagicMock()
    client.read_holding_registers.side_effect = [
        _FakeRegistersResponse([1, 2, 3]),
        _FakeRegistersResponse([10, 11, 12]),
    ]

    session = _make_session(client)
    refs = [
        _make_ref("holding:0"),
        _make_ref("holding:1"),
        _make_ref("holding:2"),
        _make_ref("holding:10"),
        _make_ref("holding:11"),
        _make_ref("holding:12"),
    ]
    results = session.read_many(refs)

    assert client.read_holding_registers.call_count == 2
    assert len(results) == 6
    assert [r.value for r in results] == [1, 2, 3, 10, 11, 12]


def test_read_many_preserves_input_order() -> None:
    """Results must match the input ref order even when refs are unsorted."""
    from unittest.mock import MagicMock

    client = MagicMock()
    # Refs are given in reverse address order; batch should still coalesce.
    client.read_holding_registers.return_value = _FakeRegistersResponse([7, 8, 9])

    session = _make_session(client)
    # refs in reverse order: holding:2, holding:1, holding:0
    refs = [_make_ref(f"holding:{i}") for i in [2, 1, 0]]
    results = session.read_many(refs)

    assert client.read_holding_registers.call_count == 1
    # Values should be 9, 8, 7 (address 2→9, address 1→8, address 0→7)
    assert [r.value for r in results] == [9, 8, 7]


def test_read_many_bulk_error_propagates_to_all_in_span() -> None:
    """If the bulk request fails, all refs in that span get Quality.BAD."""
    from unittest.mock import MagicMock

    from protoskipper.core.driver import Quality

    client = MagicMock()
    client.read_holding_registers.return_value = _FakeErrorResponse()

    session = _make_session(client)
    refs = [_make_ref("holding:0"), _make_ref("holding:1")]
    results = session.read_many(refs)

    assert all(r.quality == Quality.BAD for r in results)
    assert client.read_holding_registers.call_count == 1
