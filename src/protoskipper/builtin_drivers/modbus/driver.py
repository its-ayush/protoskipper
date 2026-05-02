# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Modbus TCP and Modbus RTU drivers.

Modbus is the simplest of the protocols ProtoSkipper targets and is
intentionally the first implementation: it shows how a driver maps onto the
:class:`ProtocolDriver` / :class:`DriverSession` contract end-to-end without
the additional complexity of reporting, GOOSE, or BACnet object discovery.

Address format
--------------

* TCP single host:  ``host[:port][/unit=N]``                   ``10.0.0.5:502/unit=1``
* TCP CIDR sweep:   ``CIDR[:port][/units=lo[-hi]]``            ``10.0.0.0/24/units=1-10``
* TCP host list:    ``host1[,host2,...][:port][/units=...]``   ``10.0.0.5,10.0.0.7/units=1-247``
* RTU single unit:  ``port@baud[,parity[,stop]][/unit=N]``     ``/dev/ttyUSB0@9600,N,1/unit=3``
* RTU bus sweep:    ``port@baud[,...]][/units=lo[-hi]]``       ``/dev/ttyUSB0@9600,N,1/units=1-32``

If unit / units are omitted, ``connect`` defaults to unit 1; ``discover``
sweeps a sensible default range (TCP: just unit 1; RTU: 1-247).

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

import ipaddress
import logging
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from protoskipper.builtin_drivers.modbus.codec import (
    REGISTER_COUNTS,
    decode_bit,
    decode_registers,
    encode_bit,
    encode_value,
)
from protoskipper.core.driver import (
    Access,
    CaptureSink,
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
)

if TYPE_CHECKING:
    from pymodbus.client import ModbusBaseSyncClient

_logger = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 502
DEFAULT_UNIT = 1
DEFAULT_PROBE_TIMEOUT_S = 1.0
DEFAULT_PROBE_WORKERS = 64

_TABLES = {"coils", "discrete", "holding", "input"}


# ---------------------------------------------------------------------------
# pymodbus 3.7+ compatibility shim: slave= -> device_id=
# ---------------------------------------------------------------------------


def _detect_unit_kwarg() -> str:
    try:
        import inspect

        from pymodbus.client import ModbusTcpClient

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
# Address / object_id parsing
# ---------------------------------------------------------------------------

_TCP_SINGLE_RE = re.compile(
    r"""
    ^
    (?P<host>[^\s:/,]+)            # hostname or IPv4 (no whitespace, no : / ,)
    (?::(?P<port>\d+))?            # optional :port
    (?:/unit=(?P<unit>\d+))?       # optional /unit=N
    $
    """,
    re.VERBOSE,
)


def _parse_tcp_address(address: str) -> tuple[str, int, int]:
    """Parse a single Modbus TCP address (used by ``connect``).

    For the looser probing target syntax (CIDR, comma list, units range),
    use :func:`parse_probe_target`.
    """
    m = _TCP_SINGLE_RE.match(address.strip())
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
# Probe target parsing (CIDR, comma list, unit range)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeTarget:
    """Parsed probe specification: which hosts to probe and which units to try."""

    hosts: tuple[tuple[str, int], ...]  # ((host, port), ...)
    units: tuple[int, ...]


_UNITS_RE = re.compile(r"^/units=(?P<lo>\d+)(?:-(?P<hi>\d+))?$")
_UNIT_RE = re.compile(r"^/unit=(?P<unit>\d+)$")
_PORT_RE = re.compile(r"^:(?P<port>\d+)$")


def parse_probe_target(spec: str) -> ProbeTarget:
    """Parse a probing spec into a list of (host, port) and units to try.

    Accepts any of::

        10.0.0.5
        10.0.0.5:502
        10.0.0.5/unit=3
        10.0.0.5:502/unit=3
        10.0.0.0/24
        10.0.0.0/24/units=1-10
        10.0.0.5,10.0.0.7,10.0.0.9
        10.0.0.5,10.0.0.7:502/units=1-247

    Raises :class:`EncodingError` on garbage. The returned :class:`ProbeTarget`
    can be substantial (e.g. ``10.0.0.0/16/units=1-247`` is ~16M probe
    points); it is the caller's responsibility to be reasonable.
    """
    spec = spec.strip()
    if not spec:
        raise EncodingError("Empty probe target")

    # Split off optional /unit= or /units= suffix. We have to be careful:
    # CIDR uses /N, so we look only for /unit= or /units=.
    units: tuple[int, ...]
    m = re.search(r"/units=\d+(?:-\d+)?$", spec)
    if m:
        units = _parse_units_range("/units=" + m.group()[len("/units=") :])
        spec = spec[: m.start()]
    elif re.search(r"/unit=\d+$", spec):
        m2 = re.search(r"/unit=\d+$", spec)
        units = (int(m2.group()[len("/unit=") :]),)
        spec = spec[: m2.start()]
    else:
        units = (DEFAULT_UNIT,)

    # Now spec is host[s][:port] or CIDR[:port].
    # Split off optional :port.
    port = DEFAULT_TCP_PORT
    portm = re.search(r":(\d+)$", spec)
    if portm:
        # Make sure this isn't part of a hostname like "host:8000" inside
        # a comma list - we only honour a trailing :port. Already enforced
        # by the $ anchor.
        port = int(portm.group(1))
        spec = spec[: portm.start()]

    hosts: list[tuple[str, int]] = []
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        # CIDR?
        if "/" in piece:
            try:
                net = ipaddress.ip_network(piece, strict=False)
            except ValueError as exc:
                raise EncodingError(f"Bad CIDR: {piece!r}: {exc}") from exc
            if net.num_addresses > 65536:
                raise EncodingError(
                    f"Refusing to probe more than 65536 hosts ({net} -> {net.num_addresses}); "
                    f"narrow the range or split the scan."
                )
            for ip in net.hosts() if net.num_addresses > 1 else [net.network_address]:
                hosts.append((str(ip), port))
        else:
            # Bare host or IP.
            hosts.append((piece, port))

    if not hosts:
        raise EncodingError(f"No hosts parsed from: {spec!r}")

    # De-duplicate (host, port) entries while preserving first-seen order.
    # Without this, ``CIDR,explicit-host`` combinations probe overlapping
    # addresses twice and yield duplicate DeviceRef rows.
    seen: set[tuple[str, int]] = set()
    deduped: list[tuple[str, int]] = []
    for hp in hosts:
        if hp in seen:
            continue
        seen.add(hp)
        deduped.append(hp)

    return ProbeTarget(hosts=tuple(deduped), units=units)


def _parse_units_range(suffix: str) -> tuple[int, ...]:
    """``"/units=1-10"`` -> ``(1, 2, ..., 10)``."""
    m = _UNITS_RE.match(suffix)
    if not m:
        raise EncodingError(f"Bad units suffix: {suffix!r}")
    lo = int(m.group("lo"))
    hi = int(m.group("hi") or lo)
    if not (1 <= lo <= 247) or not (1 <= hi <= 247) or lo > hi:
        raise EncodingError(f"Unit range out of bounds: {lo}-{hi} (must be 1-247, lo<=hi)")
    return tuple(range(lo, hi + 1))


# ---------------------------------------------------------------------------
# Identification helpers (best-effort - never fatal to a probe)
# ---------------------------------------------------------------------------


def _identify(client: Any, unit: int) -> dict:
    """Try common Modbus identification calls. Always returns a dict.

    We try, in order:

    1. **FC 43 / MEI 14** (Read Device Identification) - returns
       VendorName, ProductCode, MajorMinorRevision when supported.
    2. **FC 17** (Report Server ID) - vendor-specific blob; we record
       its presence and length as a fallback signal.

    Anything that raises is swallowed; identification is bonus, not
    correctness.
    """
    metadata: dict = {"unit_id": unit}

    # FC 43 / MEI 14 - basic device info.
    try:
        # pymodbus 3.x exposes either read_device_information() (3.13+) or
        # builds the request manually. We try the convenience method first.
        if hasattr(client, "read_device_information"):
            resp = client.read_device_information(**_u(unit))
            if not getattr(resp, "isError", lambda: True)():
                info = getattr(resp, "information", None) or {}
                # info keys: 0=vendor_name, 1=product_code, 2=revision (bytes)
                if 0 in info:
                    metadata["vendor_name"] = _bytes_to_str(info[0])
                if 1 in info:
                    metadata["product_code"] = _bytes_to_str(info[1])
                if 2 in info:
                    metadata["revision"] = _bytes_to_str(info[2])
    except Exception:  # pragma: no cover - vendor specific
        pass

    # FC 17 - report server / slave id.
    try:
        if hasattr(client, "report_slave_id"):
            resp = client.report_slave_id(**_u(unit))
            if not getattr(resp, "isError", lambda: True)():
                identifier = getattr(resp, "identifier", b"") or b""
                if identifier:
                    metadata.setdefault("slave_id_blob_hex", identifier.hex())
                metadata.setdefault("slave_id_status", bool(getattr(resp, "status", False)))
    except Exception:  # pragma: no cover - vendor specific
        pass

    return metadata


def _bytes_to_str(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("ascii", errors="replace").strip()
        except Exception:
            return value.hex()
    return str(value)


# ---------------------------------------------------------------------------
# TCP driver
# ---------------------------------------------------------------------------


class ModbusTcpDriver(ProtocolDriver):
    """Modbus TCP master driver.

    Probing supports single hosts, CIDR ranges, comma-separated host
    lists, and per-host slave-ID sweeps. Probes run concurrently in a
    thread pool; results stream out as soon as each host responds. The
    pool size and per-host timeout are tunable via constructor args
    (defaults: 64 workers, 1.0 s).
    """

    PROTOCOL_ID: ClassVar[str] = "modbus.tcp"
    DISPLAY_NAME: ClassVar[str] = "Modbus TCP"
    DESCRIPTION: ClassVar[str] = "Modbus over TCP/IP, RFC 6815"

    def __init__(
        self,
        probe_workers: int = DEFAULT_PROBE_WORKERS,
        probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
    ) -> None:
        self._probe_workers = probe_workers
        self._probe_timeout = probe_timeout_s

    def discover(self, target: str) -> Iterator[DeviceRef]:
        """Yield discovered devices from ``target``.

        ``target`` accepts the full probe-target syntax documented in this
        module's docstring; for a single host with one unit, it is
        backwards compatible with the simple ``host:port/unit=N`` form.
        """
        probe = parse_probe_target(target)
        _logger.info(
            "Modbus TCP probe: %d host(s), %d unit(s) per host (%d total points)",
            len(probe.hosts),
            len(probe.units),
            len(probe.hosts) * len(probe.units),
        )

        max_workers = min(self._probe_workers, max(1, len(probe.hosts)))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [
                ex.submit(_probe_tcp_host, host, port, probe.units, self._probe_timeout)
                for host, port in probe.hosts
            ]
            for fut in as_completed(futures):
                try:
                    devices = fut.result()
                except Exception as exc:  # pragma: no cover - defensive
                    _logger.debug("probe future failed: %s", exc)
                    continue
                yield from devices

    def connect(self, device: DeviceRef, safety: SafetyContext) -> DriverSession:
        host, port, unit = _parse_tcp_address(device.address)
        client = _make_capturing_tcp_client(host=host, port=port, timeout=3.0)
        if not client.connect():  # type: ignore[union-attr]
            raise ConnectionFailure(f"Could not open Modbus TCP socket to {host}:{port}")
        return _ModbusSession(client=client, unit=unit, device=device, safety=safety)


def _probe_tcp_host(
    host: str,
    port: int,
    units: tuple[int, ...],
    timeout: float,
) -> list[DeviceRef]:
    """Probe one host for one or more units; return whatever responds.

    Lives at module scope (not inside ``ModbusTcpDriver``) so it works
    cleanly with :class:`ThreadPoolExecutor` and is unit-testable.
    """
    from pymodbus.client import ModbusTcpClient

    found: list[DeviceRef] = []
    client: Any = None
    try:
        client = ModbusTcpClient(host=host, port=port, timeout=timeout)
        if not client.connect():
            return found
        for unit in units:
            try:
                resp = client.read_holding_registers(address=0, count=1, **_u(unit))
            except Exception:
                continue
            if getattr(resp, "isError", lambda: True)():
                continue
            metadata = _identify(client, unit)
            label = metadata.get("vendor_name") or f"Modbus TCP @ {host}:{port}/unit={unit}"
            found.append(
                DeviceRef(
                    protocol=ModbusTcpDriver.PROTOCOL_ID,
                    address=f"{host}:{port}/unit={unit}",
                    label=label,
                    metadata=metadata,
                )
            )
    except Exception as exc:
        _logger.debug("Modbus TCP probe %s:%d failed: %s", host, port, exc)
    finally:
        if client is not None:
            import contextlib

            with contextlib.suppress(Exception):  # pragma: no cover
                client.close()
    return found


# ---------------------------------------------------------------------------
# RTU driver - real implementation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RtuConfig:
    """Parsed Modbus RTU serial configuration."""

    port: str
    baudrate: int = 9600
    parity: str = "N"  # N / E / O
    stopbits: int = 1  # 1 / 2
    bytesize: int = 8
    unit: int = DEFAULT_UNIT
    units_range: tuple[int, ...] = ()  # populated for /units=lo-hi probe specs


_RTU_UNIT_RE = re.compile(r"/unit=(\d+)$")
_RTU_UNITS_RE = re.compile(r"/units=(\d+)(?:-(\d+))?$")


def parse_rtu_address(address: str) -> RtuConfig:
    """Parse an RTU address string into :class:`RtuConfig`.

    Examples::

        "/dev/ttyUSB0"                            # defaults: 9600,N,1, unit=1
        "/dev/ttyUSB0@9600,N,1/unit=3"
        "COM4@19200,E,1/units=1-32"

    The port may itself contain ``/`` (e.g. ``/dev/ttyUSB0``), so we strip
    the optional ``/unit=`` / ``/units=`` suffix manually rather than with
    a single anchored regex.
    """
    address = address.strip()
    if not address:
        raise EncodingError("Empty Modbus RTU address")

    # Strip optional /unit= or /units= suffix.
    unit = DEFAULT_UNIT
    units_range: tuple[int, ...] = ()

    m_unit = _RTU_UNIT_RE.search(address)
    m_units = _RTU_UNITS_RE.search(address)
    if m_unit:
        unit = int(m_unit.group(1))
        if not (1 <= unit <= 247):
            raise EncodingError(f"RTU unit out of bounds: {unit} (must be 1-247)")
        address = address[: m_unit.start()]
    elif m_units:
        lo = int(m_units.group(1))
        hi = int(m_units.group(2) or lo)
        if not (1 <= lo <= 247) or not (1 <= hi <= 247) or lo > hi:
            raise EncodingError(f"RTU unit range out of bounds: {lo}-{hi} (must be 1-247, lo<=hi)")
        unit = lo
        units_range = tuple(range(lo, hi + 1))
        address = address[: m_units.start()]

    # Now address is either ``port`` or ``port@params``.
    baud = 9600
    parity = "N"
    stopbits = 1
    if "@" in address:
        port, _, params = address.partition("@")
        if not params:
            raise EncodingError(f"Empty serial parameters in: {address!r}")
        parts = params.split(",")
        try:
            baud = int(parts[0])
        except ValueError as exc:
            raise EncodingError(f"Bad baud rate {parts[0]!r}: {exc}") from exc
        if baud <= 0:
            raise EncodingError(f"Baud rate must be positive: {baud}")
        if len(parts) >= 2 and parts[1]:
            parity = parts[1].upper()
            if parity not in {"N", "E", "O"}:
                raise EncodingError(f"Bad parity {parity!r} (expected N, E, or O)")
        if len(parts) >= 3 and parts[2]:
            try:
                stopbits = int(parts[2])
            except ValueError as exc:
                raise EncodingError(f"Bad stopbits {parts[2]!r}: {exc}") from exc
            if stopbits not in {1, 2}:
                raise EncodingError(f"Stopbits must be 1 or 2, got {stopbits}")
    else:
        port = address

    if not port:
        raise EncodingError("Empty port path")

    return RtuConfig(
        port=port,
        baudrate=baud,
        parity=parity,
        stopbits=stopbits,
        bytesize=8,
        unit=unit,
        units_range=units_range,
    )


class ModbusRtuDriver(ProtocolDriver):
    """Modbus RTU master driver.

    Discovery sweeps the configured slave-id range (default 1-247) on a
    serial bus; each unit that answers a holding-register read is yielded
    as a separate :class:`DeviceRef`. RTU buses are inherently
    single-threaded - we open the port once and probe units sequentially
    with a per-call timeout tuned for typical RS-485 lines.
    """

    PROTOCOL_ID: ClassVar[str] = "modbus.rtu"
    DISPLAY_NAME: ClassVar[str] = "Modbus RTU"
    DESCRIPTION: ClassVar[str] = "Modbus over serial RS-485 / RS-232"

    def __init__(self, probe_timeout_s: float = 0.3) -> None:
        self._probe_timeout = probe_timeout_s

    def discover(self, target: str) -> Iterator[DeviceRef]:
        from pymodbus.client import ModbusSerialClient

        cfg = parse_rtu_address(target)
        units = cfg.units_range or tuple(range(1, 248))
        _logger.info(
            "Modbus RTU probe: %s @ %d,%s,%d - sweeping units %d..%d",
            cfg.port,
            cfg.baudrate,
            cfg.parity,
            cfg.stopbits,
            min(units),
            max(units),
        )

        client = ModbusSerialClient(
            port=cfg.port,
            baudrate=cfg.baudrate,
            parity=cfg.parity,
            stopbits=cfg.stopbits,
            bytesize=cfg.bytesize,
            timeout=self._probe_timeout,
        )
        if not client.connect():
            _logger.warning("RTU probe could not open serial port %s", cfg.port)
            return
        try:
            for unit in units:
                try:
                    resp = client.read_holding_registers(
                        address=0,
                        count=1,
                        **_u(unit),
                    )
                except Exception:
                    continue
                if getattr(resp, "isError", lambda: True)():
                    continue
                metadata = _identify(client, unit)
                label = metadata.get("vendor_name") or f"Modbus RTU @ {cfg.port} unit={unit}"
                yield DeviceRef(
                    protocol=self.PROTOCOL_ID,
                    address=(f"{cfg.port}@{cfg.baudrate},{cfg.parity},{cfg.stopbits}/unit={unit}"),
                    label=label,
                    metadata=metadata,
                )
        finally:
            import contextlib

            with contextlib.suppress(Exception):  # pragma: no cover
                client.close()

    def connect(self, device: DeviceRef, safety: SafetyContext) -> DriverSession:
        cfg = parse_rtu_address(device.address)
        client = _make_capturing_serial_client(
            port=cfg.port,
            baudrate=cfg.baudrate,
            parity=cfg.parity,
            stopbits=cfg.stopbits,
            bytesize=cfg.bytesize,
            timeout=1.0,
        )
        if not client.connect():  # type: ignore[union-attr]
            raise ConnectionFailure(f"Could not open serial port {cfg.port}")
        return _ModbusSession(
            client=client,
            unit=cfg.unit,
            device=device,
            safety=safety,
        )

    def parse_address(self, address: str) -> DeviceRef:
        # Keep validation early so the New Connection dialog rejects bad
        # input before it ever creates a session.
        parse_rtu_address(address)
        return super().parse_address(address)


# ---------------------------------------------------------------------------
# Frame-capturing transport wrappers
# ---------------------------------------------------------------------------


class _CapturingMixin:
    """Mixin that intercepts ``send`` / ``recv`` to feed a :class:`CaptureSink`.

    Designed for cooperative multiple inheritance with pymodbus sync clients
    (``ModbusTcpClient``, ``ModbusSerialClient``). The mixin stores a sink
    reference and calls ``sink.write_frame`` with raw bytes on every I/O.
    Frames are silently dropped when ``_capture_sink`` is ``None``.

    **Thread safety**: ``_capture_sink`` is read in the worker thread and
    set from the worker thread immediately after ``connect()``; there is no
    concurrent access in normal operation. We intentionally avoid locking to
    keep the hot path allocation-free.
    """

    _capture_sink: CaptureSink | None = None  # type: ignore[assignment]

    def send(self, request: object, addr: tuple | None = None) -> int:  # type: ignore[override]
        if self._capture_sink is not None and request:
            try:
                self._capture_sink.write_frame(
                    datetime.now(timezone.utc),
                    "tx",
                    bytes(request),  # type: ignore[call-overload]
                )
            except Exception:  # pragma: no cover - sink errors must never kill I/O
                _logger.debug("CaptureSink.write_frame (tx) raised", exc_info=True)
        return super().send(request, addr)  # type: ignore[misc]

    def recv(self, size: int | None) -> bytes:  # type: ignore[override]
        data: bytes = super().recv(size)  # type: ignore[misc]
        if self._capture_sink is not None and data:
            try:
                self._capture_sink.write_frame(
                    datetime.now(timezone.utc),
                    "rx",
                    data,
                )
            except Exception:  # pragma: no cover - sink errors must never kill I/O
                _logger.debug("CaptureSink.write_frame (rx) raised", exc_info=True)
        return data


class _CapturingTcpClient(_CapturingMixin):
    """ModbusTcpClient subclass that captures raw TX/RX bytes.

    The lazy import is deferred to first use so the core package stays
    importable on a minimal install (pymodbus is optional). The class is
    built once and cached so the import overhead is paid once.
    """


class _CapturingSerialClient(_CapturingMixin):
    """ModbusSerialClient subclass that captures raw TX/RX bytes."""


def _make_capturing_tcp_client(host: str, port: int, timeout: float) -> _CapturingMixin:
    """Return a capturing TCP client (lazy-imports ModbusTcpClient)."""
    from pymodbus.client import ModbusTcpClient

    cls = type(
        "_CapturingTcpClientImpl",
        (_CapturingMixin, ModbusTcpClient),
        {},
    )
    return cls(host=host, port=port, timeout=timeout)  # type: ignore[call-arg]


def _make_capturing_serial_client(
    port: str,
    baudrate: int,
    parity: str,
    stopbits: int,
    bytesize: int,
    timeout: float,
) -> _CapturingMixin:
    """Return a capturing serial client (lazy-imports ModbusSerialClient)."""
    from pymodbus.client import ModbusSerialClient

    cls = type(
        "_CapturingSerialClientImpl",
        (_CapturingMixin, ModbusSerialClient),
        {},
    )
    return cls(  # type: ignore[call-arg]
        port=port,
        baudrate=baudrate,
        parity=parity,
        stopbits=stopbits,
        bytesize=bytesize,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Shared session implementation (used by both TCP and RTU)
# ---------------------------------------------------------------------------


class _ModbusSession(DriverSession):
    """Concrete :class:`DriverSession` for Modbus TCP and RTU.

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

    def attach_frame_sink(self, sink: CaptureSink | None) -> None:
        """Override: store sink and wire it into the capturing transport."""
        self._frame_sink = sink
        # _CapturingMixin exposes _capture_sink; plain clients just ignore this.
        if isinstance(self._client, _CapturingMixin):
            self._client._capture_sink = sink

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
            ("holding:5", "uint16", Access.READ_WRITE, None),
            ("input:0", "uint16", Access.READ_ONLY, None),
            ("input:1", "uint16", Access.READ_ONLY, None),
            ("coils:0", "boolean", Access.READ_WRITE, None),
            ("coils:1", "boolean", Access.READ_WRITE, None),
            ("discrete:0", "boolean", Access.READ_ONLY, None),
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
        table, address, parsed_count = _parse_object_id(ref.object_id)
        ts = datetime.now(timezone.utc)

        # For fixed-width multi-register types, use the codec's register
        # count instead of the object_id count so callers don't need to
        # embed the count in every object_id.
        dtype = ref.data_type
        codec_count = REGISTER_COUNTS.get(dtype, 0)
        count = codec_count if codec_count > 0 else parsed_count

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
                object_ref=ref,
                value=None,
                quality=Quality.BAD,
                timestamp=ts,
                error=repr(exc),
            )

        if resp.isError():
            return ReadResult(
                object_ref=ref,
                value=None,
                quality=Quality.BAD,
                timestamp=ts,
                error=str(resp),
            )

        value: Any
        if table in {"coils", "discrete"}:
            value = list(resp.bits)[:count]
            value = value[0] if count == 1 else value
        else:
            raw_regs = list(resp.registers)
            bit_index = ref.metadata.get("bit")
            if bit_index is not None:
                # Bitfield: extract a single bit from the holding register.
                value = decode_bit(raw_regs[0], int(bit_index))
            elif dtype in REGISTER_COUNTS and REGISTER_COUNTS[dtype] > 1:
                # Decode multi-register types through the codec.
                byte_order = str(ref.metadata.get("byte_order", "big"))
                word_order = str(ref.metadata.get("word_order", "big"))
                try:
                    value = decode_registers(raw_regs, dtype, byte_order, word_order)
                except Exception as exc:
                    _logger.warning(
                        "Codec decode failed for %s (dtype=%s): %s",
                        ref.object_id,
                        dtype,
                        exc,
                    )
                    return ReadResult(
                        object_ref=ref,
                        value=None,
                        quality=Quality.BAD,
                        timestamp=ts,
                        error=repr(exc),
                    )
            else:
                value = raw_regs[0] if len(raw_regs) == 1 else raw_regs

        return ReadResult(
            object_ref=ref,
            value=value,
            quality=Quality.GOOD,
            timestamp=ts,
            raw_bytes=None,
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

        dtype = ref.data_type
        byte_order = str(ref.metadata.get("byte_order", "big"))
        word_order = str(ref.metadata.get("word_order", "big"))
        bit_index = ref.metadata.get("bit")

        try:
            if table == "holding":
                codec_count = REGISTER_COUNTS.get(dtype, 0)
                if bit_index is not None:
                    # Bitfield: we need a read-modify-write; store the bit info.
                    bool_value = bool(value)
                    encoded = b"\xff\x00" if bool_value else b"\x00\x00"
                    description = (
                        f"Write holding[{address}] bit {bit_index} := {bool_value}"
                        " (read-modify-write)"
                    )
                    extra_meta: dict = {"bit": int(bit_index), "rmw": True}
                elif codec_count > 1:
                    # Multi-register type: encode through the codec.
                    registers = encode_value(float(value), dtype, byte_order, word_order)
                    encoded = b"".join(r.to_bytes(2, "big") for r in registers)
                    description = (
                        f"Write holding[{address}:{address + codec_count - 1}]"
                        f" := {value!r} ({dtype})"
                    )
                    extra_meta: dict = {"registers": registers}
                else:
                    # Single-register (uint16 / int16 / boolean stored in holding).
                    int_value = int(value) & 0xFFFF
                    encoded = int_value.to_bytes(2, "big")
                    description = f"Write holding[{address}] := {int_value} (0x{int_value:04x})"
                    extra_meta = {}
            else:  # coils
                bool_value = bool(value)
                encoded = b"\xff\x00" if bool_value else b"\x00\x00"
                description = f"Write coil[{address}] := {bool_value}"
                extra_meta = {}
        except (TypeError, ValueError) as exc:
            raise EncodingError(f"Cannot encode {value!r} for {ref.object_id}: {exc}") from exc

        return WriteIntent(
            object_ref=ref,
            requested_value=value,
            encoded_bytes=encoded,
            description=description,
            metadata={
                "unit_id": self._unit,
                "table": table,
                "address": address,
                **extra_meta,
            },
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
                registers: list[int] | None = intent.metadata.get("registers")
                bit: int | None = intent.metadata.get("bit")
                if bit is not None:
                    # Read-modify-write for a bitfield object.
                    read_resp = self._client.read_holding_registers(
                        address=address, count=1, **unit
                    )
                    if read_resp.isError():
                        raise RuntimeError(
                            f"Read-modify-write: pre-read of holding[{address}] failed: {read_resp}"
                        )
                    current = next(iter(read_resp.registers))
                    bool_value = intent.encoded_bytes == b"\xff\x00"
                    new_register = encode_bit(current, bit, bool_value)
                    resp = self._client.write_register(address=address, value=new_register, **unit)
                elif registers is not None and len(registers) > 1:
                    # Multi-register write (float32, uint32, int32, …)
                    resp = self._client.write_registers(address=address, values=registers, **unit)
                else:
                    int_value = int.from_bytes(intent.encoded_bytes, "big")
                    resp = self._client.write_register(address=address, value=int_value, **unit)
            else:  # coils
                bool_value = intent.encoded_bytes == b"\xff\x00"
                resp = self._client.write_coil(address=address, value=bool_value, **unit)
        except Exception as exc:
            result = WriteResult(intent=intent, success=False, timestamp=ts, error=repr(exc))
            self.safety.record_write_outcome(result)
            return result

        if resp.isError():
            result = WriteResult(intent=intent, success=False, timestamp=ts, error=str(resp))
            self.safety.record_write_outcome(result)
            return result

        result = WriteResult(intent=intent, success=True, timestamp=ts)
        self.safety.record_write_outcome(result)
        return result

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
