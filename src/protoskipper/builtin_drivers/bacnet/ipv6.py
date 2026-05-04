# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet/IPv6 transport scaffold (P7.B.2).

BACnet/IPv6 (ASHRAE Annex U) replaces UDP/IPv4 BVLC with BVLCI6 messages
carried over UDP/IPv6.  This module provides a skeleton ``BACnetIPv6Session``
that documents the BVLCI6 function codes and will delegate to bacpypes3 IPv6
support once that is stable.

Key differences from BACnet/IP (Annex J):
* Port remains 0xBAC0 (47808) on ::1
* BVLC type is 0x82 (not 0x81)
* New BVLCI6 functions: BVLCI6-Result, Original-Unicast-NPDU, Forwarded-NPDU,
  Distribute-Broadcast-To-Network, Register-Foreign-Device,
  Delete-Foreign-Device-Table-Entry, Secure-BVLLL
* Multicast addresses replace broadcast: BACnet all-devices = FF0X::BAC0

Usage::

    from protoskipper.builtin_drivers.bacnet.ipv6 import BACnetIPv6Session

    sess = BACnetIPv6Session(
        local_address="fe80::1",
        interface="eth0",
        device_id=1234,
    )
    # sess.connect()  ← raises NotImplementedError
"""

from __future__ import annotations

__all__ = ["BVLCI6_FUNCTIONS", "BACnetIPv6Session"]

_NOT_IMPLEMENTED_MSG = (
    "BACnet/IPv6 (Annex U) is not yet implemented. "
    "Requires bacpypes3 >= 0.0.110 with BVLCI6 support."
)

# BVLCI6 function codes (ASHRAE 135 Annex U Table U-2)
BVLCI6_FUNCTIONS: dict[int, str] = {
    0x00: "BVLCI6-Result",
    0x01: "Original-Unicast-NPDU",
    0x02: "Original-Multicast-NPDU",
    0x03: "Forwarded-NPDU",
    0x04: "Register-Foreign-Device",
    0x05: "Delete-Foreign-Device-Table-Entry",
    0x06: "Distribute-Broadcast-To-Network",
    0x07: "Original-Unicast-NPDU-Expecting-Reply",
    0x08: "Secure-BVLLL",
}

# Well-known BACnet/IPv6 multicast addresses
BACNET_IPV6_MULTICAST = {
    "all-devices": "FF0E::BAC0",  # site-local scope (0E)
    "all-bbmds": "FF0E::BAC1",
    "all-nodes-link-local": "FF02::BAC0",  # link-local scope (02)
}


class BACnetIPv6Session:
    """Stub for a BACnet/IPv6 session.

    Parameters
    ----------
    local_address:
        Local IPv6 address to bind (e.g. ``"fe80::1%eth0"``).
    interface:
        Network interface name for link-local scope (e.g. ``"eth0"``).
    device_id:
        Local device instance number.
    port:
        UDP port (default 47808 = 0xBAC0).
    multicast_scope:
        Multicast scope character: ``"2"`` (link-local) or ``"e"`` (site).
    """

    def __init__(
        self,
        local_address: str,
        *,
        interface: str = "",
        device_id: int = 0,
        port: int = 47808,
        multicast_scope: str = "e",
    ) -> None:
        if ":" not in local_address:
            raise ValueError(
                f"Expected an IPv6 address, got: {local_address!r}. "
                "BACnet/IPv6 (Annex U) requires an IPv6 local address."
            )
        self.local_address = local_address
        self.interface = interface
        self.device_id = device_id
        self.port = port
        self.multicast_scope = multicast_scope

    def connect(self) -> None:
        """Bind to the local IPv6 address and join BACnet multicast groups."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def disconnect(self) -> None:
        """Leave multicast groups and close the socket."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def who_is(self, low: int = 0, high: int = 4194303) -> list[dict[str, object]]:
        """Multicast Who-Is on the BACnet/IPv6 segment."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def read_property(self, device_id: int, object_id: str, prop: str) -> object:
        """ReadProperty via BACnet/IPv6."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    @property
    def all_devices_multicast(self) -> str:
        """Return the site-local all-devices multicast address."""
        return f"FF0{self.multicast_scope.upper()}::BAC0"

    def __repr__(self) -> str:
        return (
            f"<BACnetIPv6Session local={self.local_address!r} "
            f"device_id={self.device_id} port={self.port}>"
        )
