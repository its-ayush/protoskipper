# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""The plugin contract every protocol driver implements.

This module defines the protocol-agnostic vocabulary ProtoSkipper uses to
describe devices, addressable points, reads, writes, captures, and the
safety context that gates destructive operations. A new protocol driver
adds support for a fieldbus by subclassing :class:`ProtocolDriver` and
:class:`DriverSession` and registering itself via the
``protoskipper.protocols`` entry-point group.

Design notes
------------

* **Read/write split**: writes are a two-phase operation. ``prepare_write``
  returns a :class:`WriteIntent` with the exact bytes that *would* go on the
  wire; ``commit_write`` is what actually transmits. This split is what
  makes dry-run mode, the confirmation dialog, and the audit log work
  uniformly across every protocol. Drivers that fuse the two will fail
  review.

* **Optional capabilities** (subscribe, simulate, capture) are expressed as
  separate :class:`typing.Protocol` mix-ins, not abstract methods. This lets
  a Modbus driver skip simulation support without a do-nothing stub, and
  lets the GUI introspect capabilities via :func:`isinstance`.

* **Quality** is a first-class field on every :class:`ReadResult`. SCADA
  systems care about whether a value is stale, uncertain, or simulated; the
  GUI uses this to color points in the watchlist.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class SessionProfile(Enum):
    """How strict the SafetyContext is about authorising writes.

    The profile is chosen by the operator at connection time and cannot be
    relaxed for the life of the session. It can be tightened (a session in
    LAB can be promoted to PRODUCTION mid-flight if the operator decides
    the target is now live).
    """

    LAB = "lab"
    """Single-click write confirmation. Intended for benchtop simulators."""

    COMMISSIONING = "commissioning"
    """Explicit confirm dialog with target + value before each write."""

    PRODUCTION = "production"
    """Operator must type the target tag back to confirm each write,
    similar to ``kubectl delete --confirm``."""


class Access(Enum):
    """Whether an :class:`ObjectRef` may be read, written, or both."""

    READ_ONLY = "ro"
    WRITE_ONLY = "wo"
    READ_WRITE = "rw"


class Quality(Enum):
    """Quality flag carried on every :class:`ReadResult`.

    Maps loosely onto the OPC UA / IEC 61850 quality enums but kept small
    so every protocol can populate it sensibly.
    """

    GOOD = "good"
    UNCERTAIN = "uncertain"
    BAD = "bad"
    TIMEOUT = "timeout"
    SIMULATED = "simulated"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Reference types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceRef:
    """Protocol-agnostic handle to a discovered device.

    The ``address`` field is opaque to the GUI: the driver decides its
    format (``"192.168.1.10:502/unit=5"`` for Modbus TCP,
    ``"/dev/ttyUSB0/unit=3@9600,8E1"`` for Modbus RTU,
    ``"bacnet://device/4194302"`` for BACnet, etc.). Drivers must round-trip
    addresses through :meth:`ProtocolDriver.parse_address`.
    """

    protocol: str  #: e.g. ``"modbus.tcp"``; matches ``ProtocolDriver.PROTOCOL_ID``.
    address: str  #: protocol-specific opaque address.
    label: str | None = None  #: human-readable name, if known.
    metadata: Mapping[str, Any] = field(default_factory=dict)  #: free-form per-protocol info.

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.label or self.address} [{self.protocol}]"


@dataclass(frozen=True)
class ObjectRef:
    """Addressable data point on a device (a Modbus register, BACnet object,
    IEC 61850 data attribute, etc.)."""

    device: DeviceRef
    object_id: str  #: protocol-specific id, e.g. ``"holding:40001"``.
    data_type: str  #: ``"uint16"``, ``"float32"``, ``"boolean"``, ``"string"``, …
    access: Access = Access.READ_ONLY
    label: str | None = None  #: tag name from the SCD / device description, if any.
    unit: str | None = None  #: engineering unit, e.g. ``"V"``, ``"kWh"``, ``"degC"``.
    metadata: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Read / Write result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadResult:
    """Outcome of a single read operation."""

    object_ref: ObjectRef
    value: Any
    quality: Quality
    timestamp: datetime
    raw_bytes: bytes | None = None  #: for the packet view + audit log.
    error: str | None = None  #: populated only if quality != GOOD.


@dataclass(frozen=True)
class WriteIntent:
    """Exactly what would be transmitted on the wire if this write commits.

    Returned by :meth:`DriverSession.prepare_write` so the safety layer can
    show the operator a confirmation dialog with the *real* bytes, and the
    audit log can record the intent even if the operator cancels.
    """

    object_ref: ObjectRef
    requested_value: Any
    encoded_bytes: bytes
    description: str  #: human-readable summary, e.g. ``"Set 40001 to 230 (V)"``.
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a committed write."""

    intent: WriteIntent
    success: bool
    timestamp: datetime
    response_bytes: bytes | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Safety context
# ---------------------------------------------------------------------------


class SafetyContext:
    """Runtime gate around write operations.

    Drivers MUST call :meth:`require_write_authorization` before transmitting
    any write. The context resolves the request through the configured
    profile (single-click confirm, dialog, or typed-tag confirmation) and
    records the outcome in the audit log either way.

    The context is intentionally synchronous: an interactive confirmation
    dialog must block the calling thread. Drivers that want to issue writes
    from inside an asyncio loop should hand off to a worker thread.
    """

    def __init__(
        self,
        profile: SessionProfile,
        confirm_callback: ConfirmCallback,
        audit_callback: AuditCallback,
    ) -> None:
        self._profile = profile
        self._confirm = confirm_callback
        self._audit = audit_callback

    @property
    def profile(self) -> SessionProfile:
        return self._profile

    def require_write_authorization(self, intent: WriteIntent) -> bool:
        """Block until the operator authorises (or denies) the write.

        Returns ``True`` if the write may proceed, ``False`` if the operator
        denied it. Either outcome is recorded in the audit log; only an
        authorised intent is later paired with a :class:`WriteResult`.
        """
        authorized = self._confirm(intent, self._profile)
        self._audit(
            event="write_authorization",
            intent=intent,
            authorized=authorized,
            profile=self._profile,
        )
        return authorized


# Callback signatures used by SafetyContext - kept loose on purpose so the
# core does not depend on the GUI or the audit module.
class ConfirmCallback(Protocol):
    def __call__(self, intent: WriteIntent, profile: SessionProfile) -> bool: ...


class AuditCallback(Protocol):
    def __call__(self, **fields: Any) -> None: ...


# ---------------------------------------------------------------------------
# Driver and Session ABCs
# ---------------------------------------------------------------------------


class ProtocolDriver(ABC):
    """Implement this to add a new protocol to ProtoSkipper.

    A driver class is **stateless**; per-connection state lives in the
    :class:`DriverSession` returned by :meth:`connect`. The driver class is
    instantiated once per process; sessions are cheap and short-lived.
    """

    PROTOCOL_ID: ClassVar[str]
    """Unique identifier, e.g. ``"modbus.tcp"``. Must match the entry-point key."""

    DISPLAY_NAME: ClassVar[str]
    """Short label shown in the GUI protocol picker."""

    DESCRIPTION: ClassVar[str] = ""
    """Optional one-line description shown next to the display name."""

    @abstractmethod
    def discover(self, target: str) -> Iterator[DeviceRef]:
        """Probe ``target`` and yield every device this driver can identify.

        ``target`` is a free-form string interpreted by the driver: a CIDR
        range for IP-based protocols, a serial port path for RTU,
        ``"broadcast"`` for BACnet Who-Is, etc. Drivers should yield results
        as they are found rather than collecting them all first, so the GUI
        can populate the device tree progressively.
        """

    @abstractmethod
    def connect(
        self,
        device: DeviceRef,
        safety: SafetyContext,
    ) -> DriverSession:
        """Open a session to ``device``. The returned session owns its
        transport resources and must be closed via :meth:`DriverSession.close`
        (or used as a context manager)."""

    def parse_address(self, address: str) -> DeviceRef:  # noqa: D401
        """Parse a user-typed address string into a :class:`DeviceRef`.

        Default implementation just stores the raw string. Drivers that want
        to support manual entry (vs only discovery) should override and
        validate the format.
        """
        return DeviceRef(protocol=self.PROTOCOL_ID, address=address)


class DriverSession(ABC):
    """An open connection to a single device.

    Sessions are not thread-safe. The GUI dispatches calls from a worker
    thread; drivers that internally use asyncio must marshal to their loop.
    """

    device: DeviceRef
    safety: SafetyContext

    @abstractmethod
    def enumerate_objects(self) -> Iterator[ObjectRef]:
        """Yield every readable/writable point on the device.

        For protocols with self-description (BACnet, IEC 61850) the driver
        queries the device. For protocols that lack it (Modbus) the driver
        consults a register map provided by configuration; it MAY yield a
        synthetic placeholder set so the GUI is not empty.
        """

    @abstractmethod
    def read(self, ref: ObjectRef) -> ReadResult:
        """Fetch the current value of a single point."""

    def read_many(self, refs: list[ObjectRef]) -> list[ReadResult]:
        """Batch read. Default falls back to repeated single reads.

        Drivers should override when the underlying protocol supports
        contiguous-range reads (Modbus FC03, IEC 104 GI), since the
        speed-up is significant for watchlists.
        """
        return [self.read(r) for r in refs]

    @abstractmethod
    def prepare_write(self, ref: ObjectRef, value: Any) -> WriteIntent:
        """Encode ``value`` for the wire and return the intent.

        MUST NOT transmit anything. Encoding errors are raised here, before
        the safety context sees the intent, so the operator cannot authorise
        a malformed write.
        """

    @abstractmethod
    def commit_write(self, intent: WriteIntent) -> WriteResult:
        """Transmit a previously-prepared write.

        Drivers MUST call ``self.safety.require_write_authorization(intent)``
        and refuse to send if it returns False. They MUST NOT bypass the
        safety context, even for "trivially safe" operations.
        """

    @abstractmethod
    def close(self) -> None:
        """Release transport resources."""

    # Context-manager sugar so callers can write ``with driver.connect(...) as s:``
    def __enter__(self) -> DriverSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Optional capability protocols
# ---------------------------------------------------------------------------
#
# Drivers opt into these by inheriting from them in addition to DriverSession.
# The GUI uses isinstance() to decide which UI affordances to show: only
# sessions that implement Subscriber get a "live" indicator, only those that
# implement Capturer get the capture button enabled, etc.


@runtime_checkable
class Subscriber(Protocol):
    """Driver session supports push-style subscriptions / report blocks."""

    def subscribe(
        self,
        refs: list[ObjectRef],
        callback: SubscriptionCallback,
    ) -> SubscriptionHandle: ...

    def unsubscribe(self, handle: SubscriptionHandle) -> None: ...


class SubscriptionCallback(Protocol):
    def __call__(self, result: ReadResult) -> None: ...


class SubscriptionHandle(Protocol):
    """Opaque token returned by :meth:`Subscriber.subscribe`."""


@runtime_checkable
class Simulator(Protocol):
    """Driver session can act as the slave/server side of the protocol."""

    def start_simulator(self, config: Mapping[str, Any]) -> None: ...

    def stop_simulator(self) -> None: ...


@runtime_checkable
class Capturer(Protocol):
    """Driver session can stream raw protocol traffic to a sink."""

    def start_capture(self, sink: CaptureSink) -> None: ...

    def stop_capture(self) -> None: ...


class CaptureSink(Protocol):
    """Where captured protocol frames go.

    A pcapng-backed sink is provided by ``protoskipper.core.capture``;
    drivers MAY also accept user-defined sinks for custom telemetry pipes.
    """

    def write_frame(
        self,
        timestamp: datetime,
        direction: str,  # "tx" | "rx"
        payload: bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> None: ...
