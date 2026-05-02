# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Integration test for Modbus RTU using a virtual serial-port pair (socat).

Skip conditions:
* Not running on Linux.
* ``socat`` is not on PATH.
* pyserial is not installed (``pymodbus[serial]`` missing).

The test creates a virtual serial-port pair with socat, runs the Modbus TCP
simulator from the test suite, and bridges it to the RTU slave end of the
pair using pymodbus's ``StartSerialServer``. The RTU driver connects to the
master end and performs the same write→read→deny→audit sequence as the TCP
integration test.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Platform / dependency guards
# ---------------------------------------------------------------------------


def _socat_available() -> bool:
    return shutil.which("socat") is not None


def _pyserial_available() -> bool:
    try:
        import serial  # noqa: F401

        return True
    except ImportError:
        return False


skip_not_linux = pytest.mark.skipif(sys.platform != "linux", reason="Linux only (socat)")
skip_no_socat = pytest.mark.skipif(not _socat_available(), reason="socat not on PATH")
skip_no_serial = pytest.mark.skipif(not _pyserial_available(), reason="pyserial not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def virtual_serial_pair(tmp_path: Path):
    """Create a socat virtual serial-port pair.

    Yields ``(master_port, slave_port)`` as ``/dev/pts/N`` style paths.
    The socat process is terminated in teardown.
    """
    master = str(tmp_path / "master")
    slave = str(tmp_path / "slave")

    proc = subprocess.Popen(
        [
            "socat",
            f"PTY,link={master},raw,echo=0",
            f"PTY,link={slave},raw,echo=0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Give socat time to create the symlinks.
    for _ in range(20):
        if Path(master).exists() and Path(slave).exists():
            break
        time.sleep(0.1)
    else:
        proc.terminate()
        pytest.skip("socat did not create PTY links in time")

    yield master, slave

    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture()
def rtu_simulator(virtual_serial_pair):
    """Spin up a pymodbus RTU server on the slave end of the PTY pair."""
    slave_port, _ = virtual_serial_pair  # note: slave drives the server side
    _master_port, slave_port = virtual_serial_pair

    try:
        from pymodbus.datastore import (
            ModbusSequentialDataBlock,
            ModbusServerContext,
            ModbusSlaveContext,
        )
        from pymodbus.server import StartSerialServer
    except ImportError:
        pytest.skip("pymodbus[serial] not installed")

    import threading

    store = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(0, [0] * 100),
    )
    context = ModbusServerContext(slaves=store, single=True)

    server_thread = threading.Thread(
        target=StartSerialServer,
        kwargs={
            "context": context,
            "port": slave_port,
            "baudrate": 9600,
            "stopbits": 1,
            "bytesize": 8,
            "parity": "N",
            "timeout": 1,
            "framer": "rtu",
        },
        daemon=True,
    )
    server_thread.start()
    time.sleep(0.5)  # allow server to start listening

    # Return (master_port, 1) so tests address unit_id=1 on the master end.
    master_port, _ = virtual_serial_pair
    yield master_port, 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@skip_not_linux
@skip_no_socat
@skip_no_serial
def test_rtu_session_write_read_audit(virtual_serial_pair, rtu_simulator) -> None:
    """Write a value, read it back, and verify the audit log."""
    master_port, unit_id = rtu_simulator

    from protoskipper.builtin_drivers.modbus.driver import ModbusRtuDriver
    from protoskipper.core.audit import verify_log
    from protoskipper.core.driver import Access, DeviceRef, ObjectRef, SessionProfile
    from protoskipper.core.session import open_session

    drv = ModbusRtuDriver()
    device = DeviceRef(
        protocol="modbus.rtu",
        address=f"{master_port}@9600,N,1/unit={unit_id}",
        label="rtu-sim",
    )

    ref = ObjectRef(
        device=device,
        object_id="holding:10",
        data_type="uint16",
        access=Access.READ_WRITE,
        label="test_register",
    )

    with tempfile.TemporaryDirectory() as td:
        audit_dir = Path(td)
        with open_session(
            drv,
            device,
            profile=SessionProfile.LAB,
            operator="ci@datasailors.io",
            audit_dir=audit_dir,
            confirm=lambda intent, profile: True,
        ) as session:
            intent = session.driver_session.prepare_write(ref, 1234)
            session.driver_session.commit_write(intent)

            result = session.driver_session.read(ref)
            assert result.value == 1234, f"Expected 1234, got {result.value}"

        # Audit chain must be intact.
        ok, errors = verify_log(audit_dir)
        assert ok, f"Audit log verification failed: {errors}"


@skip_not_linux
@skip_no_socat
@skip_no_serial
def test_rtu_write_denied_in_production(virtual_serial_pair, rtu_simulator) -> None:
    """In PRODUCTION profile, refusing the confirm dialog must deny the write."""
    master_port, unit_id = rtu_simulator

    from protoskipper.builtin_drivers.modbus.driver import ModbusRtuDriver
    from protoskipper.core.driver import Access, DeviceRef, ObjectRef, SessionProfile
    from protoskipper.core.errors import AuthorizationDenied
    from protoskipper.core.session import open_session

    drv = ModbusRtuDriver()
    device = DeviceRef(
        protocol="modbus.rtu",
        address=f"{master_port}@9600,N,1/unit={unit_id}",
        label="rtu-sim-prod",
    )

    ref = ObjectRef(
        device=device,
        object_id="holding:10",
        data_type="uint16",
        access=Access.READ_WRITE,
        label="test_register",
    )

    import tempfile

    with (
        tempfile.TemporaryDirectory() as td,
        open_session(
            drv,
            device,
            profile=SessionProfile.PRODUCTION,
            operator="ci@datasailors.io",
            audit_dir=Path(td),
            confirm=lambda intent, profile: False,  # always deny
        ) as session,
    ):
        intent = session.driver_session.prepare_write(ref, 9999)
        with pytest.raises(AuthorizationDenied):
            session.driver_session.commit_write(intent)
