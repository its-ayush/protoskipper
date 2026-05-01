# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""End-to-end probing tests against the bundled Modbus simulator.

These run against a real socket-bound pymodbus server, so they exercise
the same code paths a user hits in production. They are fast (sub-second
per test) but not free; mark them ``integration`` so they are skipped in
the default unit-only invocation.
"""

from __future__ import annotations

import pytest

from protoskipper.builtin_drivers.modbus.driver import ModbusTcpDriver

pytestmark = pytest.mark.integration


def test_probe_single_host_finds_simulator(modbus_simulator) -> None:
    host, port = modbus_simulator
    drv = ModbusTcpDriver(probe_workers=4, probe_timeout_s=1.0)
    devices = list(drv.discover(f"{host}:{port}"))
    assert len(devices) == 1
    assert devices[0].address == f"{host}:{port}/unit=1"
    assert devices[0].metadata.get("unit_id") == 1


def test_probe_unit_range_finds_each_unit(modbus_simulator) -> None:
    """The simulator runs in single=True mode, so it accepts any unit-id;
    a probing sweep with three units should yield three DeviceRefs."""
    host, port = modbus_simulator
    drv = ModbusTcpDriver(probe_workers=4, probe_timeout_s=1.0)
    devices = list(drv.discover(f"{host}:{port}/units=1-3"))
    addresses = {d.address for d in devices}
    assert addresses == {
        f"{host}:{port}/unit=1",
        f"{host}:{port}/unit=2",
        f"{host}:{port}/unit=3",
    }


def test_probe_cidr_with_simulator_in_range(modbus_simulator) -> None:
    """A /30 CIDR scan that includes the simulator IP must find it.

    /30 = 4 addresses, 2 usable hosts (.1 and .2). Since the simulator
    listens on 127.0.0.1 we use 127.0.0.0/30 which yields .1 and .2 as
    probe targets - one of which (127.0.0.1) is the simulator.
    """
    host, port = modbus_simulator
    assert host == "127.0.0.1"
    drv = ModbusTcpDriver(probe_workers=4, probe_timeout_s=1.0)
    devices = list(drv.discover(f"127.0.0.0/30:{port}"))
    addresses = [d.address for d in devices]
    assert any(f"127.0.0.1:{port}/unit=1" == a for a in addresses), addresses


def test_probe_finds_nothing_on_dead_host() -> None:
    """A host that doesn't answer should yield zero devices, not an error."""
    drv = ModbusTcpDriver(probe_workers=2, probe_timeout_s=0.3)
    # Use a port we know won't be open. 1 is reserved (TCP port multiplexer)
    # and we expect the connect attempt to fail fast.
    devices = list(drv.discover("127.0.0.1:1/unit=1"))
    assert devices == []


def test_probe_concurrent_workers_dont_interfere(modbus_simulator) -> None:
    """The thread pool must not produce duplicate or interleaved garbage."""
    host, port = modbus_simulator
    drv = ModbusTcpDriver(probe_workers=8, probe_timeout_s=0.5)
    devices = list(
        drv.discover(
            # 4 hosts (only one is the simulator) x 5 units
            f"127.0.0.0/30,{host}:{port}/units=1-5"
        )
    )
    # We expect 5 hits (one per unit on the simulator); other hosts in the
    # /30 don't run a Modbus server.
    assert len(devices) == 5
    units = sorted(d.metadata["unit_id"] for d in devices)
    assert units == [1, 2, 3, 4, 5]
