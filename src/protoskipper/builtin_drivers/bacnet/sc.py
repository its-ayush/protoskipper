# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet/SC (Secure Connect) transport scaffold (P7.C).

BACnet/SC (ASHRAE Addendum 135-2020bj) uses WebSocket over TLS instead of
BVLC/UDP.  This module provides a skeleton ``BACnetSCSession`` that validates
connection parameters and raises ``NotImplementedError`` for all operations
until a bacpypes3 version that supports Annex AB becomes available.

Minimum bacpypes3 version required for production use: **0.0.110** (estimated).
Current shipped version: 0.0.106.

Usage::

    from protoskipper.builtin_drivers.bacnet.sc import BACnetSCSession

    sess = BACnetSCSession(
        hub_uri="wss://bacnet-hub.example.com:47808",
        ca_cert="/etc/ssl/certs/bacnet-ca.pem",
        client_cert="/etc/ssl/certs/bacnet-client.pem",
        client_key="/etc/ssl/private/bacnet-client.key",
    )
    # sess.read_property(...)  ← raises NotImplementedError
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["BACnetSCSession"]

_NOT_IMPLEMENTED_MSG = (
    "BACnet/SC is not yet implemented. "
    "Requires bacpypes3 >= 0.0.110 with Annex AB WebSocket support. "
    "Current shipped version: 0.0.106."
)


class BACnetSCSession:
    """Stub for a BACnet/SC (WebSocket+TLS) session.

    Parameters
    ----------
    hub_uri:
        WebSocket URI of the BACnet/SC hub node (must begin with ``wss://``).
    ca_cert:
        Path to the CA certificate PEM file for TLS validation.
    client_cert:
        Path to the client certificate PEM file.
    client_key:
        Path to the client private key PEM file.
    device_id:
        Local device instance number for the SC node.
    vmac:
        Virtual MAC address (48-bit hex string) for this node.
    """

    def __init__(
        self,
        hub_uri: str,
        *,
        ca_cert: str | Path,
        client_cert: str | Path,
        client_key: str | Path,
        device_id: int = 0,
        vmac: str = "00:00:00:00:00:01",
    ) -> None:
        if not hub_uri.startswith("wss://"):
            raise ValueError(f"hub_uri must start with 'wss://' for BACnet/SC, got: {hub_uri!r}")
        self.hub_uri = hub_uri
        self.ca_cert = Path(ca_cert)
        self.client_cert = Path(client_cert)
        self.client_key = Path(client_key)
        self.device_id = device_id
        self.vmac = vmac

    def connect(self) -> None:
        """Open the WebSocket connection to the SC hub."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def disconnect(self) -> None:
        """Close the WebSocket connection."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def read_property(self, object_id: str, prop: str) -> object:
        """ReadProperty over BACnet/SC."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def write_property(self, object_id: str, prop: str, value: object, priority: int = 0) -> None:
        """WriteProperty over BACnet/SC."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def who_is(self, low: int = 0, high: int = 4194303) -> list[dict[str, object]]:
        """Who-Is / I-Am discovery over BACnet/SC."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def __repr__(self) -> str:
        return f"<BACnetSCSession hub={self.hub_uri!r} device_id={self.device_id}>"
