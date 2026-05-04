#!/usr/bin/env python3.11
# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""browse_ied.py — IEC 61850 IED tag browser (headless CLI tool).

Usage
-----
    python3.11 scripts/browse_ied.py --host 10.10.14.123 [options]

Options
-------
--host HOST         IED hostname or IP address (required)
--port PORT         MMS port (default: 102)
--timeout MS        Connection timeout in milliseconds (default: 5000)
--ied-name NAME     Filter SCL expansion to a specific IED name
--tree              Print the tag hierarchy as an indented tree
--tabular           Print tags in a plain TSV table (default)
--poll N            After listing, read each tag N times (0 = no poll)
--poll-interval S   Seconds between reads when polling (default: 1.0)
--ld INST           Filter output to this Logical Device instance
--ln REF            Filter output to this LN reference (e.g. "MMXU1")
--do NAME           Filter output to this Data Object name (e.g. "A")
--fc FC             Filter output to this Functional Constraint

Examples
--------
    # List all tags from IED at 10.10.14.123
    python3.11 scripts/browse_ied.py --host 10.10.14.123

    # Tree view, filtered to the CTRL logical device
    python3.11 scripts/browse_ied.py --host 10.10.14.123 --tree --ld CTRL

    # Read MMXU1.A attributes once
    python3.11 scripts/browse_ied.py --host 10.10.14.123 --ln MMXU1 --do A --poll 1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Ensure the project source is importable when running from the repo root.
_repo = Path(__file__).resolve().parents[1]
if str(_repo / "src") not in sys.path:
    sys.path.insert(0, str(_repo / "src"))
if str(_repo / "plugins-builtin" / "protoskipper-iec61850" / "src") not in sys.path:
    sys.path.insert(0, str(_repo / "plugins-builtin" / "protoskipper-iec61850" / "src"))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Browse IEC 61850 IED tag model via MMS + SCL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", required=True, help="IED hostname or IP address")
    p.add_argument("--port", type=int, default=102, help="MMS port (default: 102)")
    p.add_argument(
        "--timeout", type=int, default=5000, help="Connection timeout in ms (default: 5000)"
    )
    p.add_argument("--ied-name", default=None, help="Filter SCL to a specific IED name")
    p.add_argument("--tree", action="store_true", help="Print tags as indented tree")
    p.add_argument("--tabular", action="store_true", help="Print tags as TSV table (default)")
    p.add_argument("--poll", type=int, default=0, metavar="N", help="Read each tag N times")
    p.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        metavar="S",
        help="Seconds between poll reads (default: 1.0)",
    )
    p.add_argument("--ld", default=None, help="Filter: Logical Device instance")
    p.add_argument("--ln", default=None, help="Filter: LN reference (e.g. MMXU1)")
    p.add_argument("--do", default=None, dest="do_name", help="Filter: Data Object name")
    p.add_argument("--fc", default=None, help="Filter: Functional Constraint")
    return p.parse_args()


def _apply_filters(
    tags: list,
    ld: str | None,
    ln: str | None,
    do_name: str | None,
    fc: str | None,
) -> list:
    result = tags
    if ld:
        result = [t for t in result if t.ld_inst == ld]
    if ln:
        result = [t for t in result if t.ln_ref == ln]
    if do_name:
        result = [t for t in result if t.do_name == do_name]
    if fc:
        result = [t for t in result if t.fc == fc]
    return result


def _print_tree(tags: list) -> None:
    from collections import defaultdict

    ld_map: dict = defaultdict(lambda: defaultdict(list))
    for tag in tags:
        ld_map[tag.ld_inst][tag.ln_ref].append(tag)

    for ld_name in sorted(ld_map):
        print(f"[LD] {ld_name}")
        for ln_name in sorted(ld_map[ld_name]):
            print(f"  [LN] {ln_name}")
            # Group by DO
            do_map: dict = defaultdict(list)
            for tag in ld_map[ld_name][ln_name]:
                do_map[tag.do_name].append(tag)
            for do_name in sorted(do_map):
                do_tags = do_map[do_name]
                cdc = do_tags[0].cdc if do_tags else ""
                print(f"    [DO] {do_name} ({cdc})")
                for tag in do_tags:
                    rw = "RW" if tag.writable else "RO"
                    print(f"      [{tag.fc}] .{tag.da_path}  {tag.basic_type}  {rw}")


def _print_tabular(tags: list) -> None:
    cols = ("MMS Path", "FC", "Type", "CDC", "Access")
    widths = [50, 4, 10, 8, 6]
    sep = "  "
    header = sep.join(c.ljust(w) for c, w in zip(cols, widths, strict=True))
    print(header)
    print("-" * len(header))
    for tag in tags:
        rw = "RW" if tag.writable else "RO"
        row = sep.join(
            [
                tag.mms_path.ljust(widths[0]),
                tag.fc.ljust(widths[1]),
                tag.basic_type.ljust(widths[2]),
                tag.cdc.ljust(widths[3]),
                rw.ljust(widths[4]),
            ]
        )
        print(row)


def _poll_tags(client: object, tags: list, n: int, interval_s: float) -> None:
    """Read each tag n times and print the results.

    Uses the MmsClient directly via its internal read helper.
    Only tags with a known functional constraint are read.
    """
    print(f"\n--- Polling {len(tags)} tags, {n} read(s) each, {interval_s}s interval ---\n")
    read_fn = getattr(client, "_read_da", None)
    if read_fn is None:
        print("NOTE: Live read not available in this environment.", file=sys.stderr)
        return

    for i in range(n):
        print(f"Read #{i + 1}")
        for tag in tags:
            try:
                data = read_fn(tag.mms_path, tag.fc)
                print(f"  {tag.mms_path:<50} = {data}")
            except Exception as exc:
                print(f"  {tag.mms_path:<50} ! {exc}")
        if i < n - 1:
            time.sleep(interval_s)


def main() -> None:
    args = _parse_args()

    try:
        from protoskipper_iec61850._mms_client import MmsClient, MmsConnectError, MmsDirectoryError
        from protoskipper_iec61850.scl.dtt import IecTag, expand_tags
    except ImportError as exc:
        print(f"ERROR: Cannot import IEC 61850 plugin: {exc}", file=sys.stderr)
        print(
            "Ensure protoskipper-iec61850 is installed and pyiec61850 is available.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Connecting to {args.host}:{args.port} (timeout={args.timeout}ms)…")
    try:
        with MmsClient(args.host, args.port, connect_timeout_ms=args.timeout) as client:
            print(f"  Connected (PDU size={client.negotiated_pdu_size})")

            # Fetch and parse SCL
            print("Fetching SCL configuration…")
            try:
                xml_bytes = client.fetch_scl()
            except MmsDirectoryError as exc:
                print(f"  ERROR: Cannot fetch SCL: {exc}", file=sys.stderr)
                sys.exit(1)

            print(f"  Got {len(xml_bytes):,} bytes of SCL XML.")
            tags: list[IecTag] = expand_tags(xml_bytes, ied_name=args.ied_name)
            print(f"  Expanded {len(tags)} leaf data attributes.")

            # Apply filters
            tags = _apply_filters(tags, args.ld, args.ln, args.do_name, args.fc)
            if not tags:
                print("No tags match the given filters.")
                sys.exit(0)

            print(f"  Showing {len(tags)} tags after filtering.\n")

            # Display
            if args.tree:
                _print_tree(tags)
            else:
                _print_tabular(tags)

            # Optional polling
            if args.poll > 0:
                _poll_tags(client, tags, args.poll, args.poll_interval)

    except MmsConnectError as exc:
        print(f"CONNECT FAILED: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
