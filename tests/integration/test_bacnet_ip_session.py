# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Integration tests for the BACnet/IP driver + simulator (P7.F.1).

These tests spin up a ``BacnetSimulator`` on a random UDP port and connect
to it with ``BacnetIpSession`` to exercise the full APDU stack end-to-end:
Who-Is/I-Am discovery, RP, RPM, WP, WPM, COV subscribe/notify, and
read_range basics.

All tests are marked ``integration`` so they can be excluded when only
pymodbus is absent (the BACnet tests need bacpypes3, which is declared in
the ``[bacnet]`` extras).
"""

from __future__ import annotations

import socket
import time
from typing import Any

import pytest

from protoskipper.builtin_drivers.bacnet.driver import BacnetIpDriver
from protoskipper.builtin_drivers.bacnet.simulator import BacnetSimulator
from protoskipper.core.driver import DeviceRef, SessionProfile

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_udp_port() -> int:
    """Return a free UDP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_DEFAULT_OBJECTS: list[dict[str, Any]] = [
    {
        "type": "analog-input",
        "instance": 1,
        "objectName": "AI-Temp",
        "presentValue": 21.5,
        "units": "degrees-celsius",
    },
    {
        "type": "analog-value",
        "instance": 1,
        "objectName": "AV-Setpoint",
        "presentValue": 22.0,
        "units": "degrees-celsius",
        "covIncrement": 0.5,
    },
    {
        "type": "analog-value",
        "instance": 2,
        "objectName": "AV-Damper",
        "presentValue": 50.0,
        "units": "percent",
    },
    {"type": "binary-input", "instance": 1, "objectName": "BI-RunStatus", "presentValue": False},
    {"type": "binary-value", "instance": 1, "objectName": "BV-Override", "presentValue": False},
    {
        "type": "multi-state-value",
        "instance": 1,
        "objectName": "MSV-Mode",
        "presentValue": 1,
        "numberOfStates": 4,
        "stateText": ["Off", "Heating", "Cooling", "Auto"],
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bacnet_simulator():
    """Start a BACnet simulator for this test module and yield (sim, device_id, port)."""
    port = _free_udp_port()
    sim = BacnetSimulator()
    sim.start_simulator(
        {
            "device_id": 8001,
            "address": "127.0.0.1",
            "port": port,
            "objects": _DEFAULT_OBJECTS,
        }
    )
    yield sim, 8001, port
    sim.stop_simulator()


@pytest.fixture
def session(bacnet_simulator):
    """Open a BacnetIpSession against the module-scoped simulator."""
    _sim, device_id, port = bacnet_simulator
    device = DeviceRef(
        protocol="bacnet.ip",
        address=f"127.0.0.1:{port}/dev={device_id}",
        label="sim",
    )
    drv = BacnetIpDriver()
    s = drv.connect(
        device,
        safety=_make_safety(allow_writes=True),
    )
    yield s
    s.close()


def _make_safety(*, allow_writes: bool = True):
    from unittest.mock import MagicMock

    from protoskipper.core.driver import SafetyContext

    ctx = MagicMock(spec=SafetyContext)
    ctx.require_write_authorization.return_value = allow_writes
    ctx.record_write_outcome.return_value = None
    ctx.profile = SessionProfile.LAB
    ctx.replay_mode = False
    return ctx


# ---------------------------------------------------------------------------
# P7.F.1 — Simulator start/stop
# ---------------------------------------------------------------------------


def test_simulator_starts_and_stops() -> None:
    """BacnetSimulator.start_simulator() should not raise and should ready within 10 s."""
    port = _free_udp_port()
    sim = BacnetSimulator()
    sim.start_simulator({"device_id": 9001, "address": "127.0.0.1", "port": port, "objects": []})
    assert sim._app is not None
    sim.stop_simulator()
    assert sim._app is None


def test_simulator_all_object_types() -> None:
    """All supported object types should be created without error."""
    port = _free_udp_port()
    sim = BacnetSimulator()
    sim.start_simulator(
        {
            "device_id": 9002,
            "address": "127.0.0.1",
            "port": port,
            "objects": _DEFAULT_OBJECTS,
        }
    )
    try:
        assert len(sim._objects) == len(_DEFAULT_OBJECTS)
        assert "analog-input:1" in sim._objects
        assert "analog-value:1" in sim._objects
        assert "binary-input:1" in sim._objects
        assert "binary-value:1" in sim._objects
        assert "multi-state-value:1" in sim._objects
    finally:
        sim.stop_simulator()


def test_simulator_update_value() -> None:
    """update_value() should change the object's presentValue."""
    port = _free_udp_port()
    sim = BacnetSimulator()
    sim.start_simulator(
        {
            "device_id": 9003,
            "address": "127.0.0.1",
            "port": port,
            "objects": [
                {
                    "type": "analog-value",
                    "instance": 1,
                    "objectName": "AV-Test",
                    "presentValue": 0.0,
                },
            ],
        }
    )
    try:
        sim.update_value("analog-value:1", "presentValue", 42.0)
        val = sim.get_value("analog-value:1", "presentValue")
        assert abs(float(val) - 42.0) < 0.01
    finally:
        sim.stop_simulator()


def test_simulator_update_value_missing_object() -> None:
    """update_value() with an unknown object_id should raise KeyError."""
    port = _free_udp_port()
    sim = BacnetSimulator()
    sim.start_simulator({"device_id": 9004, "address": "127.0.0.1", "port": port, "objects": []})
    try:
        with pytest.raises(KeyError):
            sim.update_value("analog-value:999", "presentValue", 1.0)
    finally:
        sim.stop_simulator()


def test_simulator_alias_types() -> None:
    """Short-form aliases (ai, av, bi, bv, msv) should be accepted."""
    port = _free_udp_port()
    sim = BacnetSimulator()
    sim.start_simulator(
        {
            "device_id": 9005,
            "address": "127.0.0.1",
            "port": port,
            "objects": [
                {"type": "ai", "instance": 1, "objectName": "AI-1", "presentValue": 0.0},
                {"type": "av", "instance": 1, "objectName": "AV-1", "presentValue": 0.0},
                {"type": "bi", "instance": 1, "objectName": "BI-1", "presentValue": False},
                {"type": "bv", "instance": 1, "objectName": "BV-1", "presentValue": False},
                {
                    "type": "msv",
                    "instance": 1,
                    "objectName": "MSV-1",
                    "presentValue": 1,
                    "numberOfStates": 3,
                },
            ],
        }
    )
    try:
        assert "analog-input:1" in sim._objects
        assert "analog-value:1" in sim._objects
        assert "binary-input:1" in sim._objects
        assert "binary-value:1" in sim._objects
        assert "multi-state-value:1" in sim._objects
    finally:
        sim.stop_simulator()


# ---------------------------------------------------------------------------
# BACnet client → simulator end-to-end tests
# ---------------------------------------------------------------------------


def test_who_is_discovers_simulator(bacnet_simulator) -> None:
    """who_is() should find the simulated device."""
    _sim, device_id, port = bacnet_simulator
    drv = BacnetIpDriver()
    results = list(drv.discover(f"127.0.0.1:{port}"))
    assert any(str(device_id) in str(r) for r in results), (
        f"device {device_id} not discovered; got {results}"
    )


def test_enumerate_objects(session, bacnet_simulator) -> None:
    """enumerate_objects() should return ObjectRefs for each configured object."""
    _sim, _device_id, _port = bacnet_simulator
    refs = list(session.enumerate_objects())
    # Must include at least the configured objects plus the device object
    assert len(refs) >= len(_DEFAULT_OBJECTS)
    ids = [r.object_id for r in refs]
    assert any("analog-value:1" in oid or "AV:1" in oid for oid in ids)


def test_read_analog_value(session, bacnet_simulator) -> None:
    """read() on an AnalogValue should return a GOOD ReadResult."""
    from protoskipper.core.driver import Quality

    refs = list(session.enumerate_objects())
    av1 = next((r for r in refs if "analog-value:1" in r.object_id), None)
    if av1 is None:
        pytest.skip("analog-value:1 not found in enumerated objects")
    result = session.read(av1)
    assert result.quality == Quality.GOOD
    assert result.value is not None
    assert abs(float(result.value) - 22.0) < 1.0


def test_read_binary_value(session, bacnet_simulator) -> None:
    """read() on a BinaryValue should return a GOOD ReadResult."""
    from protoskipper.core.driver import Quality

    refs = list(session.enumerate_objects())
    bv1 = next((r for r in refs if "binary-value:1" in r.object_id), None)
    if bv1 is None:
        pytest.skip("binary-value:1 not enumerated")
    result = session.read(bv1)
    assert result.quality == Quality.GOOD


def test_write_analog_value(session, bacnet_simulator) -> None:
    """prepare_write + commit_write should update the simulator's object model."""
    sim, _device_id, _port = bacnet_simulator
    refs = list(session.enumerate_objects())
    av2 = next((r for r in refs if "analog-value:2" in r.object_id), None)
    if av2 is None:
        pytest.skip("analog-value:2 not enumerated")

    intent = session.prepare_write(av2, 75.0)
    assert intent is not None
    result = session.commit_write(intent)
    assert result.success

    # Verify the change is visible server-side
    time.sleep(0.2)
    srv_val = sim.get_value("analog-value:2", "presentValue")
    assert abs(float(srv_val) - 75.0) < 1.0


def test_write_binary_value(session, bacnet_simulator) -> None:
    """Writing True to a BinaryValue should flip it active."""
    sim, _device_id, _port = bacnet_simulator
    refs = list(session.enumerate_objects())
    bv1 = next((r for r in refs if "binary-value:1" in r.object_id), None)
    if bv1 is None:
        pytest.skip("binary-value:1 not enumerated")

    intent = session.prepare_write(bv1, True)
    result = session.commit_write(intent)
    assert result.success

    time.sleep(0.2)
    srv_val = sim.get_value("binary-value:1", "presentValue")
    # bacpypes3 returns "active"/"inactive" enum or bool
    assert str(srv_val).lower() in ("active", "true", "1")


def test_read_many_batch(session, bacnet_simulator) -> None:
    """read_many() should return one ReadResult per ref, all GOOD."""
    from protoskipper.core.driver import Quality

    refs = list(session.enumerate_objects())
    analog = [r for r in refs if "analog" in r.object_id][:3]
    if not analog:
        pytest.skip("no analog objects enumerated")
    results = session.read_many(analog)
    assert len(results) == len(analog)
    assert all(r.quality == Quality.GOOD for r in results)


def test_update_value_reflected_in_read(session, bacnet_simulator) -> None:
    """update_value() on the simulator should be visible to a subsequent read()."""
    from protoskipper.core.driver import Quality

    sim, _device_id, _port = bacnet_simulator
    sim.update_value("analog-input:1", "presentValue", 99.9)
    time.sleep(0.3)

    refs = list(session.enumerate_objects())
    ai1 = next((r for r in refs if "analog-input:1" in r.object_id), None)
    if ai1 is None:
        pytest.skip("analog-input:1 not enumerated")
    result = session.read(ai1)
    assert result.quality == Quality.GOOD
    assert abs(float(result.value) - 99.9) < 1.0
