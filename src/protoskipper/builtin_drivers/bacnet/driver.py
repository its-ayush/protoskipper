# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet/IP protocol driver — :class:`ProtocolDriver` integration.

Address format
--------------

``[bacnet://]host[:port][/dev=N]``

Examples::

    192.168.1.100
    192.168.1.100:47808
    192.168.1.100:47808/dev=1234
    bacnet://10.0.0.5/dev=4194302

``port`` defaults to 47808 (BACnet/IP standard).
``/dev=N`` pins the remote device instance; omit for autodetect.

Object id format
----------------

``<object-type>:<instance>``  e.g. ``analog-value:1``, ``binary-output:5``

The object type is the ASHRAE 135-2020 snake-case name (matching bacpypes3
conventions).  Short aliases (``AV``, ``BO``, etc.) are also accepted in the
points-list CSV but are normalised to the long form before storage.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

from protoskipper.core.driver import (
    DeviceRef,
    DriverSession,
    ProtocolDriver,
    SafetyContext,
)
from protoskipper.core.errors import EncodingError

from .client import BacnetIpSession
from .points import PointDef, load_point_list
from .vendor_profiles import get_profile

_logger = logging.getLogger(__name__)

DEFAULT_PORT = 47808
_BACNET_IP_RE = re.compile(
    r"""
    ^
    (?:bacnet://)?
    (?P<host>[^\s:/,]+)
    (?::(?P<port>\d+))?
    (?:/dev=(?P<dev_id>\d+))?
    $
    """,
    re.VERBOSE | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------


def parse_address(address: str) -> tuple[str, int, int | None]:
    """Parse a BACnet/IP address string.

    Returns ``(host, port, device_id_or_None)``.
    Raises :class:`~protoskipper.core.errors.EncodingError` on bad input.
    """
    cleaned = address.strip().removeprefix("bacnet://")
    m = _BACNET_IP_RE.match(cleaned)
    if not m:
        raise EncodingError(f"Cannot parse BACnet/IP address: {address!r}")
    host = m.group("host")
    port = int(m.group("port") or DEFAULT_PORT)
    dev_id = int(m.group("dev_id")) if m.group("dev_id") else None
    return host, port, dev_id


def _canonical_address(host: str, port: int, dev_id: int | None) -> str:
    """Rebuild the canonical address string from parsed components."""
    addr = f"{host}:{port}" if port != DEFAULT_PORT else host
    if dev_id is not None:
        addr += f"/dev={dev_id}"
    return addr


# ---------------------------------------------------------------------------
# BACnet/IP driver
# ---------------------------------------------------------------------------


class BacnetIpDriver(ProtocolDriver):
    """BACnet/IP master driver (BVLC/NPDU/APDU over UDP/47808)."""

    PROTOCOL_ID: ClassVar[str] = "bacnet.ip"
    DISPLAY_NAME: ClassVar[str] = "BACnet/IP"
    DESCRIPTION: ClassVar[str] = "BACnet/IP client (ASHRAE 135 Annex J)"

    def __init__(self) -> None:
        self._point_lists: dict[str, list[PointDef]] = {}

    # ------------------------------------------------------------------
    # Point list registration (called by GUI / CLI before connect)
    # ------------------------------------------------------------------

    def register_point_list(self, address: str, points: list[PointDef]) -> None:
        """Attach a pre-parsed points list for *address*."""
        self._point_lists[address] = points

    def load_point_list_from_path(self, address: str, path: Path | str) -> None:
        """Parse and register a points list from a CSV / EDE / JSON file."""
        self._point_lists[address] = load_point_list(Path(path))

    # ------------------------------------------------------------------
    # ProtocolDriver API
    # ------------------------------------------------------------------

    def parse_address(self, address: str) -> DeviceRef:
        """Validate and normalise a BACnet/IP address into a :class:`DeviceRef`."""
        host, port, dev_id = parse_address(address)
        canonical = _canonical_address(host, port, dev_id)
        return DeviceRef(
            protocol=self.PROTOCOL_ID,
            address=canonical,
            label=f"BACnet @ {host}:{port}" + (f" (dev {dev_id})" if dev_id is not None else ""),
            metadata={"host": host, "port": port, "device_id": dev_id},
        )

    def discover(self, target: str) -> Iterator[DeviceRef]:
        """Probe *target* with a BACnet Who-Is broadcast and yield responding devices.

        *target* is one of:

        * ``"broadcast"`` or ``"255.255.255.255"`` — global broadcast.
        * An IP address / hostname — directed unicast Who-Is.
        * A comma-separated list of the above.
        * ``"low=N,high=M"`` — device-instance-range Who-Is.

        Discovery creates a temporary bacpypes3 application, sends Who-Is,
        waits up to 5 seconds for I-Am replies, then tears down the app.
        """
        try:
            from bacpypes3.ipv4.app import NormalApplication  # noqa: F401
            from bacpypes3.local.device import DeviceObject  # noqa: F401
            from bacpypes3.pdu import Address  # noqa: F401
        except ImportError as exc:
            raise EncodingError(
                "bacpypes3 is not installed — run: pip install 'protoskipper[bacnet]'"
            ) from exc

        import asyncio

        # Parse range hints from target
        low_limit: int | None = None
        high_limit: int | None = None
        target_address: str | None = None

        specs = [s.strip() for s in target.split(",") if s.strip()]
        clean_specs: list[str] = []
        for spec in specs:
            m_low = re.match(r"low=(\d+)", spec, re.IGNORECASE)
            m_high = re.match(r"high=(\d+)", spec, re.IGNORECASE)
            if m_low:
                low_limit = int(m_low.group(1))
            elif m_high:
                high_limit = int(m_high.group(1))
            else:
                clean_specs.append(spec)

        if clean_specs and clean_specs[0] not in {"broadcast", "255.255.255.255"}:
            target_address = clean_specs[0]

        # Build a temporary event loop and application for discovery
        loop = asyncio.new_event_loop()
        try:
            devices = loop.run_until_complete(
                self._async_discover_run(
                    loop,
                    low_limit=low_limit,
                    high_limit=high_limit,
                    target_address=target_address,
                    timeout=5.0,
                )
            )
        finally:
            loop.close()

        for dev in devices:
            host_port = dev["address"]
            dev_id = dev["device_id"]
            vendor_id = dev.get("vendor_id")
            profile = get_profile(vendor_id=vendor_id)
            yield DeviceRef(
                protocol=self.PROTOCOL_ID,
                address=f"{host_port}/dev={dev_id}",
                label=f"{profile.display_name} #{dev_id} @ {host_port}",
                metadata={
                    "device_id": dev_id,
                    "vendor_id": vendor_id,
                    "max_apdu": dev.get("max_apdu"),
                    "segmentation": dev.get("segmentation"),
                    "profile_id": profile.id,
                },
            )

    async def _async_discover_run(
        self,
        loop: Any,
        *,
        low_limit: int | None,
        high_limit: int | None,
        target_address: str | None,
        timeout: float,
    ) -> list[dict[str, Any]]:
        from bacpypes3.ipv4.app import NormalApplication
        from bacpypes3.local.device import DeviceObject
        from bacpypes3.pdu import Address

        local_device = DeviceObject(
            objectIdentifier=("device", 3194002),
            objectName="ProtoSkipper-BACnet-Discovery",
            vendorIdentifier=0xFFFF,
        )
        app = NormalApplication(local_device, Address("0.0.0.0"))
        try:
            from bacpypes3.pdu import Address as _Addr

            address = _Addr(target_address) if target_address else None
            result = await app.who_is(
                low_limit=low_limit,
                high_limit=high_limit,
                address=address,
                timeout=timeout,
            )
        except Exception as exc:
            _logger.debug("Who-Is error: %s", exc)
            result = []
        finally:
            app.close()

        devices = []
        for i_am in result or []:
            try:
                devices.append(
                    {
                        "device_id": int(i_am.iAmDeviceIdentifier[1]),
                        "address": str(i_am.pduSource),
                        "max_apdu": int(i_am.maxAPDULengthAccepted),
                        "segmentation": str(i_am.segmentationSupported),
                        "vendor_id": int(i_am.vendorID),
                    }
                )
            except Exception as exc:
                _logger.debug("Malformed I-Am: %s", exc)
        return devices

    def connect(self, device: DeviceRef, safety: SafetyContext) -> DriverSession:
        """Open a BACnet/IP session to *device*."""
        _host, _port, _dev_id = parse_address(device.address)
        # Look up vendor profile from metadata or by performing a quick device read
        vendor_id = device.metadata.get("vendor_id")
        profile_id = device.metadata.get("profile_id")
        profile = get_profile(vendor_id=vendor_id, profile_id=profile_id)

        session = BacnetIpSession(device, safety, vendor_profile=profile)

        # Attach any registered point list
        points = self._point_lists.get(device.address, [])
        if points:
            session._registered_points = points  # type: ignore[attr-defined]

        return session
