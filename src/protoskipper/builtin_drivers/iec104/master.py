# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 60870-5-104 master state machine and transport.

This module implements the protocol-level state machine that sits between
a TCP socket and the high-level :class:`Iec104Session` driver session:

* APDU framing: read whole APDUs out of a TCP byte stream.
* Sliding-window flow control (k, w) per IEC 60870-5-104 §5.3.
* Timers t0 (connect), t1 (send timeout), t2 (ack timeout), t3 (test idle).
* STARTDT/STOPDT lifecycle.
* Pending-request matching (callers register a future against a predicate;
  the receive thread completes it when a matching ASDU arrives).

The session is **single-master**: the receive loop runs on a dedicated
thread; the master thread (driver session) dispatches calls and waits on
futures backed by ``threading.Event``. There is no asyncio dependency,
mirroring the synchronous transport pattern used by the Modbus driver.

Defaults (per IEC 60870-5-104 Table 5):

* k = 12   (max sent I-frames without ack)
* w = 8    (latest ack threshold for received I-frames)
* t0 = 30s (connection establishment)
* t1 = 15s (send/test confirmation timeout)
* t2 = 10s (ack timeout for received I-frames; must be < t1)
* t3 = 20s (test frame idle timeout)
"""

from __future__ import annotations

import contextlib
import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from protoskipper.builtin_drivers.iec104 import apci, asdu
from protoskipper.builtin_drivers.iec104.apci import (
    Apdu,
    FrameFormat,
    UType,
    build_i_frame,
    build_s_frame,
    build_u_frame,
    parse_apdu,
    peek_apdu_length,
    seq_inc,
)
from protoskipper.builtin_drivers.iec104.asdu import COT, Asdu, TypeID
from protoskipper.core.errors import ConnectionFailure, ProtoSkipperError

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MasterConfig:
    """Tunable parameters of the master session."""

    host: str
    port: int = 2404
    ca: int = 1
    originator: int = 0
    k: int = 12
    w: int = 8
    t0: float = 30.0
    t1: float = 15.0
    t2: float = 10.0
    t3: float = 20.0
    auto_reconnect: bool = True
    reconnect_delay: float = 5.0
    # IEC 62351-3 TLS transport. Enable to wrap the TCP socket with TLS.
    # If ``tls_context`` is None and ``tls`` is True, a default client
    # context is created; tests / production code typically pass an
    # ``ssl.SSLContext`` configured with the substation's CA bundle and
    # client cert.
    tls: bool = False
    tls_context: ssl.SSLContext | None = None
    tls_server_hostname: str | None = None


# ---------------------------------------------------------------------------
# Pending request bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class PendingRequest:
    """One outstanding master->slave request waiting on a matching reply.

    ``predicate`` examines each received ASDU and returns ``True`` if it
    completes this request. The first match transfers the ASDU to
    ``replies`` and (for single-shot requests) sets the event.
    """

    predicate: Callable[[Asdu], bool]
    replies: list[Asdu]
    event: threading.Event
    multi: bool = False  # True = collect until terminator (e.g. GI ACTTERM)
    terminator: Callable[[Asdu], bool] | None = None


# ---------------------------------------------------------------------------
# Frame sink (for capture)
# ---------------------------------------------------------------------------


class FrameSink:
    """Minimal protocol used to forward raw frames to a capture target.

    Matches :class:`protoskipper.core.driver.CaptureSink` structurally; we
    avoid importing it here to keep this module driver-agnostic.
    """

    def write_frame(
        self,
        timestamp: datetime,
        direction: str,
        payload: bytes,
        metadata: dict | None = None,
    ) -> None:  # pragma: no cover - protocol stub
        ...


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class Iec104MasterSession:
    """Synchronous IEC 104 master session.

    Lifecycle::

        s = Iec104MasterSession(MasterConfig(host="10.0.0.5"))
        s.connect()                  # opens TCP, sends STARTDT act, waits con
        s.general_interrogation()    # blocks until ACTTERM
        s.read(ioa=4001)             # blocks until reply
        s.single_command(ioa=2001, on=True)
        s.close()
    """

    def __init__(
        self,
        config: MasterConfig,
        frame_sink: FrameSink | None = None,
    ) -> None:
        self._cfg = config
        self._frame_sink = frame_sink

        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._rx_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._started = threading.Event()  # STARTDT confirmed
        self._closed = False

        # Sequence numbers / window
        self._ns = 0  # send sequence number
        self._nr = 0  # receive sequence number
        self._ack_sent = 0  # peer's ack of our N(S) (largest acked + 1)
        self._unacked_received = 0  # how many I-frames received without S-ack
        self._last_rx_time = time.monotonic()
        self._last_tx_time = time.monotonic()
        self._test_outstanding = False
        self._test_sent_time = 0.0
        self._oldest_unacked_send_time = 0.0  # when the oldest unacked I-frame was sent

        # Pending request queue
        self._pending: list[PendingRequest] = []

        # Optional async ASDU listener (e.g. spontaneous events) for the
        # higher-level driver session.
        self._spont_listener: Callable[[Asdu], None] | None = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def set_spontaneous_listener(self, fn: Callable[[Asdu], None] | None) -> None:
        self._spont_listener = fn

    def connect(self) -> None:
        if self._closed:
            raise ProtoSkipperError("Session is closed; build a new one")
        try:
            raw_sock = socket.create_connection(
                (self._cfg.host, self._cfg.port),
                timeout=self._cfg.t0,
            )
        except OSError as exc:
            raise ConnectionFailure(
                f"IEC104 connect to {self._cfg.host}:{self._cfg.port} failed: {exc}"
            ) from exc
        if self._cfg.tls:
            ctx = self._cfg.tls_context or ssl.create_default_context()
            try:
                self._sock = ctx.wrap_socket(
                    raw_sock,
                    server_hostname=self._cfg.tls_server_hostname or self._cfg.host,
                )
            except (ssl.SSLError, OSError) as exc:
                with contextlib.suppress(OSError):
                    raw_sock.close()
                raise ConnectionFailure(f"IEC104 TLS handshake failed: {exc}") from exc
        else:
            self._sock = raw_sock
        # Switch to non-blocking-ish: use a timeout for clean shutdown polling.
        self._sock.settimeout(0.5)
        self._connected.set()
        self._rx_thread = threading.Thread(target=self._receive_loop, name="iec104-rx", daemon=True)
        self._rx_thread.start()
        # Send STARTDT act and wait for STARTDT con.
        self._send_u(UType.STARTDT_ACT)
        if not self._started.wait(timeout=self._cfg.t1):
            self.close()
            raise ConnectionFailure("STARTDT not confirmed within t1")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                # Best-effort STOPDT before closing.
                if self._started.is_set():
                    try:
                        with self._send_lock:
                            sock.sendall(build_u_frame(UType.STOPDT_ACT))
                    except OSError:
                        pass
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            with contextlib.suppress(OSError):
                sock.close()
        if self._rx_thread is not None and self._rx_thread is not threading.current_thread():
            self._rx_thread.join(timeout=2.0)
        # Fail any pending requests.
        with self._state_lock:
            for req in self._pending:
                req.event.set()
            self._pending.clear()

    @property
    def started(self) -> bool:
        return self._started.is_set()

    # ------------------------------------------------------------------
    # High-level operations
    # ------------------------------------------------------------------

    def general_interrogation(
        self,
        qoi: int = asdu.QOI_STATION,
        timeout: float | None = None,
    ) -> list[Asdu]:
        """Issue a station interrogation and collect responses until ACTTERM.

        Returns the list of monitor-direction ASDUs received as part of the
        interrogation, in arrival order. Raises if no ACTCON arrives within
        t1, or no ACTTERM within ``timeout``.
        """
        body = asdu.build_general_interrogation(self._cfg.ca, qoi)
        ca = self._cfg.ca

        replies: list[Asdu] = []
        completed = threading.Event()

        def predicate(a: Asdu) -> bool:
            if a.ca != ca:
                return False
            # Collect ACTCON / monitor responses tagged with COT INTROGEN
            if a.type_id is TypeID.C_IC_NA_1 and a.cot in (COT.ACTCON, COT.ACTTERM):
                return True
            return a.cot is COT.INTROGEN

        def terminator(a: Asdu) -> bool:
            return a.type_id is TypeID.C_IC_NA_1 and a.cot is COT.ACTTERM and a.ca == ca

        req = PendingRequest(
            predicate=predicate,
            replies=replies,
            event=completed,
            multi=True,
            terminator=terminator,
        )
        with self._state_lock:
            self._pending.append(req)
        self._send_i(body)
        wait_t = timeout if timeout is not None else max(self._cfg.t1 * 4, 60.0)
        if not completed.wait(timeout=wait_t):
            self._cancel_pending(req)
            raise ConnectionFailure("General interrogation did not complete within timeout")
        return replies

    def read(self, ioa: int, timeout: float | None = None) -> Asdu:
        """Issue a C_RD_NA_1 read and wait for the reply ASDU."""
        body = asdu.build_read_command(self._cfg.ca, ioa)
        ca = self._cfg.ca
        replies: list[Asdu] = []
        ev = threading.Event()

        def predicate(a: Asdu) -> bool:
            if a.ca != ca:
                return False
            # Accept a monitor frame at this IOA OR a negative confirmation.
            if a.type_id is TypeID.C_RD_NA_1 and a.cot in (COT.ACTCON,):
                return True
            return any(obj.ioa == ioa for obj in a.objects)

        req = PendingRequest(predicate=predicate, replies=replies, event=ev)
        with self._state_lock:
            self._pending.append(req)
        self._send_i(body)
        wait_t = timeout if timeout is not None else self._cfg.t1
        if not ev.wait(timeout=wait_t):
            self._cancel_pending(req)
            raise ConnectionFailure(f"Read of IOA {ioa} timed out")
        if not replies:
            raise ConnectionFailure(f"Read of IOA {ioa} got no reply")
        return replies[0]

    def single_command(
        self,
        ioa: int,
        on: bool,
        timeout: float | None = None,
        *,
        select: bool = False,
        qu: int = 0,
    ) -> Asdu:
        body = asdu.build_single_command(self._cfg.ca, ioa, on, select=select, qu=qu)
        return self._issue_command(body, ioa, TypeID.C_SC_NA_1, timeout)

    def double_command(
        self,
        ioa: int,
        dcs: int,
        timeout: float | None = None,
        *,
        select: bool = False,
        qu: int = 0,
    ) -> Asdu:
        body = asdu.build_double_command(self._cfg.ca, ioa, dcs, select=select, qu=qu)
        return self._issue_command(body, ioa, TypeID.C_DC_NA_1, timeout)

    def set_point_normalised(
        self,
        ioa: int,
        value: int,
        timeout: float | None = None,
        *,
        select: bool = False,
        ql: int = 0,
    ) -> Asdu:
        body = asdu.build_set_point_normalised(self._cfg.ca, ioa, value, select=select, ql=ql)
        return self._issue_command(body, ioa, TypeID.C_SE_NA_1, timeout)

    def set_point_scaled(
        self,
        ioa: int,
        value: int,
        timeout: float | None = None,
        *,
        select: bool = False,
        ql: int = 0,
    ) -> Asdu:
        body = asdu.build_set_point_scaled(self._cfg.ca, ioa, value, select=select, ql=ql)
        return self._issue_command(body, ioa, TypeID.C_SE_NB_1, timeout)

    def set_point_float(
        self,
        ioa: int,
        value: float,
        timeout: float | None = None,
        *,
        select: bool = False,
        ql: int = 0,
    ) -> Asdu:
        body = asdu.build_set_point_float(self._cfg.ca, ioa, value, select=select, ql=ql)
        return self._issue_command(body, ioa, TypeID.C_SE_NC_1, timeout)

    def bitstring_command(
        self,
        ioa: int,
        value: int,
        timeout: float | None = None,
    ) -> Asdu:
        body = asdu.build_bitstring_command(self._cfg.ca, ioa, value)
        return self._issue_command(body, ioa, TypeID.C_BO_NA_1, timeout)

    def counter_interrogation(
        self,
        rqt: int = asdu.QCC_RQT_GENERAL,
        frz: int = asdu.QCC_FRZ_READ,
        timeout: float | None = None,
    ) -> list[Asdu]:
        """Issue C_CI_NA_1 and collect M_IT_* replies until ACTTERM."""
        body = asdu.build_counter_interrogation(self._cfg.ca, rqt=rqt, frz=frz)
        ca = self._cfg.ca
        replies: list[Asdu] = []
        completed = threading.Event()

        def predicate(a: Asdu) -> bool:
            if a.ca != ca:
                return False
            if a.type_id in (TypeID.M_IT_NA_1, TypeID.M_IT_TB_1):
                return True
            return a.type_id is TypeID.C_CI_NA_1

        def is_terminator(a: Asdu) -> bool:
            return a.type_id is TypeID.C_CI_NA_1 and a.cot is COT.ACTTERM

        req = PendingRequest(
            predicate=predicate,
            replies=replies,
            event=completed,
            multi=True,
            terminator=is_terminator,
        )
        with self._state_lock:
            self._pending.append(req)
        self._send_i(body)
        wait_t = timeout if timeout is not None else max(self._cfg.t1 * 4, 60.0)
        if not completed.wait(timeout=wait_t):
            self._cancel_pending(req)
            raise ConnectionFailure("Counter interrogation did not complete within timeout")
        return replies

    def clock_sync(self, ts: datetime | None = None, timeout: float | None = None) -> Asdu:
        ts = ts or datetime.now(tz=timezone.utc)
        body = asdu.build_clock_sync(self._cfg.ca, ts)
        ca = self._cfg.ca
        replies: list[Asdu] = []
        ev = threading.Event()

        def predicate(a: Asdu) -> bool:
            return (
                a.ca == ca
                and a.type_id is TypeID.C_CS_NA_1
                and a.cot
                in (
                    COT.ACTCON,
                    COT.UNKNOWN_TYPE,
                    COT.UNKNOWN_CAUSE,
                )
            )

        req = PendingRequest(predicate=predicate, replies=replies, event=ev)
        with self._state_lock:
            self._pending.append(req)
        self._send_i(body)
        wait_t = timeout if timeout is not None else self._cfg.t1
        if not ev.wait(timeout=wait_t):
            self._cancel_pending(req)
            raise ConnectionFailure("Clock sync ACTCON not received within t1")
        return replies[0]

    def _issue_command(
        self,
        body: bytes,
        ioa: int,
        type_id: TypeID,
        timeout: float | None,
    ) -> Asdu:
        ca = self._cfg.ca
        replies: list[Asdu] = []
        ev = threading.Event()

        def predicate(a: Asdu) -> bool:
            if a.ca != ca or a.type_id is not type_id:
                return False
            if a.cot not in (COT.ACTCON, COT.ACTTERM):
                return False
            return any(obj.ioa == ioa for obj in a.objects)

        req = PendingRequest(predicate=predicate, replies=replies, event=ev)
        with self._state_lock:
            self._pending.append(req)
        self._send_i(body)
        wait_t = timeout if timeout is not None else self._cfg.t1
        if not ev.wait(timeout=wait_t):
            self._cancel_pending(req)
            raise ConnectionFailure(f"Command on IOA {ioa} not confirmed within t1")
        return replies[0]

    # ------------------------------------------------------------------
    # Internal: send paths
    # ------------------------------------------------------------------

    def _send_i(self, body: bytes) -> None:
        """Send an I-frame, blocking if the send window is full."""
        # Wait until window allows another send: outstanding = ns - ack_sent
        deadline = time.monotonic() + self._cfg.t1
        while True:
            with self._state_lock:
                outstanding = (self._ns - self._ack_sent) & apci.SEQ_MASK
                if outstanding < self._cfg.k:
                    ns = self._ns
                    nr = self._nr
                    self._ns = seq_inc(self._ns)
                    self._unacked_received = 0  # we're piggybacking ack
                    if outstanding == 0:
                        self._oldest_unacked_send_time = time.monotonic()
                    break
            if time.monotonic() > deadline:
                raise ConnectionFailure("Send window full; t1 elapsed without ACK")
            time.sleep(0.01)
        frame = build_i_frame(ns, nr, body)
        self._raw_send(frame, "tx-i")

    def _send_s(self) -> None:
        with self._state_lock:
            nr = self._nr
            self._unacked_received = 0
        frame = build_s_frame(nr)
        self._raw_send(frame, "tx-s")

    def _send_u(self, utype: UType) -> None:
        frame = build_u_frame(utype)
        self._raw_send(frame, "tx-u")
        if utype is UType.TESTFR_ACT:
            self._test_outstanding = True
            self._test_sent_time = time.monotonic()

    def _raw_send(self, frame: bytes, tag: str) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionFailure("Socket not connected")
        with self._send_lock:
            try:
                sock.sendall(frame)
            except OSError as exc:
                raise ConnectionFailure(f"send failed: {exc}") from exc
        self._last_tx_time = time.monotonic()
        if self._frame_sink is not None:
            try:
                self._frame_sink.write_frame(
                    datetime.now(tz=timezone.utc),
                    "tx",
                    frame,
                    {"protocol": "iec104.tcp", "tag": tag},
                )
            except Exception:  # pragma: no cover - sink errors must not break protocol
                _logger.debug("frame sink raised on tx", exc_info=True)

    # ------------------------------------------------------------------
    # Internal: receive loop
    # ------------------------------------------------------------------

    def _receive_loop(self) -> None:
        buf = bytearray()
        try:
            while not self._stop.is_set():
                self._tick_timers()
                sock = self._sock
                if sock is None:
                    return
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    continue
                except OSError as exc:
                    _logger.debug("recv error: %s", exc)
                    return
                if not chunk:
                    _logger.info("peer closed connection")
                    return
                buf.extend(chunk)
                while True:
                    try:
                        total = peek_apdu_length(bytes(buf[:2]))
                    except ProtoSkipperError as exc:
                        _logger.warning("framing error, dropping connection: %s", exc)
                        return
                    if total is None or len(buf) < total:
                        break
                    apdu_bytes = bytes(buf[:total])
                    del buf[:total]
                    try:
                        apdu = parse_apdu(apdu_bytes)
                    except ProtoSkipperError as exc:
                        _logger.warning("malformed APDU dropped: %s", exc)
                        continue
                    if self._frame_sink is not None:
                        try:
                            self._frame_sink.write_frame(
                                datetime.now(tz=timezone.utc),
                                "rx",
                                apdu_bytes,
                                {"protocol": "iec104.tcp"},
                            )
                        except Exception:  # pragma: no cover
                            _logger.debug("frame sink raised on rx", exc_info=True)
                    self._handle_apdu(apdu)
        finally:
            self._connected.clear()
            self._started.clear()
            with self._state_lock:
                for req in self._pending:
                    req.event.set()
                self._pending.clear()

    def _tick_timers(self) -> None:
        now = time.monotonic()
        # t3 idle: send TESTFR if no traffic for t3 and no test outstanding.
        if (
            self._started.is_set()
            and not self._test_outstanding
            and now - self._last_tx_time >= self._cfg.t3
            and now - self._last_rx_time >= self._cfg.t3
        ):
            try:
                self._send_u(UType.TESTFR_ACT)
            except ConnectionFailure:
                return
        # t1: TESTFR or oldest unacked I-frame must be confirmed within t1.
        if self._test_outstanding and now - self._test_sent_time > self._cfg.t1:
            _logger.warning("TESTFR not confirmed within t1; closing")
            self._stop.set()
            return
        with self._state_lock:
            outstanding = (self._ns - self._ack_sent) & apci.SEQ_MASK
            if outstanding > 0 and now - self._oldest_unacked_send_time > self._cfg.t1:
                _logger.warning("Oldest I-frame unacked > t1 (%ds); closing", int(self._cfg.t1))
                self._stop.set()
                return
        # t2: ack received I-frames if w threshold reached or t2 elapsed.
        with self._state_lock:
            need_s = self._unacked_received >= self._cfg.w or (
                self._unacked_received > 0 and now - self._last_rx_time >= self._cfg.t2
            )
        if need_s:
            try:
                self._send_s()
            except ConnectionFailure:
                return

    def _handle_apdu(self, frame: Apdu) -> None:
        self._last_rx_time = time.monotonic()
        if frame.fmt is FrameFormat.U:
            self._handle_u(frame.utype)
            return
        if frame.fmt is FrameFormat.S:
            self._record_ack(frame.recv_seq or 0)
            return
        # I-format
        # Update receive-side seq num and flow accounting.
        with self._state_lock:
            expected = self._nr
            if frame.send_seq != expected:
                _logger.warning(
                    "out-of-sequence I-frame: got N(S)=%d expected %d", frame.send_seq, expected
                )
                # Per spec, mismatched N(S) -> close. We just stop the loop.
                self._stop.set()
                return
            self._nr = seq_inc(self._nr)
            self._unacked_received += 1
        self._record_ack(frame.recv_seq or 0)
        # Decode ASDU and dispatch.
        try:
            asdu_obj = asdu.decode_asdu(frame.asdu)
        except ProtoSkipperError as exc:
            _logger.warning("undecodable ASDU dropped: %s", exc)
            return
        self._dispatch_asdu(asdu_obj)

    def _record_ack(self, peer_nr: int) -> None:
        with self._state_lock:
            self._ack_sent = peer_nr & apci.SEQ_MASK
            outstanding = (self._ns - self._ack_sent) & apci.SEQ_MASK
            if outstanding > 0:
                self._oldest_unacked_send_time = time.monotonic()

    def _handle_u(self, utype: UType | None) -> None:
        if utype is None:
            return
        if utype is UType.STARTDT_CON:
            self._started.set()
        elif utype is UType.STARTDT_ACT:
            # Master should not receive this; respond defensively.
            self._send_u(UType.STARTDT_CON)
        elif utype is UType.STOPDT_ACT:
            self._send_u(UType.STOPDT_CON)
            self._started.clear()
        elif utype is UType.STOPDT_CON:
            self._started.clear()
        elif utype is UType.TESTFR_ACT:
            self._send_u(UType.TESTFR_CON)
        elif utype is UType.TESTFR_CON:
            self._test_outstanding = False

    def _dispatch_asdu(self, a: Asdu) -> None:
        # First, check pending request matchers.
        completed: list[PendingRequest] = []
        matched = False
        with self._state_lock:
            for req in self._pending:
                if req.predicate(a):
                    req.replies.append(a)
                    if req.multi:
                        if req.terminator is not None and req.terminator(a):
                            req.event.set()
                            completed.append(req)
                    else:
                        req.event.set()
                        completed.append(req)
                    matched = True
                    break
            # Drain completed pendings while we still hold the lock so that a
            # follow-up command on the same IOA does not collide with a stale
            # entry whose event is already set.
            for req in completed:
                if req in self._pending:
                    self._pending.remove(req)
        if matched:
            return
        # Spontaneous / unsolicited
        if self._spont_listener is not None:
            try:
                self._spont_listener(a)
            except Exception:  # pragma: no cover - listener failures are non-fatal
                _logger.debug("spontaneous listener raised", exc_info=True)

    def _cancel_pending(self, req: PendingRequest) -> None:
        with self._state_lock:
            if req in self._pending:
                self._pending.remove(req)
