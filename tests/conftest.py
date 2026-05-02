# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Root-level conftest.py — fixtures shared across all test suites.

Fixtures that live here are visible to tests/unit/, tests/integration/,
and tests/gui/ alike.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import pytest


def _free_port() -> int:
    """Bind to port 0 so the OS picks a free TCP port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start_simulator_on_port(port: int) -> threading.Thread:
    """Start the bundled Modbus simulator on ``localhost:port`` in a daemon thread."""
    from pymodbus.datastore import ModbusServerContext

    from protoskipper.builtin_drivers.modbus.simulator import build_datastore

    try:
        ctx = ModbusServerContext(devices=build_datastore(), single=True)
    except TypeError:  # pragma: no cover - older pymodbus
        ctx = ModbusServerContext(slaves=build_datastore(), single=True)

    async def _serve() -> None:
        from pymodbus.server import StartAsyncTcpServer

        await StartAsyncTcpServer(context=ctx, address=("127.0.0.1", port))

    thread = threading.Thread(target=lambda: asyncio.run(_serve()), daemon=True)
    thread.start()
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
    # Daemon thread — no explicit shutdown needed; process exit reaps it.
