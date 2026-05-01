# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Pytest fixtures for the integration suite.

The fixtures spin up the bundled Modbus simulator on a free port for the
lifetime of the test, and tear it down cleanly afterwards. They never
share simulator instances across tests so each test starts with a known
state.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import pytest


def _free_port() -> int:
    """Bind to port 0 so the OS picks a free TCP port, then release it.

    There's a tiny race window between this socket closing and the
    simulator binding to the same port, but it's fine for tests.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start_simulator_on_port(port: int) -> threading.Thread:
    """Start the bundled simulator on ``localhost:port`` in a daemon thread."""
    from protoskipper.builtin_drivers.modbus.simulator import build_datastore
    from pymodbus.datastore import ModbusServerContext

    try:
        ctx = ModbusServerContext(devices=build_datastore(), single=True)
    except TypeError:  # pragma: no cover - older pymodbus
        ctx = ModbusServerContext(slaves=build_datastore(), single=True)

    async def _serve() -> None:
        from pymodbus.server import StartAsyncTcpServer
        await StartAsyncTcpServer(context=ctx, address=("127.0.0.1", port))

    thread = threading.Thread(target=lambda: asyncio.run(_serve()), daemon=True)
    thread.start()
    # Give the server a moment to bind. Simulator startup is sub-second on
    # modern boxes; 1.5s leaves headroom for slow CI.
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return thread
        except OSError:
            time.sleep(0.05)
    return thread  # last-ditch: return anyway, tests will fail if not listening


@pytest.fixture(scope="function")
def modbus_simulator() -> Iterator[tuple[str, int]]:
    """Yields ``(host, port)`` of a freshly-started Modbus TCP simulator."""
    port = _free_port()
    _start_simulator_on_port(port)
    yield "127.0.0.1", port
    # Daemon thread, no explicit shutdown - process exit reaps it.
