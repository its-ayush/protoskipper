#!/usr/bin/env python3.11
"""Fetch ICD/CID files from IEC 61850 IEDs (IED-Scout style).

Usage::

    python3.11 scripts/fetch_icd.py [--hosts HOST,...] [--port PORT]
                                     [--out-dir DIR] [--timeout MS]

By default probes all 16 known IEDs at 10.10.14.123-140
(excluding .134 and .136 which don't respond).

What it does
------------
1. MMS Initiate (connect) to each IED on port 102.
2. GetFileAttributeValues on root directory "" to list files.
3. Download any file whose name ends in .icd, .cid, .scd, or .iid.
4. Save to <out-dir>/<ip>/<filename>.

Requires ``pyiec61850`` to be installed (built from libiec61850 source).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

# Default IED list (confirmed alive from prior scan)
_DEFAULT_HOSTS = [
    "10.10.14.123",
    "10.10.14.124",
    "10.10.14.125",
    "10.10.14.126",
    "10.10.14.127",
    "10.10.14.128",
    "10.10.14.129",
    "10.10.14.130",
    "10.10.14.131",
    "10.10.14.132",
    "10.10.14.133",
    "10.10.14.135",
    "10.10.14.137",
    "10.10.14.138",
    "10.10.14.139",
    "10.10.14.140",
]

_ICD_EXTENSIONS = {".icd", ".cid", ".scd", ".iid"}

# Directories to search on the IED in addition to root
_DIRECTORIES_TO_PROBE = [
    "",  # root
    "/",
    "COMTRADE",
    "IED",
    "cfg",
    "config",
]


def _fetch_one(host: str, port: int, timeout_ms: int, out_dir: Path) -> None:
    """Connect to one IED, list files, download ICD/CID files."""
    from protoskipper_iec61850._mms_client import (  # type: ignore[import]
        MmsClient,
        MmsConnectError,
        MmsDirectoryError,
    )

    print(f"\n{'=' * 60}")
    print(f"Connecting to {host}:{port} …")

    client = MmsClient(host, port, connect_timeout_ms=timeout_ms)
    try:
        client.connect()
    except MmsConnectError as exc:
        print(f"  CONNECT FAILED: {exc}")
        return
    except ImportError as exc:
        print(f"  ERROR: {exc}")
        sys.exit(1)

    print(f"  Connected (PDU size={client.negotiated_pdu_size})")

    ied_dir = out_dir / host
    ied_dir.mkdir(parents=True, exist_ok=True)

    # remote_path -> size
    found_files: dict[str, int] = {}

    with client:
        # Try GetServerDirectory to show logical device structure
        try:
            logical_devices = client.get_server_directory()
            print(f"  Logical devices: {logical_devices}")
        except MmsDirectoryError as exc:
            print(f"  GetServerDirectory failed (non-fatal): {exc}")

        # Probe each candidate directory
        seen_dirs: set[str] = set()
        dirs_to_visit = list(_DIRECTORIES_TO_PROBE)

        while dirs_to_visit:
            d = dirs_to_visit.pop(0)
            if d in seen_dirs:
                continue
            seen_dirs.add(d)

            try:
                entries = client.list_files(d)
            except MmsDirectoryError as exc:
                _log.debug("list_files(%r) on %s: %s", d, host, exc)
                continue

            for entry in entries:
                name = entry.name
                suffix = Path(name).suffix.lower()
                if suffix in _ICD_EXTENSIONS:
                    # The name returned by the IED is already the full path
                    # or just the filename; build the full remote path.
                    if d and not name.startswith("/") and not name.startswith(d):
                        sep = "" if d.endswith("/") else "/"
                        remote_path = f"{d}{sep}{name}"
                    else:
                        remote_path = name
                    found_files[remote_path] = entry.size
                elif not suffix:
                    # Might be a subdirectory — add to visit list
                    candidate = name.rstrip("/")
                    if candidate not in seen_dirs and "/" not in candidate:
                        dirs_to_visit.append(candidate)

        if not found_files:
            print("  No ICD/CID/SCD files found in root or standard directories.")
            # Print full directory listing for manual inspection
            try:
                all_entries = client.list_files("")
                if all_entries:
                    print("  Root directory contents:")
                    for e in all_entries:
                        print(f"    {e.name!r:40s}  {e.size:>10} bytes")
                else:
                    print("  Root directory is empty.")
            except MmsDirectoryError as exc:
                print(f"  Cannot list root: {exc}")
            return

        # Download found files
        for remote_path, size in found_files.items():
            local_name = Path(remote_path).name
            local_path = ied_dir / local_name
            print(f"  Downloading {remote_path!r} ({size} bytes) …", end=" ")
            try:
                data = client.get_file(remote_path)
                local_path.write_bytes(data)
                print(f"saved → {local_path}")
            except MmsDirectoryError as exc:
                print(f"FAILED: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hosts",
        default=",".join(_DEFAULT_HOSTS),
        help="Comma-separated list of IED IP addresses",
    )
    parser.add_argument("--port", type=int, default=102, help="MMS port (default 102)")
    parser.add_argument(
        "--out-dir",
        default="icd_files",
        help="Output directory for downloaded files (default: icd_files/)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10_000,
        help="MMS connect timeout in milliseconds (default 10000)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Fetching ICD/CID files from {len(hosts)} IED(s) → {out_dir}/")

    for host in hosts:
        try:
            _fetch_one(host, args.port, args.timeout, out_dir)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 1
        except Exception as exc:
            print(f"  UNEXPECTED ERROR on {host}: {exc}")
            _log.debug("Unexpected error on %s", host, exc_info=True)

    print(f"\nDone. Files saved in: {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
