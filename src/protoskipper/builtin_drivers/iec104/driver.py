# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 60870-5-104 master driver — :class:`ProtocolDriver` integration.

Address format
--------------

``host[:port][/ca=N][/oa=N]``  e.g. ``10.0.0.5:2404/ca=1``

Object id format
----------------

``<type-mnemonic>:<ioa>``  e.g. ``M_ME_NC_1:4001``

The point list (loaded via :func:`Iec104TcpDriver.load_point_list_from_path`)
attaches labels and engineering units; without one, the driver still works
with raw IOAs but ``enumerate_objects`` returns nothing (the GUI watchlist
still allows manual entry).
"""

from __future__ import annotations

import logging
import re
import socket
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from protoskipper.builtin_drivers.iec104.asdu import (
    COT,
    Asdu,
    TypeID,
)
from protoskipper.builtin_drivers.iec104.master import Iec104MasterSession, MasterConfig
from protoskipper.builtin_drivers.iec104.pointlist import PointDef, load_point_list
from protoskipper.core.driver import (
    CaptureSink,
    DeviceRef,
    DriverSession,
    ObjectRef,
    ProtocolDriver,
    ReadResult,
    SafetyContext,
    WriteIntent,
    WriteResult,
)
from protoskipper.core.driver import (
    Quality as CoreQuality,
)
from protoskipper.core.errors import (
    AuthorizationDenied,
    ConnectionFailure,
    EncodingError,
    UnsupportedOperation,
)

_logger = logging.getLogger(__name__)

DEFAULT_PORT = 2404


_ADDR_RE = re.compile(
    r"""
    ^
    (?P<host>[^\s:/,]+)
    (?::(?P<port>\d+))?
    (?:/ca=(?P<ca>\d+))?
    (?:/oa=(?P<oa>\d+))?
    $
    """,
    re.VERBOSE,
)


def parse_address(address: str) -> tuple[str, int, int, int]:
    m = _ADDR_RE.match(address.strip())
    if not m:
        raise EncodingError(f"Cannot parse IEC 104 address: {address!r}")
    host = m.group("host")
    port = int(m.group("port") or DEFAULT_PORT)
    ca = int(m.group("ca") or 1)
    oa = int(m.group("oa") or 0)
    return host, port, ca, oa


def _object_id(type_id: TypeID, ioa: int) -> str:
    return f"{type_id.name}:{ioa}"


def _parse_object_id(object_id: str) -> tuple[TypeID, int]:
    if ":" not in object_id:
        raise EncodingError(f"IEC104 object_id must be 'TYPE:ioa', got {object_id!r}")
    type_str, ioa_str = object_id.split(":", 1)
    try:
        type_id = TypeID[type_str.strip().upper()]
    except KeyError as exc:
        raise EncodingError(f"Unknown ASDU type {type_str!r}") from exc
    try:
        ioa = int(ioa_str.strip(), 0)
    except ValueError as exc:
        raise EncodingError(f"Bad IOA in object_id {object_id!r}") from exc
    return type_id, ioa


def _data_type_for(type_id: TypeID) -> str:
    if type_id in {TypeID.M_SP_NA_1, TypeID.M_SP_TB_1, TypeID.C_SC_NA_1}:
        return "boolean"
    if type_id in {TypeID.M_DP_NA_1, TypeID.M_DP_TB_1, TypeID.C_DC_NA_1}:
        return "double-point"
    if type_id in {TypeID.M_ME_NA_1, TypeID.M_ME_NB_1, TypeID.C_SE_NA_1, TypeID.C_SE_NB_1}:
        return "int16"
    if type_id in {TypeID.M_ME_NC_1, TypeID.M_ME_TF_1, TypeID.C_SE_NC_1}:
        return "float32"
    if type_id in {TypeID.M_BO_NA_1, TypeID.M_BO_TB_1, TypeID.C_BO_NA_1}:
        return "bitstring32"
    if type_id in {TypeID.M_IT_NA_1, TypeID.M_IT_TB_1}:
        return "counter"
    if type_id in {TypeID.M_EI_NA_1, TypeID.C_IC_NA_1, TypeID.C_CI_NA_1}:
        return "uint8"
    if type_id is TypeID.C_RD_NA_1:
        return "any"
    if type_id is TypeID.C_CS_NA_1:
        return "datetime"
    return "unknown"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class Iec104TcpDriver(ProtocolDriver):
    """IEC 60870-5-104 master driver (TCP)."""

    PROTOCOL_ID: ClassVar[str] = "iec104.tcp"
    DISPLAY_NAME: ClassVar[str] = "IEC 60870-5-104"
    DESCRIPTION: ClassVar[str] = "IEC 104 telecontrol master over TCP"

    def __init__(self) -> None:
        self._point_lists: dict[str, list[PointDef]] = {}

    # ------------------------------------------------------------------
    # Point list registration (out-of-band; called by GUI/CLI)
    # ------------------------------------------------------------------

    def register_point_list(self, address: str, points: list[PointDef]) -> None:
        """Attach a point list to the device at ``address`` (post-parse)."""
        self._point_lists[address] = points

    def load_point_list_from_path(self, address: str, path: Path | str) -> None:
        self._point_lists[address] = load_point_list(Path(path))

    # ------------------------------------------------------------------
    # ProtocolDriver API
    # ------------------------------------------------------------------

    def discover(self, target: str) -> Iterator[DeviceRef]:
        """Probe ``target`` (host or comma list) for IEC 104 listeners.

        We only verify that the TCP port is open — sending STARTDT against
        a real RTU has side effects (it may close other sessions on
        single-master devices), so probing is intentionally non-intrusive.
        Operators wanting a deeper handshake can invoke ``connect`` and
        observe the result in the session log.
        """
        host_specs = [s.strip() for s in target.split(",") if s.strip()]
        for spec in host_specs:
            try:
                host, port, ca, _ = parse_address(spec)
            except EncodingError:
                _logger.debug("skipping unparseable target %r", spec)
                continue
            try:
                with socket.create_connection((host, port), timeout=1.0):
                    yield DeviceRef(
                        protocol=self.PROTOCOL_ID,
                        address=f"{host}:{port}/ca={ca}",
                        label=f"IEC104 @ {host}:{port}",
                        metadata={"ca": ca, "port": port},
                    )
            except OSError as exc:
                _logger.debug("IEC104 probe %s:%d closed/unreachable: %s", host, port, exc)

    def parse_address(self, address: str) -> DeviceRef:
        parse_address(address)  # validates
        return super().parse_address(address)

    def connect(self, device: DeviceRef, safety: SafetyContext) -> DriverSession:
        host, port, ca, oa = parse_address(device.address)
        cfg = MasterConfig(host=host, port=port, ca=ca, originator=oa)
        master = Iec104MasterSession(cfg)
        try:
            master.connect()
        except Exception:
            master.close()
            raise
        points = self._point_lists.get(device.address, [])
        return _Iec104Session(
            device=device,
            safety=safety,
            master=master,
            points=points,
            ca=ca,
        )


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class _Iec104Session(DriverSession):
    """Driver-session wrapper around :class:`Iec104MasterSession`."""

    def __init__(
        self,
        device: DeviceRef,
        safety: SafetyContext,
        master: Iec104MasterSession,
        points: list[PointDef],
        ca: int,
    ) -> None:
        self.device = device
        self.safety = safety
        self._master = master
        self._points = points
        self._ca = ca
        self._cache: dict[int, Asdu] = {}  # last spontaneous/GI value per IOA
        master.set_spontaneous_listener(self._on_spontaneous)

    # ---- frame capture --------------------------------------------------
    def attach_frame_sink(self, sink: CaptureSink | None) -> None:  # type: ignore[override]
        super().attach_frame_sink(sink)
        # Forward into the master's transport hook.
        self._master._frame_sink = sink  # type: ignore[assignment]

    # ---- enumeration ----------------------------------------------------
    def enumerate_objects(self) -> Iterator[ObjectRef]:
        for p in self._points:
            yield ObjectRef(
                device=self.device,
                object_id=_object_id(p.type_id, p.ioa),
                data_type=_data_type_for(p.type_id),
                access=p.access,
                label=p.label,
                unit=p.unit,
                metadata={"description": p.description, "ca": p.ca or self._ca},
            )

    # ---- read -----------------------------------------------------------
    def read(self, ref: ObjectRef) -> ReadResult:
        _type_id, ioa = _parse_object_id(ref.object_id)
        # Prefer cached spontaneous/GI value; fall back to C_RD_NA_1.
        cached = self._cache.get(ioa)
        if cached is not None:
            return self._cached_to_result(ref, cached, ioa)
        try:
            reply = self._master.read(ioa=ioa)
        except ConnectionFailure as exc:
            return ReadResult(
                object_ref=ref,
                value=None,
                quality=CoreQuality.TIMEOUT,
                timestamp=datetime.now(tz=timezone.utc),
                error=str(exc),
            )
        return self._cached_to_result(ref, reply, ioa)

    def read_many(self, refs: list[ObjectRef]) -> list[ReadResult]:
        # Trigger a station interrogation if cache is cold.
        if not self._cache:
            try:
                self._master.general_interrogation()
            except ConnectionFailure as exc:
                return [
                    ReadResult(
                        object_ref=ref,
                        value=None,
                        quality=CoreQuality.TIMEOUT,
                        timestamp=datetime.now(tz=timezone.utc),
                        error=str(exc),
                    )
                    for ref in refs
                ]
        return [self.read(r) for r in refs]

    def _cached_to_result(self, ref: ObjectRef, a: Asdu, ioa: int) -> ReadResult:
        match = next((o for o in a.objects if o.ioa == ioa), None)
        ts = match.timestamp if match and match.timestamp else datetime.now(tz=timezone.utc)
        if match is None:
            return ReadResult(
                object_ref=ref,
                value=None,
                quality=CoreQuality.UNCERTAIN,
                timestamp=ts,
                error="reply did not include requested IOA",
            )
        q = match.quality
        if q is None:
            quality = CoreQuality.GOOD
        elif q.invalid:
            quality = CoreQuality.BAD
        elif q.not_topical or q.substituted or q.blocked:
            quality = CoreQuality.UNCERTAIN
        else:
            quality = CoreQuality.GOOD
        return ReadResult(
            object_ref=ref,
            value=match.value,
            quality=quality,
            timestamp=ts,
        )

    # ---- write ----------------------------------------------------------
    def prepare_write(self, ref: ObjectRef, value: Any) -> WriteIntent:
        type_id, ioa = _parse_object_id(ref.object_id)
        # Allow callers to opt into Select-Before-Operate by passing a
        # 2-tuple ``(value, {"select": True})`` or a dict with key 'value'.
        select = False
        ql = 0
        actual_value: Any = value
        if isinstance(value, dict) and "value" in value:
            actual_value = value["value"]
            select = bool(value.get("select", False))
            ql = int(value.get("ql", value.get("qu", 0)))
        from protoskipper.builtin_drivers.iec104 import asdu as asdu_mod

        if type_id is TypeID.C_SC_NA_1:
            on = bool(actual_value)
            description = (
                f"Single command IOA={ioa} -> {'ON' if on else 'OFF'}"
                f"{' (SELECT)' if select else ''}"
            )
            encoded = asdu_mod.build_single_command(self._ca, ioa, on, select=select, qu=ql)
        elif type_id is TypeID.C_DC_NA_1:
            dcs = int(actual_value)
            if dcs not in (1, 2):
                raise EncodingError(f"Double command DCS must be 1 or 2, got {dcs}")
            description = (
                f"Double command IOA={ioa} -> {'ON' if dcs == 2 else 'OFF'}"
                f"{' (SELECT)' if select else ''}"
            )
            encoded = asdu_mod.build_double_command(self._ca, ioa, dcs, select=select, qu=ql)
        elif type_id is TypeID.C_SE_NA_1:
            description = (
                f"Set-point (normalised) IOA={ioa} -> {actual_value}{' (SELECT)' if select else ''}"
            )
            encoded = asdu_mod.build_set_point_normalised(
                self._ca, ioa, int(actual_value), select=select, ql=ql
            )
        elif type_id is TypeID.C_SE_NB_1:
            description = (
                f"Set-point (scaled) IOA={ioa} -> {actual_value}{' (SELECT)' if select else ''}"
            )
            encoded = asdu_mod.build_set_point_scaled(
                self._ca, ioa, int(actual_value), select=select, ql=ql
            )
        elif type_id is TypeID.C_SE_NC_1:
            description = (
                f"Set-point (float) IOA={ioa} -> {actual_value}{' (SELECT)' if select else ''}"
            )
            encoded = asdu_mod.build_set_point_float(
                self._ca, ioa, float(actual_value), select=select, ql=ql
            )
        elif type_id is TypeID.C_BO_NA_1:
            description = f"Bitstring command IOA={ioa} -> {int(actual_value):#010x}"
            encoded = asdu_mod.build_bitstring_command(self._ca, ioa, int(actual_value))
        elif type_id is TypeID.C_CS_NA_1:
            ts = (
                actual_value
                if isinstance(actual_value, datetime)
                else datetime.now(tz=timezone.utc)
            )
            description = f"Clock sync -> {ts.isoformat()}"
            encoded = asdu_mod.build_clock_sync(self._ca, ts)
        else:
            raise UnsupportedOperation(f"Write not supported for type {type_id.name}")
        return WriteIntent(
            object_ref=ref,
            requested_value=actual_value,
            encoded_bytes=encoded,
            description=description,
            metadata={
                "ca": self._ca,
                "ioa": ioa,
                "type": type_id.name,
                "select": select,
                "ql": ql,
            },
        )

    def commit_write(self, intent: WriteIntent) -> WriteResult:
        if not self.safety.require_write_authorization(intent):
            raise AuthorizationDenied(intent.description)
        type_id, ioa = _parse_object_id(intent.object_ref.object_id)
        select = bool(intent.metadata.get("select", False))
        ql = int(intent.metadata.get("ql", 0))
        try:
            if type_id is TypeID.C_SC_NA_1:
                reply = self._master.single_command(
                    ioa, bool(intent.requested_value), select=select, qu=ql
                )
            elif type_id is TypeID.C_DC_NA_1:
                reply = self._master.double_command(
                    ioa, int(intent.requested_value), select=select, qu=ql
                )
            elif type_id is TypeID.C_SE_NA_1:
                reply = self._master.set_point_normalised(
                    ioa, int(intent.requested_value), select=select, ql=ql
                )
            elif type_id is TypeID.C_SE_NB_1:
                reply = self._master.set_point_scaled(
                    ioa, int(intent.requested_value), select=select, ql=ql
                )
            elif type_id is TypeID.C_SE_NC_1:
                reply = self._master.set_point_float(
                    ioa, float(intent.requested_value), select=select, ql=ql
                )
            elif type_id is TypeID.C_BO_NA_1:
                reply = self._master.bitstring_command(ioa, int(intent.requested_value))
            elif type_id is TypeID.C_CS_NA_1:
                ts = (
                    intent.requested_value
                    if isinstance(intent.requested_value, datetime)
                    else datetime.now(tz=timezone.utc)
                )
                reply = self._master.clock_sync(ts)
            else:
                raise UnsupportedOperation(f"commit_write: unsupported type {type_id.name}")
        except ConnectionFailure as exc:
            result = WriteResult(
                intent=intent,
                success=False,
                timestamp=datetime.now(tz=timezone.utc),
                error=str(exc),
            )
            self.safety.record_write_outcome(result)
            return result
        success = reply.cot is COT.ACTCON and not reply.negative
        result = WriteResult(
            intent=intent,
            success=success,
            timestamp=datetime.now(tz=timezone.utc),
            response_bytes=None,
            error=None if success else f"reply COT={reply.cot.name} negative={reply.negative}",
        )
        self.safety.record_write_outcome(result)
        return result

    # ---- lifecycle ------------------------------------------------------
    def close(self) -> None:
        self._master.close()

    def abort(self) -> None:
        self._master.close()

    # ---- spontaneous handler -------------------------------------------
    def _on_spontaneous(self, a: Asdu) -> None:
        for obj in a.objects:
            self._cache[obj.ioa] = a
