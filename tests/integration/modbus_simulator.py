# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Thin re-export shim kept for backwards compatibility.

The simulator implementation now lives in
:mod:`protoskipper.builtin_drivers.modbus.simulator`. This module
re-exports the public API so existing direct invocations still work::

    python -m tests.integration.modbus_simulator

or via the bundled CLI::

    protoskipper sim modbus

The simulator listens on ``localhost:5020`` (a non-privileged port so no
sudo is required) with a small register map: 100 holding registers, 100
input registers, 100 coils, 100 discrete inputs, all initialised with
distinguishable test values.

This is also used as a pytest fixture by the integration tests; running it
manually is the same code path with a longer-running event loop.
"""
from __future__ import annotations

# Re-export from the canonical package location so the test suite's
# direct invocation (`python -m tests.integration.modbus_simulator`) and
# any existing imports inside tests still work without change.
from protoskipper.builtin_drivers.modbus.simulator import (  # noqa: F401
    DEFAULT_HOST,
    DEFAULT_PORT,
    build_datastore,
    serve,
)

if __name__ == "__main__":
    import argparse
    import logging

    parser = argparse.ArgumentParser(
        prog="modbus_simulator",
        description="Modbus TCP slave for testing ProtoSkipper without real hardware.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Bind address (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Bind port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        serve(args.host, args.port)
    except KeyboardInterrupt:
        raise SystemExit(0)
