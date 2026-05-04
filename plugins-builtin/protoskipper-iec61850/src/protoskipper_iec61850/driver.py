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

import re
from collections.abc import Iterator
from typing import Any, ClassVar

from protoskipper.core.driver import (
    DeviceRef,
    DriverSession,
    ObjectRef,
    ProtocolDriver,
    ReadResult,
    SafetyContext,
    WriteIntent,
    WriteResult,
)
from protoskipper.core.errors import EncodingError

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


class Iec61850MmsDriver(ProtocolDriver):
    """ProtoSkipper driver for IEC 61850 MMS (ISO 9506 over TCP/102).

    Phase 8 scaffold — all live operations raise :class:`NotImplementedError`
    until the corresponding P8.B.x tasks are completed.
    """

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
        """Open an MMS session to ``device``.

        .. note::
            Not yet implemented (P8.B.2).
        """
        raise NotImplementedError(
            "IEC 61850 MMS connect is not yet implemented.  "
            "See P8.B.2 in docs/internal/EXECUTION_PLAN.md."
        )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class Iec61850MmsSession(DriverSession):
    """An open IEC 61850 MMS connection to a single IED.

    Phase 8 scaffold — all methods raise :class:`NotImplementedError`.
    """

    # Populated by the real implementation (P8.B.2).
    device: DeviceRef
    safety: SafetyContext

    def __init__(self, device: DeviceRef, safety: SafetyContext) -> None:
        self.device = device
        self.safety = safety

    def enumerate_objects(self) -> Iterator[ObjectRef]:
        """Discover the IED data model.

        .. note::
            Not yet implemented (P8.B.3).
        """
        raise NotImplementedError(
            "IEC 61850 data-model discovery is not yet implemented.  "
            "See P8.B.3 in docs/internal/EXECUTION_PLAN.md."
        )
        return
        yield  # make this a generator

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
        """Release MMS transport resources.

        .. note::
            Not yet implemented (P8.B.2).  No-op at scaffold stage.
        """
