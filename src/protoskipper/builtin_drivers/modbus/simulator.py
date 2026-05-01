# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Standalone Modbus TCP slave for testing ProtoSkipper end-to-end.

Run via the bundled CLI::

    protoskipper sim modbus

or directly::

    python -m protoskipper.builtin_drivers.modbus.simulator

The simulator listens on ``localhost:5020`` (a non-privileged port so no
sudo is required) with a small register map: 100 holding registers, 100
input registers, 100 coils, 100 discrete inputs, all initialised with
distinguishable test values.

This is also used as a pytest fixture by the integration tests; running it
manually is the same code path with a longer-running event loop.
"""

from __future__ import annotations

import logging
import sys

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5020

_logger = logging.getLogger(__name__)


def build_datastore():
    """Return a pymodbus device context with sample data.

    In pymodbus 3.7+ this class is ``ModbusDeviceContext``; older 3.x had
    it named ``ModbusSlaveContext``. We try both so the simulator works
    across versions a user might have pinned.
    """
    from pymodbus.datastore import ModbusSequentialDataBlock

    try:
        from pymodbus.datastore import ModbusDeviceContext as DeviceContext
    except ImportError:  # pragma: no cover - older pymodbus
        from pymodbus.datastore import ModbusSlaveContext as DeviceContext  # type: ignore[no-redef]

    # pymodbus 3.13 requires address >= 1 (internally does address-1 to map
    # to wire-zero addressing). Earlier versions accepted 0; we pick 1 to
    # work cleanly with both.
    holding = ModbusSequentialDataBlock(1, [v + 100 for v in range(100)])
    input_regs = ModbusSequentialDataBlock(1, [v + 200 for v in range(100)])
    coils = ModbusSequentialDataBlock(1, [v % 2 == 0 for v in range(100)])
    discrete = ModbusSequentialDataBlock(1, [v % 3 == 0 for v in range(100)])

    # ``zero_mode=True`` was removed in pymodbus 3.7+ (zero-based addressing
    # is the default now). Pass it only when accepted to stay compatible
    # with older releases.
    try:
        return DeviceContext(di=discrete, co=coils, hr=holding, ir=input_regs, zero_mode=True)
    except TypeError:
        return DeviceContext(di=discrete, co=coils, hr=holding, ir=input_regs)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Block forever serving Modbus TCP requests."""
    from pymodbus.datastore import ModbusServerContext
    from pymodbus.server import StartTcpServer

    context = ModbusServerContext(slaves=build_datastore(), single=True)
    _logger.info("Modbus simulator listening on %s:%d", host, port)
    print(f"ProtoSkipper Modbus simulator listening on {host}:{port}", file=sys.stderr)
    print("  Holding registers 0..99 = 100..199", file=sys.stderr)
    print("  Input   registers 0..99 = 200..299", file=sys.stderr)
    print("  Coils         0..99 alternate true/false", file=sys.stderr)
    print("  Discrete inputs 0..99 every-third true", file=sys.stderr)
    print("  Press Ctrl-C to stop.", file=sys.stderr)
    StartTcpServer(context=context, address=(host, port))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="protoskipper.builtin_drivers.modbus.simulator",
        description="Modbus TCP slave for testing ProtoSkipper without real hardware.",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"Bind address (default: {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Bind port (default: {DEFAULT_PORT})"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        serve(args.host, args.port)
    except KeyboardInterrupt:
        raise SystemExit(0) from None
