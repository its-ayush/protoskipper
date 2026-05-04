# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""IEC 61850 MMS driver — :class:`ProtocolDriver` integration.

Address format
--------------

``host[:port]``  e.g. ``192.168.1.10`` or ``192.168.1.10:102``

Optional query parameters (appended with ``?``):

``ap_title=1,3,9999,33``  — calling AP-Title OID (default ``1,3,9999,33``)
``called_ap_title=1,3,9999,33``  — called AP-Title
``edition=2.1``  — enforce a specific SCL edition (default ``auto``)

Example::

    192.168.1.10:102?ap_title=1,3,9999,33&edition=2.1

Implementation notes
--------------------

This module is a scaffold (P8.A.1).  Every method raises
:class:`NotImplementedError` with a note pointing to the relevant
P8.B.x sub-task that implements it.  The goal is:

* The driver registers cleanly via the entry-point group.
* ``protoskipper list-protocols`` shows ``iec61850.mms``.
* ``protoskipper-gui`` does not crash when the plugin is installed.
* Tests can verify the contract without requiring a live IED.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import Any, ClassVar

from protoskipper.core.driver import (
    Access,
    DeviceRef,
    DriverSession,
    ObjectRef,
    ProtocolDriver,
    ReadResult,
    SafetyContext,
    WriteIntent,
    WriteResult,
)
from protoskipper.core.errors import ConnectionFailure, EncodingError

from protoskipper_iec61850._mms_client import (
    ACSI_CLASS_DATA_OBJECT,
    MmsClient,
    MmsConnectError,
    MmsDirectoryError,
)

_logger = logging.getLogger(__name__)

__all__ = ["Iec61850MmsDriver", "Iec61850MmsSession"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MMS_PORT: int = 102

_ADDRESS_RE = re.compile(
    r"^(?P<host>[^:?]+)"
    r"(?::(?P<port>\d+))?"
    r"(?:\?(?P<query>.*))?$"
)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _host_port_from_address(address: str) -> tuple[str, int]:
    """Extract ``(host, port)`` from a normalised :class:`DeviceRef` address.

    The address has already been through :meth:`Iec61850MmsDriver.parse_address`
    so it is guaranteed to be ``host:port[?query]``.
    """
    base = address.split("?")[0]
    host, _, port_str = base.rpartition(":")
    return host, int(port_str)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class Iec61850MmsDriver(ProtocolDriver):
    """ProtoSkipper driver for IEC 61850 MMS (ISO 9506 over TCP/102)."""

    PROTOCOL_ID: ClassVar[str] = "iec61850.mms"
    DISPLAY_NAME: ClassVar[str] = "IEC 61850 (MMS)"
    DESCRIPTION: ClassVar[str] = (
        "IEC 61850 MMS over TCP/102 — browse data model, read/write "
        "data attributes, subscribe to reports, GOOSE/SV monitoring."
    )

    def parse_address(self, address: str) -> DeviceRef:
        """Parse ``host[:port][?query]`` into a :class:`DeviceRef`.

        Raises
        ------
        EncodingError
            If ``address`` does not match the expected format.
        """
        m = _ADDRESS_RE.match(address.strip())
        if m is None:
            raise EncodingError(f"Invalid IEC 61850 address: {address!r}")
        host = m.group("host")
        port = int(m.group("port")) if m.group("port") else DEFAULT_MMS_PORT
        if not (1 <= port <= 65535):
            raise EncodingError(f"Port {port} out of range 1-65535")
        normalised = f"{host}:{port}"
        if m.group("query"):
            normalised += f"?{m.group('query')}"
        return DeviceRef(
            protocol=self.PROTOCOL_ID,
            address=normalised,
            label=host,
        )

    def discover(self, target: str) -> Iterator[DeviceRef]:
        """Probe ``target`` for IEC 61850 IEDs.

        .. note::
            Not yet implemented (P8.B.2).  Yields nothing silently.
        """
        return
        yield  # make this a generator even before P8.B.2 lands

    def connect(
        self,
        device: DeviceRef,
        safety: SafetyContext,
    ) -> Iec61850MmsSession:
        """Open an MMS Initiate exchange and return a live session.

        Raises
        ------
        ImportError
            If ``pyiec61850`` is not installed (see build instructions in
            ``docs/internal/IEC61850_MMS_LIBRARY.md``).
        ConnectionFailure
            If the MMS Initiate is rejected or times out.
        """
        host, port = _host_port_from_address(device.address)
        client = MmsClient(host, port)
        try:
            client.connect()
        except MmsConnectError as exc:
            raise ConnectionFailure(str(exc)) from exc
        return Iec61850MmsSession(device=device, safety=safety, client=client)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class Iec61850MmsSession(DriverSession):
    """An open IEC 61850 MMS connection to a single IED."""

    device: DeviceRef
    safety: SafetyContext

    def __init__(
        self,
        device: DeviceRef,
        safety: SafetyContext,
        client: MmsClient | None = None,
    ) -> None:
        self.device = device
        self.safety = safety
        self._client = client

    @property
    def negotiated_pdu_size(self) -> int:
        """Maximum PDU size negotiated in MMS Initiate (0 if unavailable)."""
        return self._client.negotiated_pdu_size if self._client else 0

    @property
    def peer_implementation(self) -> str:
        """Peer implementation string from MMS Initiate response."""
        return self._client.peer_implementation if self._client else ""

    def enumerate_objects(self) -> Iterator[ObjectRef]:
        """Walk the IED data model via MMS GetDirectory services.

        Traversal order: server -> logical device -> logical node ->
        data object.  Each data object is yielded as one
        :class:`~protoskipper.core.driver.ObjectRef`.

        Actual data types and writability are resolved during reads
        (P8.B.4).  ``data_type`` is ``"do"`` for every yielded ref.

        Errors on individual directory queries are logged at WARNING and
        skipped; they do not abort the whole enumeration.

        Yields nothing if the session has no active client.
        """
        if self._client is None:
            return

        try:
            ld_names = self._client.get_server_directory()
        except MmsDirectoryError as exc:
            _logger.warning("GetServerDirectory failed: %s", exc)
            return

        for ld_name in ld_names:
            try:
                ln_names = self._client.get_logical_device_directory(ld_name)
            except MmsDirectoryError as exc:
                _logger.warning("GetLogicalDeviceDirectory(%r) failed: %s", ld_name, exc)
                continue

            for ln_name in ln_names:
                ln_ref = f"{ld_name}/{ln_name}"
                try:
                    do_names = self._client.get_logical_node_directory(
                        ln_ref, ACSI_CLASS_DATA_OBJECT
                    )
                except MmsDirectoryError as exc:
                    _logger.warning("GetLogicalNodeDirectory(%r) failed: %s", ln_ref, exc)
                    continue

                for do_name in do_names:
                    do_ref = f"{ln_ref}.{do_name}"
                    yield ObjectRef(
                        device=self.device,
                        object_id=do_ref,
                        data_type="do",
                        access=Access.READ_ONLY,
                        label=do_ref,
                    )

    def read(self, ref: ObjectRef) -> ReadResult:
        """Read a single data attribute.

        .. note::
            Not yet implemented (P8.B.4).
        """
        raise NotImplementedError(
            "IEC 61850 read is not yet implemented.  See P8.B.4 in docs/internal/EXECUTION_PLAN.md."
        )

    def prepare_write(self, ref: ObjectRef, value: Any) -> WriteIntent:
        """Encode a write intent (no I/O).

        .. note::
            Not yet implemented (P8.B.5).
        """
        raise NotImplementedError(
            "IEC 61850 prepare_write is not yet implemented.  "
            "See P8.B.5 in docs/internal/EXECUTION_PLAN.md."
        )

    def commit_write(self, intent: WriteIntent) -> WriteResult:
        """Transmit a prepared write.

        .. note::
            Not yet implemented (P8.B.5).
        """
        raise NotImplementedError(
            "IEC 61850 commit_write is not yet implemented.  "
            "See P8.B.5 in docs/internal/EXECUTION_PLAN.md."
        )

    def close(self) -> None:
        """Send MMS Close and release all transport resources."""
        if self._client is not None:
            self._client.close()
            self._client = None
