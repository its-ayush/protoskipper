# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 60870-5-104 slave/server simulator.

A configurable, point-list-driven slave that listens on a TCP port and
emulates a real RTU/IED for testing master implementations, integration
tests, and lab commissioning.

Supported features (per ``docs/IEC104_PLAN.md`` P4.C):

* Multi-client TCP listener (each connection runs its own state machine
  on a daemon thread).
* APCI: STARTDT/STOPDT/TESTFR, S-frame ack, k/w sliding window, t1/t2/t3
  timers (mostly observed via the master side - the slave is permissive).
* Configurable point store keyed by IOA. Each point carries a type, value,
  quality and optional timestamp; values are mutable from the test harness
  via :meth:`Iec104SlaveServer.update` and the change is broadcast as a
  spontaneous ASDU to every connected master if the link is started.
* General Interrogation (C_IC_NA_1): replies ACTCON, then a sequence of
  monitor-direction ASDUs (one per point grouped by type), then ACTTERM.
* Counter Interrogation (C_CI_NA_1): replies ACTCON, M_IT_NA_1 with all
  counter points, then ACTTERM.
* Read command (C_RD_NA_1): returns the current value as an
  M_xx_NA_1/REQ ASDU.
* Direct-execute and Select-Before-Operate single/double commands and
  set-point commands. SBO state is tracked per IOA and times out after
  ``sbo_timeout`` seconds.
* Bitstring command (C_BO_NA_1).
* Clock synchronisation (C_CS_NA_1): replies ACTCON with the current UTC
  time.
* Custom command handler hook (``set_command_handler``) so tests can
  reject specific writes (return ``False`` from the handler to send a
  *negative* ACTCON with the P/N flag set).

This slave is suitable for unit/integration tests and for hardware-loop
fuzzing, but is **not** designed for production use - there is no
authentication, no TLS (yet), no persistence, and the timer recovery is
permissive.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from protoskipper.builtin_drivers.iec104.apci import (
    FrameFormat,
    UType,
    build_i_frame,
    build_s_frame,
    build_u_frame,
    parse_apdu,
    peek_apdu_length,
    seq_inc,
)
from protoskipper.builtin_drivers.iec104.asdu import (
    COT,
    QDS_IV,
    QOI_STATION,
    Asdu,
    BinaryCounter,
    InformationObject,
    Quality,
    TypeID,
    decode_asdu,
    encode_asdu,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapping monitor types to their command counterparts (and back)
# ---------------------------------------------------------------------------

# Monitor type -> default time-tagged equivalent for spontaneous events.
_TIMETAGGED_VARIANT: dict[TypeID, TypeID] = {
    TypeID.M_SP_NA_1: TypeID.M_SP_TB_1,
    TypeID.M_DP_NA_1: TypeID.M_DP_TB_1,
    TypeID.M_BO_NA_1: TypeID.M_BO_TB_1,
    TypeID.M_ME_NC_1: TypeID.M_ME_TF_1,
    TypeID.M_IT_NA_1: TypeID.M_IT_TB_1,
}

# Command type -> the monitor-direction type that mirrors its value back.
# Used to determine which point is targeted by a write command.
_COMMAND_TO_MONITOR: dict[TypeID, TypeID] = {
    TypeID.C_SC_NA_1: TypeID.M_SP_NA_1,
    TypeID.C_DC_NA_1: TypeID.M_DP_NA_1,
    TypeID.C_SE_NA_1: TypeID.M_ME_NA_1,
    TypeID.C_SE_NB_1: TypeID.M_ME_NB_1,
    TypeID.C_SE_NC_1: TypeID.M_ME_NC_1,
    TypeID.C_BO_NA_1: TypeID.M_BO_NA_1,
}

# Type IDs returned by counter interrogation.
_COUNTER_TYPES = {TypeID.M_IT_NA_1, TypeID.M_IT_TB_1}


# ---------------------------------------------------------------------------
# Configuration & data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlaveConfig:
    """Tunables for :class:`Iec104SlaveServer`."""

    host: str = "127.0.0.1"
    port: int = 0  # 0 = pick an ephemeral port
    ca: int = 1
    k: int = 12
    w: int = 8
    t1: float = 15.0
    t2: float = 10.0
    t3: float = 20.0
    sbo_timeout: float = 30.0  # SBO select expires if execute doesn't follow
    backlog: int = 5
    max_clients: int = 4
    # IEC 62351-3 TLS transport. When enabled, every accepted connection is
    # wrapped with TLS using ``tls_context`` (which MUST be provided by the
    # caller and configured with the slave's server certificate plus an
    # optional client-cert verification chain for mutual auth).
    tls: bool = False
    tls_context: ssl.SSLContext | None = None


@dataclass
class SlavePoint:
    """One point in the slave's data store."""

    ioa: int
    type_id: TypeID
    value: Any
    quality: Quality = field(default_factory=Quality)
    timestamp: datetime | None = None


# Command handler signature.
#
# Receives the decoded request ASDU. Return ``True`` to accept (slave will
# update the point and reply ACTCON with positive flag), ``False`` to reject
# (ACTCON with P/N=1, point unchanged), or ``None`` to defer to the default
# behaviour (accept and update).
CommandHandler = Callable[[Asdu], "bool | None"]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class Iec104SlaveServer:
    """A configurable IEC 60870-5-104 slave bound to a TCP port.

    Typical usage::

        srv = Iec104SlaveServer(SlaveConfig(port=0, ca=1))
        srv.add_point(SlavePoint(ioa=1001, type_id=TypeID.M_SP_NA_1, value=False))
        srv.add_point(SlavePoint(ioa=4001, type_id=TypeID.M_ME_NC_1, value=230.5))
        srv.start()
        ...
        srv.update(1001, True)        # broadcasts spontaneous M_SP_TB_1 event
        srv.stop()

    All public methods are thread-safe.
    """

    def __init__(self, config: SlaveConfig | None = None) -> None:
        self._cfg = config or SlaveConfig()
        self._points: dict[int, SlavePoint] = {}
        self._points_lock = threading.RLock()
        self._sbo: dict[int, tuple[Asdu, float]] = {}  # IOA -> (request asdu, deadline)
        self._sbo_lock = threading.Lock()
        self._command_handler: CommandHandler | None = None
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._clients: list[_ClientSession] = []
        self._clients_lock = threading.Lock()
        self._port = 0
        self._started = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def add_point(self, point: SlavePoint) -> None:
        """Register a new point, replacing any existing entry at the same IOA."""
        with self._points_lock:
            self._points[point.ioa] = point

    def add_points(self, points: list[SlavePoint]) -> None:
        for p in points:
            self.add_point(p)

    def get_point(self, ioa: int) -> SlavePoint | None:
        with self._points_lock:
            return self._points.get(ioa)

    def set_command_handler(self, handler: CommandHandler | None) -> None:
        self._command_handler = handler

    @property
    def port(self) -> int:
        return self._port

    @property
    def ca(self) -> int:
        return self._cfg.ca

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return sum(1 for c in self._clients if c.alive)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._cfg.host, self._cfg.port))
        sock.listen(self._cfg.backlog)
        sock.settimeout(0.5)
        self._sock = sock
        self._port = sock.getsockname()[1]
        self._started = True
        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name=f"iec104-slave-accept-{self._port}", daemon=True
        )
        self._accept_thread.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            c.close()
        thread = self._accept_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._started = False

    # ------------------------------------------------------------------
    # Data updates / spontaneous events
    # ------------------------------------------------------------------

    def update(
        self,
        ioa: int,
        value: Any,
        *,
        quality: Quality | None = None,
        timestamp: datetime | None = None,
        cot: COT = COT.SPONT,
        broadcast: bool = True,
    ) -> None:
        """Update a point's value and optionally broadcast as spontaneous ASDU.

        If the point is not registered yet, raises :class:`KeyError`.
        """
        with self._points_lock:
            point = self._points.get(ioa)
            if point is None:
                raise KeyError(f"unknown IOA {ioa}")
            point.value = value
            if quality is not None:
                point.quality = quality
            point.timestamp = timestamp or datetime.now(tz=timezone.utc)
        if broadcast:
            asdu = self._build_spontaneous(point, cot=cot)
            self.broadcast(asdu)

    def broadcast(self, asdu: Asdu) -> None:
        """Send ``asdu`` as an I-frame to every started connection."""
        with self._clients_lock:
            clients = [c for c in self._clients if c.alive and c.started]
        for c in clients:
            try:
                c.send_asdu(asdu)
            except OSError:
                _logger.debug("broadcast send failed; client will be reaped")

    # ------------------------------------------------------------------
    # Internal: accept loop
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._stop.is_set():
            try:
                client_sock, _ = sock.accept()
            except TimeoutError:
                self._reap_clients()
                continue
            except OSError:
                return
            if self._cfg.tls:
                if self._cfg.tls_context is None:
                    _logger.error("TLS enabled but no tls_context configured; dropping connection")
                    with contextlib.suppress(OSError):
                        client_sock.close()
                    continue
                try:
                    client_sock = self._cfg.tls_context.wrap_socket(client_sock, server_side=True)
                except (ssl.SSLError, OSError) as exc:
                    _logger.warning("TLS handshake failed: %s", exc)
                    with contextlib.suppress(OSError):
                        client_sock.close()
                    continue
            with self._clients_lock:
                if sum(1 for c in self._clients if c.alive) >= self._cfg.max_clients:
                    with contextlib.suppress(OSError):
                        client_sock.close()
                    continue
                session = _ClientSession(self, client_sock)
                self._clients.append(session)
            session.start()

    def _reap_clients(self) -> None:
        with self._clients_lock:
            self._clients = [c for c in self._clients if c.alive]

    # ------------------------------------------------------------------
    # Internal: ASDU builders & command handling
    # ------------------------------------------------------------------

    def _build_spontaneous(self, point: SlavePoint, *, cot: COT) -> Asdu:
        """Build a spontaneous monitor ASDU for ``point``.

        Time-tagged variants are used when a timestamp is set; otherwise the
        no-time-tag variant is used.
        """
        type_id = point.type_id
        if point.timestamp is not None:
            type_id = _TIMETAGGED_VARIANT.get(point.type_id, point.type_id)
        obj = InformationObject(
            ioa=point.ioa,
            value=point.value,
            quality=point.quality,
            timestamp=point.timestamp,
        )
        return Asdu(type_id=type_id, cot=cot, ca=self._cfg.ca, objects=[obj])

    def _handle_request(self, session: _ClientSession, req: Asdu) -> None:
        """Route an incoming ASDU to the appropriate handler."""
        if req.ca != self._cfg.ca:
            self._reply_unknown_ca(session, req)
            return
        type_id = req.type_id
        if type_id is TypeID.C_IC_NA_1:
            self._handle_interrogation(session, req)
        elif type_id is TypeID.C_CI_NA_1:
            self._handle_counter_interrogation(session, req)
        elif type_id is TypeID.C_RD_NA_1:
            self._handle_read(session, req)
        elif type_id is TypeID.C_CS_NA_1:
            self._handle_clock_sync(session, req)
        elif type_id in _COMMAND_TO_MONITOR:
            self._handle_command(session, req)
        else:
            self._reply_unknown_type(session, req)

    def _handle_interrogation(self, session: _ClientSession, req: Asdu) -> None:
        # ACTCON
        session.send_asdu(
            Asdu(type_id=TypeID.C_IC_NA_1, cot=COT.ACTCON, ca=self._cfg.ca, objects=req.objects)
        )
        # All monitor points grouped by type, in INTROGEN.
        by_type: dict[TypeID, list[SlavePoint]] = {}
        with self._points_lock:
            for p in self._points.values():
                if p.type_id in _COUNTER_TYPES:
                    continue  # counters answered by C_CI
                if p.type_id.value < 45:  # monitor types only
                    by_type.setdefault(p.type_id, []).append(p)
        for tid, pts in by_type.items():
            objs = [
                InformationObject(
                    ioa=p.ioa,
                    value=p.value,
                    quality=p.quality,
                    timestamp=None,  # GI uses no-time-tag variants
                )
                for p in pts
            ]
            session.send_asdu(Asdu(type_id=tid, cot=COT.INTROGEN, ca=self._cfg.ca, objects=objs))
        # ACTTERM
        session.send_asdu(
            Asdu(type_id=TypeID.C_IC_NA_1, cot=COT.ACTTERM, ca=self._cfg.ca, objects=req.objects)
        )

    def _handle_counter_interrogation(self, session: _ClientSession, req: Asdu) -> None:
        session.send_asdu(
            Asdu(type_id=TypeID.C_CI_NA_1, cot=COT.ACTCON, ca=self._cfg.ca, objects=req.objects)
        )
        with self._points_lock:
            counters = [p for p in self._points.values() if p.type_id in _COUNTER_TYPES]
        if counters:
            objs = [
                InformationObject(
                    ioa=p.ioa,
                    value=(
                        p.value
                        if isinstance(p.value, BinaryCounter)
                        else BinaryCounter(count=int(p.value))
                    ),
                )
                for p in counters
            ]
            session.send_asdu(
                Asdu(type_id=TypeID.M_IT_NA_1, cot=COT.REQCOGEN, ca=self._cfg.ca, objects=objs)
            )
        session.send_asdu(
            Asdu(type_id=TypeID.C_CI_NA_1, cot=COT.ACTTERM, ca=self._cfg.ca, objects=req.objects)
        )

    def _handle_read(self, session: _ClientSession, req: Asdu) -> None:
        if not req.objects:
            return
        ioa = req.objects[0].ioa
        with self._points_lock:
            point = self._points.get(ioa)
        if point is None:
            session.send_asdu(
                Asdu(
                    type_id=TypeID.C_RD_NA_1,
                    cot=COT.UNKNOWN_IOA,
                    ca=self._cfg.ca,
                    objects=req.objects,
                    negative=True,
                )
            )
            return
        obj = InformationObject(ioa=point.ioa, value=point.value, quality=point.quality)
        session.send_asdu(Asdu(type_id=point.type_id, cot=COT.REQ, ca=self._cfg.ca, objects=[obj]))

    def _handle_clock_sync(self, session: _ClientSession, req: Asdu) -> None:
        now = datetime.now(tz=timezone.utc)
        session.send_asdu(
            Asdu(
                type_id=TypeID.C_CS_NA_1,
                cot=COT.ACTCON,
                ca=self._cfg.ca,
                objects=[InformationObject(ioa=0, value=now, timestamp=now)],
            )
        )

    def _handle_command(self, session: _ClientSession, req: Asdu) -> None:
        if not req.objects:
            return
        obj = req.objects[0]
        ioa = obj.ioa
        # Apply user command handler first (decides accept / reject / default).
        decision: bool | None = None
        if self._command_handler is not None:
            try:
                decision = self._command_handler(req)
            except Exception:  # pragma: no cover - handler errors should not crash
                _logger.exception("command handler raised; rejecting")
                decision = False
        if decision is False:
            session.send_asdu(
                Asdu(
                    type_id=req.type_id,
                    cot=COT.ACTCON,
                    ca=self._cfg.ca,
                    objects=req.objects,
                    negative=True,
                )
            )
            return
        # SBO bookkeeping: select phase records the request and replies ACTCON
        # without applying the value. Execute phase requires a prior matching
        # select within sbo_timeout, otherwise rejected.
        is_select = bool(obj.select)
        now = time.monotonic()
        if is_select:
            with self._sbo_lock:
                self._sbo[ioa] = (req, now + self._cfg.sbo_timeout)
            session.send_asdu(
                Asdu(
                    type_id=req.type_id,
                    cot=COT.ACTCON,
                    ca=self._cfg.ca,
                    objects=req.objects,
                )
            )
            return
        # Execute phase: if there's a stored select, validate and pop.
        with self._sbo_lock:
            stored = self._sbo.pop(ioa, None)
        if stored is not None:
            stored_req, deadline = stored
            if now > deadline or stored_req.type_id is not req.type_id:
                # Expired select -> reject this execute.
                session.send_asdu(
                    Asdu(
                        type_id=req.type_id,
                        cot=COT.ACTCON,
                        ca=self._cfg.ca,
                        objects=req.objects,
                        negative=True,
                    )
                )
                return
        # Apply the value to the matching monitor point if registered.
        monitor_type = _COMMAND_TO_MONITOR.get(req.type_id)
        if monitor_type is not None:
            with self._points_lock:
                point = self._points.get(ioa)
                if point is not None:
                    point.value = obj.value
                    point.timestamp = datetime.now(tz=timezone.utc)
        # ACTCON
        session.send_asdu(
            Asdu(type_id=req.type_id, cot=COT.ACTCON, ca=self._cfg.ca, objects=req.objects)
        )
        # Some control type IDs additionally emit ACTTERM (e.g. C_SC commands
        # following step-by-step procedures). Send ACTTERM for symmetry with
        # interrogation; this matches what many real RTUs do.
        session.send_asdu(
            Asdu(type_id=req.type_id, cot=COT.ACTTERM, ca=self._cfg.ca, objects=req.objects)
        )

    def _reply_unknown_type(self, session: _ClientSession, req: Asdu) -> None:
        session.send_asdu(
            Asdu(
                type_id=req.type_id,
                cot=COT.UNKNOWN_TYPE,
                ca=self._cfg.ca,
                objects=req.objects,
                negative=True,
            )
        )

    def _reply_unknown_ca(self, session: _ClientSession, req: Asdu) -> None:
        session.send_asdu(
            Asdu(
                type_id=req.type_id,
                cot=COT.UNKNOWN_CA,
                ca=req.ca,
                objects=req.objects,
                negative=True,
            )
        )


# ---------------------------------------------------------------------------
# Per-connection state machine
# ---------------------------------------------------------------------------


class _ClientSession:
    """One peer connection's state machine."""

    def __init__(self, server: Iec104SlaveServer, sock: socket.socket) -> None:
        self._server = server
        self._sock = sock
        self._sock.settimeout(0.5)
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ns = 0
        self._nr = 0
        self._unacked_received = 0
        self._last_tx = time.monotonic()
        self._last_rx = time.monotonic()
        self._started = False
        self.alive = False

    def start(self) -> None:
        self.alive = True
        self._thread = threading.Thread(target=self._serve, name="iec104-slave-conn", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._sock.close()
        self.alive = False

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------

    def send_asdu(self, asdu: Asdu) -> None:
        body = encode_asdu(asdu)
        with self._state_lock:
            ns = self._ns
            self._ns = seq_inc(self._ns)
            nr = self._nr
            self._unacked_received = 0  # piggy-back ack
        frame = build_i_frame(send_seq=ns, recv_seq=nr, asdu=body)
        self._raw_send(frame)

    def _send_s(self) -> None:
        with self._state_lock:
            nr = self._nr
            self._unacked_received = 0
        self._raw_send(build_s_frame(nr))

    def _send_u(self, utype: UType) -> None:
        self._raw_send(build_u_frame(utype))

    def _raw_send(self, frame: bytes) -> None:
        with self._send_lock:
            try:
                self._sock.sendall(frame)
            except OSError:
                self._stop.set()
                raise
        self._last_tx = time.monotonic()

    # ------------------------------------------------------------------

    def _serve(self) -> None:
        buf = bytearray()
        try:
            while not self._stop.is_set():
                self._tick_timers()
                try:
                    chunk = self._sock.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                self._last_rx = time.monotonic()
                buf.extend(chunk)
                while True:
                    try:
                        total = peek_apdu_length(bytes(buf[:2]))
                    except Exception:
                        _logger.warning("framing error from peer; closing")
                        return
                    if total is None or len(buf) < total:
                        break
                    raw = bytes(buf[:total])
                    del buf[:total]
                    try:
                        apdu = parse_apdu(raw)
                    except Exception:
                        _logger.warning("malformed APDU dropped")
                        continue
                    self._handle_apdu(apdu)
        finally:
            self.alive = False
            with contextlib.suppress(OSError):
                self._sock.close()

    def _tick_timers(self) -> None:
        now = time.monotonic()
        # t2 ack of received I-frames.
        with self._state_lock:
            need_s = self._unacked_received >= self._server._cfg.w or (
                self._unacked_received > 0 and now - self._last_rx >= self._server._cfg.t2
            )
        if need_s:
            with contextlib.suppress(OSError):
                self._send_s()
        # t3 idle: send TESTFR if no traffic for t3.
        if (
            self._started
            and now - self._last_tx >= self._server._cfg.t3
            and now - self._last_rx >= self._server._cfg.t3
        ):
            with contextlib.suppress(OSError):
                self._send_u(UType.TESTFR_ACT)

    def _handle_apdu(self, apdu: Any) -> None:
        if apdu.fmt is FrameFormat.U:
            self._handle_u(apdu.utype)
            return
        if apdu.fmt is FrameFormat.S:
            return  # peer ack; no state needed beyond updating last_rx
        # I-format
        if not self._started:
            _logger.warning("I-frame received before STARTDT; ignoring")
            return
        with self._state_lock:
            self._nr = seq_inc(self._nr)
            self._unacked_received += 1
        try:
            req = decode_asdu(apdu.asdu)
        except Exception:
            _logger.warning("undecodable ASDU dropped")
            return
        self._server._handle_request(self, req)

    def _handle_u(self, utype: UType | None) -> None:
        if utype is UType.STARTDT_ACT:
            self._send_u(UType.STARTDT_CON)
            self._started = True
        elif utype is UType.STOPDT_ACT:
            self._send_u(UType.STOPDT_CON)
            self._started = False
        elif utype is UType.TESTFR_ACT:
            self._send_u(UType.TESTFR_CON)
        # CONs and unknown utypes are ignored.


# Keep the Quality default-with-validity exported for tests.
__all__ = [
    "QDS_IV",
    "QOI_STATION",
    "CommandHandler",
    "Iec104SlaveServer",
    "SlaveConfig",
    "SlavePoint",
]
