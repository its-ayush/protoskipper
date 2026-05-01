# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Command-line entry point for ProtoSkipper.

The CLI is intentionally thin: most user interaction happens through the GUI
(``protoskipper-gui``) or the embedded scripting REPL. The CLI exists for
headless automation, CI integrations, and quick scans from a terminal.
"""
from __future__ import annotations

import argparse
import sys
from typing import Sequence

from protoskipper import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="protoskipper",
        description=(
            "ProtoSkipper - SCADA & BMS protocol testing toolkit. "
            "Run `protoskipper-gui` for the desktop application."
        ),
    )
    parser.add_argument("--version", action="version", version=f"protoskipper {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("list-protocols", help="List protocol drivers discovered via entry points.")

    scan = sub.add_parser("scan", help="Discover devices speaking a given protocol.")
    scan.add_argument("protocol", help="Protocol id (e.g. 'modbus.tcp').")
    scan.add_argument("target", help="Network range, host, or bus identifier.")

    sim = sub.add_parser("sim", help="Run a built-in protocol simulator (test fixture).")
    sim.add_argument("which", choices=["modbus"], help="Which simulator to launch.")
    sim.add_argument("--host", default="127.0.0.1", help="Bind address.")
    sim.add_argument("--port", type=int, default=5020, help="Bind port.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "sim":
        if args.which == "modbus":
            # Local import: pymodbus is an optional dependency.
            from protoskipper.builtin_drivers.modbus.simulator import serve
            serve(host=args.host, port=args.port)
            return 0
        parser.error(f"unknown simulator: {args.which}")
        return 2

    # Lazy import: keeps `--help` and `--version` fast and avoids dragging the
    # plugin loader into every shell completion invocation.
    from protoskipper.core.plugin_loader import load_protocol_drivers

    drivers = load_protocol_drivers()

    if args.command == "list-protocols":
        if not drivers:
            print("No protocol drivers discovered. Try `pip install protoskipper[modbus]`.")
            return 0
        print(f"{'PROTOCOL':<20} {'DRIVER CLASS':<60}")
        for proto_id, cls in sorted(drivers.items()):
            print(f"{proto_id:<20} {cls.__module__}.{cls.__name__}")
        return 0

    if args.command == "scan":
        cls = drivers.get(args.protocol)
        if cls is None:
            print(f"Unknown protocol '{args.protocol}'. Run `protoskipper list-protocols`.",
                  file=sys.stderr)
            return 2
        driver = cls()
        for device in driver.discover(args.target):
            print(device)
        return 0

    parser.error(f"unhandled command: {args.command}")
    return 2  # pragma: no cover - argparse exits before reaching this
