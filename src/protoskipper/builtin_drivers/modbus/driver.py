# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Modbus TCP and Modbus RTU drivers.

Modbus is the simplest of the protocols ProtoSkipper targets and is
intentionally the first implementation: it shows how a driver maps onto the
:class:`ProtocolDriver` / :class:`DriverSession` contract end-to-end without
the additional complexity of reporting, GOOSE, or BACnet object discovery.

Address format
--------------

* TCP:  ``host:port/unit=N`` (port and unit optional; defaults 502 and 1)
* RTU:  ``device@baud,parity,stopbits/unit=N`` e.g.
        ``/dev/ttyUSB0@9600,E,1/unit=3``

Object id format
----------------

``<table>:<address>[:<count>]`` where ``table`` is one of
``coils``, ``discrete``, ``holding``, ``input`` and ``address`` is the
zero-based register number used by the protocol on the wire (NOT the
40001-style address). Count defaults to 1 register; for multi-register
encodings (float32, int64) callers should pass the right count.

The pymodbus dependency is imported lazily so the core package remains
importable on a minimal install.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from protoskipper.core.driver import (
    Access,
    DeviceRef,
    DriverSession,
    ObjectRef,
    ProtocolDriver,
    Quality,
    ReadResult,
    SafetyContext,
    WriteIntent,
    WriteResult,
)
from protoskipper.core.errors import (
    AuthorizationDenied,
    ConnectionFailure,
    EncodingError,
    UnsupportedOperation,
)

if TYPE_CHECKING:
    from pymodbus.client import ModbusBaseSyncClient

_logger = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 502
DEFAULT_UNIT = 1

_TABLES = {"coils", "discrete", "holding", "input"}


def _unit_kwarg(unit: int) -> dict:
    """Pymodbus 3.13 renamed ``slave=`` to ``device_id=``.

    Returns the kwarg dict so call sites work across both major API
    revisions without inspecting the running version.
    """
    # We just emit both; pymodbus checks one of them. This keeps the call
    # sites tidy at the cost of one harmless extra kwarg under old versions
    # which would TypeError - so we do a runtime selection at import time.
    return {"device_id": unit}


# Detect which kwarg the installed pymodbus uses. Done once at import.
def _detect_unit_kwarg() -> str:
    try:
        from pymodbus.client import ModbusTcpClient
        import inspect
        sig = inspect.signature(ModbusTcpClient.read_holding_registers)
        if "device_id" in sig.parameters:
            return "device_id"
        return "slave"
    except Exception:  # pragma: no cover - import safety
        return "device_id"


_UNIT_KW = _detect_unit_kwarg()


def _u(unit: int) -> dict:
    """Return the unit-id kwarg dict appropriate for the installed pymodbus."""
    return {_UNIT_KW: unit}


# ---------------------------------------------------------------------------
# Address parsing
# ---------------------------------------------------------------------------

_TCP_ADDRESS_RE = re.compile(
    r"""
    ^
    (?P<host>[^\s:/]+)         # hostname or IPv4 (no whitespace, no : or /)
    (?::(?P<port>\d+))?        # optional :port
    (?:/unit=(?P<unit>\d+))?   # optional /unit=N
    $
    """,
    re.VERBOSE,
)


def _parse_tcp_address(address: str) -> tuple[str, int, int]:
    m = _TCP_ADDRESS_RE.match(address.strip())
    if not m:
        raise EncodingError(f"Cannot parse Modbus TCP address: {address!r}")
    host = m.group("host")
    port = int(m.group("port") or DEFAULT_TCP_PORT)
    unit = int(m.group("unit") or DEFAULT_UNIT)
    return host, port, unit


def _parse_object_id(object_id: str) -> tuple[str, int, int]:
    parts = object_id.split(":")
    if len(parts) not in (2, 3):
        raise EncodingError(f"Modbus object_id must be 'table:address[:count]', got {object_id!r}")
    table = parts[0].lower()
    if table not in _TABLES:
        raise EncodingError(f"Unknown Modbus table {parts[0]!r}; expected one of {_TABLES}")
    try:
        address = int(parts[1], 0)
        count = int(parts[2], 0) if len(parts) == 3 else 1
    except ValueError as exc:
        raise EncodingError(f"Bad numeric component in object_id {object_id!r}: {exc}") from exc
    return table, address, count


# ---------------------------------------------------------------------------
# TCP driver
# ---------------------------------------------------------------------------


class ModbusTcpDriver(ProtocolDriver):
    """Modbus TCP master driver.

    Discovery sweeps a CIDR range probing TCP port 502 (or whatever port
    the user provides) and treats any device that answers a unit-id-1 read
    of holding register 0 as a candidate. This is intentionally simple;
    site-specific discovery (vendor identification, slave-id sweeps) belongs
    in higher-level scan profiles.
    """

    PROTOCOL_ID: ClassVar[str] = "modbus.tcp"
    DISPLAY_NAME: ClassVar[str] = "Modbus TCP"
    DESCRIPTION: ClassVar[str] = "Modbus over TCP/IP, RFC 6815"

    def discover(self, target: str) -> Iterator[DeviceRef]:
        """Yield candidate devices in ``target``.

        ``target`` is either a single ``host[:port]`` or a CIDR (``a.b.c.d/N``).
        For now the implementation only handles single hosts; CIDR sweeps
        will land alongside the GUI's progress reporting in a follow-up.
        """
        from pymodbus.client import ModbusTcpClient  # local import

        host, port, unit = _parse_tcp_address(target)
        client = ModbusTcpClient(host=host, port=port, timeout=2.0)
        try:
            if not client.connect():
                return
            response = client.read_holding_registers(address=0, count=1, **_u(unit))
            if response.isError():
                return
            yield DeviceRef(
                protocol=self.PROTOCOL_ID,
                address=f"{host}:{port}/unit={unit}",
                label=f"Modbus TCP @ {host}:{port}",
                metadata={"unit_id": unit},
            )
        finally:
            client.close()

    def connect(self, device: DeviceRef, safety: SafetyContext) -> DriverSession:
        from pymodbus.client import ModbusTcpClient  # local import

        host, port, unit = _parse_tcp_address(device.address)
        client = ModbusTcpClient(host=host, port=port, timeout=3.0)
        if not client.connect():
            raise ConnectionFailure(f"Could not open Modbus TCP socket to {host}:{port}")
        return _ModbusSession(client=client, unit=unit, device=device, safety=safety)


# ---------------------------------------------------------------------------
# RTU driver (skeleton; serial transport plumbing only)
# ---------------------------------------------------------------------------


class ModbusRtuDriver(ProtocolDriver):
    """Modbus RTU master driver.

    The RTU implementation reuses :class:`_ModbusSession` for read/write
    semantics; only the transport differs. Full parity, framing, and
    timing-tuned configuration land alongside the multi-drop bus features
    in Phase 1.
    """

    PROTOCOL_ID: ClassVar[str] = "modbus.rtu"
    DISPLAY_NAME: ClassVar[str] = "Modbus RTU"
    DESCRIPTION: ClassVar[str] = "Modbus over serial RS-485 / RS-232"

    def discover(self, target: str) -> Iterator[DeviceRef]:
        # Serial discovery is a unit-id sweep on a known port; deferred until
        # the bus-arbitration code lands. For now manual address entry is the
        # primary path.
        if False:  # pragma: no cover - placeholder
            yield  # type: ignore[misc]
        return
        yield  # pragma: no cover

    def connect(self, device: DeviceRef, safety: SafetyContext) -> DriverSession:
        raise UnsupportedOperation(
            "Modbus RTU connect is not yet implemented in the Phase-1 scaffold; "
            "use Modbus TCP or wire up the serial transport in a follow-up."
        )


# ---------------------------------------------------------------------------
# Shared session implementation
# ---------------------------------------------------------------------------


class _ModbusSession(DriverSession):
    """Concrete :class:`DriverSession` for Modbus TCP (and, soon, RTU).

    Holds a pymodbus client and translates between the protocol-agnostic
    :class:`ObjectRef` / :class:`ReadResult` / :class:`WriteIntent` types
    and pymodbus's function-code-specific calls.
    """

    def __init__(
        self,
        *,
        client: ModbusBaseSyncClient,
        unit: int,
        device: DeviceRef,
        safety: SafetyContext,
    ) -> None:
        self._client = client
        self._unit = unit
        self.device = device
        self.safety = safety

    # -- enumerate --------------------------------------------------------
    def enumerate_objects(self) -> Iterator[ObjectRef]:
        """Yield a synthetic, configurable register map.

        Modbus has no self-description, so without a register map file we
        cannot enumerate truthfully. This default yields a small sample so
        the GUI's tree is not empty; production usage replaces it with a
        register-map import (CSV / Manufacturer XML) wired in later.
        """
        sample: list[tuple[str, str, Access, str | None]] = [
            ("holding:0", "uint16", Access.READ_WRITE, None),
            ("holding:1", "uint16", Access.READ_WRITE, None),
            ("input:0", "uint16", Access.READ_ONLY, None),
            ("coils:0", "boolean", Access.READ_WRITE, None),
        ]
        for object_id, dtype, access, unit in sample:
            yield ObjectRef(
                device=self.device,
                object_id=object_id,
                data_type=dtype,
                access=access,
                unit=unit,
                label=object_id,
            )

    # -- read -------------------------------------------------------------
    def read(self, ref: ObjectRef) -> ReadResult:
        table, address, count = _parse_object_id(ref.object_id)
        ts = datetime.now(timezone.utc)

        try:
            unit = _u(self._unit)
            if table == "holding":
                resp = self._client.read_holding_registers(address=address, count=count, **unit)
            elif table == "input":
                resp = self._client.read_input_registers(address=address, count=count, **unit)
            elif table == "coils":
                resp = self._client.read_coils(address=address, count=count, **unit)
            elif table == "discrete":
                resp = self._client.read_discrete_inputs(address=address, count=count, **unit)
            else:  # pragma: no cover - guarded above
                raise EncodingError(f"Unhandled table {table!r}")
        except Exception as exc:
            _logger.warning("Modbus read failed for %s: %s", ref.object_id, exc)
            return ReadResult(
                object_ref=ref, value=None, quality=Quality.BAD,
                timestamp=ts, error=repr(exc),
            )

        if resp.isError():
            return ReadResult(
                object_ref=ref, value=None, quality=Quality.BAD,
                timestamp=ts, error=str(resp),
            )

        value: Any
        if table in {"coils", "discrete"}:
            value = list(resp.bits)[:count]
            value = value[0] if count == 1 else value
        else:
            value = list(resp.registers)
            value = value[0] if count == 1 else value

        return ReadResult(
            object_ref=ref, value=value, quality=Quality.GOOD,
            timestamp=ts, raw_bytes=None,
        )

    # -- write ------------------------------------------------------------
    def prepare_write(self, ref: ObjectRef, value: Any) -> WriteIntent:
        if ref.access == Access.READ_ONLY:
            raise EncodingError(f"{ref.object_id} is read-only on this device")

        table, address, _count = _parse_object_id(ref.object_id)
        if table not in {"holding", "coils"}:
            raise EncodingError(
                f"Modbus writes only target holding registers or coils; got {table!r}"
            )

        try:
            if table == "holding":
                int_value = int(value) & 0xFFFF
                encoded = int_value.to_bytes(2, "big")
                description = f"Write holding[{address}] := {int_value} (0x{int_value:04x})"
            else:  # coils
                bool_value = bool(value)
                encoded = b"\xff\x00" if bool_value else b"\x00\x00"
                description = f"Write coil[{address}] := {bool_value}"
        except (TypeError, ValueError) as exc:
            raise EncodingError(f"Cannot encode {value!r} for {ref.object_id}: {exc}") from exc

        return WriteIntent(
            object_ref=ref, requested_value=value,
            encoded_bytes=encoded, description=description,
            metadata={"unit_id": self._unit, "table": table, "address": address},
        )

    def commit_write(self, intent: WriteIntent) -> WriteResult:
        if not self.safety.require_write_authorization(intent):
            raise AuthorizationDenied(
                f"Write to {intent.object_ref.object_id} denied by SafetyContext"
            )

        table = intent.metadata["table"]
        address = intent.metadata["address"]
        ts = datetime.now(timezone.utc)

        try:
            unit = _u(self._unit)
            if table == "holding":
                int_value = int.from_bytes(intent.encoded_bytes, "big")
                resp = self._client.write_register(address=address, value=int_value, **unit)
            else:  # coils
                bool_value = intent.encoded_bytes == b"\xff\x00"
                resp = self._client.write_coil(address=address, value=bool_value, **unit)
        except Exception as exc:
            return WriteResult(intent=intent, success=False, timestamp=ts, error=repr(exc))

        if resp.isError():
            return WriteResult(intent=intent, success=False, timestamp=ts, error=str(resp))

        # Audit the committed write through the safety context's audit hook.
        # SafetyContext does not currently expose this; we use the same callback
        # path by re-using require_write_authorization's audit_callback. For
        # now, drivers can also surface this via the Session-level audit.
        return WriteResult(intent=intent, success=True, timestamp=ts)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - defensive
            _logger.exception("Error closing Modbus client; ignoring")


# ---------------------------------------------------------------------------
# Helpers exposed for tests
# ---------------------------------------------------------------------------


def _replace_for_test(ref: ObjectRef, **changes: Any) -> ObjectRef:
    """Test-only helper to create a tweaked ObjectRef without touching __post_init__."""
    return replace(ref, **changes)
