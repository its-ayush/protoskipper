# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for P4.B.9 — auto-reconnect on disconnect.

Uses lightweight in-process TCP servers (via threading + socket) rather than
the full Iec104SlaveServer so the tests finish fast and have no external
deps.  Each test wires up a "mini-slave" that answers STARTDT_ACT with
STARTDT_CON and then deliberately closes the connection, then verifies that
the master reconnects and re-enters the STARTDT sequence.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time

from protoskipper.builtin_drivers.iec104.apci import (
    FrameFormat,
    UType,
    build_u_frame,
    parse_apdu,
    peek_apdu_length,
)
from protoskipper.builtin_drivers.iec104.master import Iec104MasterSession, MasterConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_one_apdu(conn: socket.socket) -> bytes:
    """Blocking: read exactly one APDU from *conn*."""
    buf = bytearray()
    conn.settimeout(3.0)
    while True:
        chunk = conn.recv(256)
        if not chunk:
            raise OSError("peer closed")
        buf.extend(chunk)
        if len(buf) < 2:
            continue
        total = peek_apdu_length(bytes(buf[:2]))
        if total is None:
            continue
        while len(buf) < total:
            buf.extend(conn.recv(256))
        return bytes(buf[:total])


def _mini_slave(port: int, events: list[str], close_after_startdt: bool = False) -> None:
    """Accept one connection, do STARTDT handshake, optionally close after a short delay."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    srv.settimeout(5.0)
    conn, _ = srv.accept()
    srv.close()
    try:
        frame = _read_one_apdu(conn)
        apdu = parse_apdu(frame)
        assert apdu.fmt is FrameFormat.U and apdu.utype is UType.STARTDT_ACT
        events.append("startdt_act")
        conn.sendall(build_u_frame(UType.STARTDT_CON))
        events.append("startdt_con_sent")
        if close_after_startdt:
            # Small pause so connect() can observe _started before the finally clears it.
            time.sleep(0.15)
            conn.close()
            events.append("closed")
    except Exception:
        pass
    finally:
        with contextlib.suppress(Exception):
            conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reconnect_disabled_does_not_retry() -> None:
    """auto_reconnect=False: after peer close the master session stays closed."""
    port = _free_port()
    events: list[str] = []

    def slave() -> None:
        _mini_slave(port, events, close_after_startdt=True)

    t = threading.Thread(target=slave, daemon=True)
    t.start()
    time.sleep(0.05)

    cfg = MasterConfig(
        host="127.0.0.1",
        port=port,
        t0=3.0,
        t1=3.0,
        t2=1.0,
        t3=60.0,
        auto_reconnect=False,
    )
    m = Iec104MasterSession(cfg)
    m.connect()
    assert m.started

    # Wait for the slave to close its end
    t.join(timeout=2.0)
    # Give master's rx thread time to notice the close
    time.sleep(0.3)

    # started should be cleared
    assert not m.started
    m.close()


def test_reconnect_enabled_retries_on_peer_close() -> None:
    """auto_reconnect=True: master reconnects after peer closes the connection."""
    port = _free_port()
    # Two rounds of accept: first close immediately, second stay open.
    connect_count = 0
    ready = threading.Event()
    second_ready = threading.Event()

    def two_round_slave() -> None:
        nonlocal connect_count
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(2)
        srv.settimeout(6.0)
        ready.set()
        for close_it in [True, False]:
            conn, _ = srv.accept()
            connect_count += 1
            try:
                frame = _read_one_apdu(conn)
                apdu = parse_apdu(frame)
                assert apdu.fmt is FrameFormat.U and apdu.utype is UType.STARTDT_ACT
                conn.sendall(build_u_frame(UType.STARTDT_CON))
                if close_it:
                    # Small pause so connect() can observe _started before it's cleared.
                    time.sleep(0.15)
                    conn.close()
                else:
                    second_ready.set()
                    # stay open for 3s so master can run
                    time.sleep(3.0)
                    conn.close()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.close()
        srv.close()

    t = threading.Thread(target=two_round_slave, daemon=True)
    t.start()
    ready.wait(timeout=2.0)

    cfg = MasterConfig(
        host="127.0.0.1",
        port=port,
        t0=3.0,
        t1=3.0,
        t2=1.0,
        t3=60.0,
        auto_reconnect=True,
        reconnect_delay=0.2,  # short for tests
    )
    m = Iec104MasterSession(cfg)
    m.connect()
    assert m.started
    assert connect_count == 1

    # Wait for second connection
    second_ready.wait(timeout=4.0)
    assert second_ready.is_set(), "master did not reconnect within timeout"
    assert connect_count == 2
    # Give master time to process STARTDT_CON from the second connection.
    time.sleep(0.15)
    assert m.started


def test_reconnect_resets_sequence_numbers() -> None:
    """After reconnect, N(S)/N(R) are reset to 0 (fresh session)."""
    port = _free_port()
    ready = threading.Event()
    second_started = threading.Event()

    def slave() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(2)
        srv.settimeout(6.0)
        ready.set()
        for close_it in [True, False]:
            conn, _ = srv.accept()
            conn.settimeout(3.0)
            try:
                frame = _read_one_apdu(conn)
                apdu = parse_apdu(frame)
                assert apdu.utype is UType.STARTDT_ACT
                conn.sendall(build_u_frame(UType.STARTDT_CON))
                if close_it:
                    conn.close()
                else:
                    second_started.set()
                    time.sleep(3.0)
                    conn.close()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.close()
        srv.close()

    t = threading.Thread(target=slave, daemon=True)
    t.start()
    ready.wait(timeout=2.0)

    cfg = MasterConfig(
        host="127.0.0.1",
        port=port,
        t0=3.0,
        t1=3.0,
        t2=1.0,
        t3=60.0,
        auto_reconnect=True,
        reconnect_delay=0.1,
    )
    m = Iec104MasterSession(cfg)
    m.connect()
    # After reconnect the sequence counters must be 0.
    second_started.wait(timeout=4.0)
    assert second_started.is_set()
    # Give master time to process STARTDT_CON
    time.sleep(0.15)
    assert m._ns == 0
    assert m._nr == 0

    m.close()
    t.join(timeout=2.0)


def test_reconnect_fails_pending_requests() -> None:
    """When the connection drops, pending requests get their events set so
    callers unblock with an empty reply list (ConnectionFailure)."""
    port = _free_port()
    ready = threading.Event()

    def slave_close() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(5.0)
        ready.set()
        conn, _ = srv.accept()
        frame = _read_one_apdu(conn)
        apdu = parse_apdu(frame)
        assert apdu.utype is UType.STARTDT_ACT
        conn.sendall(build_u_frame(UType.STARTDT_CON))
        time.sleep(0.1)
        conn.close()
        srv.close()

    t = threading.Thread(target=slave_close, daemon=True)
    t.start()
    ready.wait(timeout=2.0)

    cfg = MasterConfig(
        host="127.0.0.1",
        port=port,
        t0=3.0,
        t1=3.0,
        t2=1.0,
        t3=60.0,
        auto_reconnect=False,
    )
    m = Iec104MasterSession(cfg)
    m.connect()
    # Manually inject a pending request that will never be answered.
    import threading as _th

    ev = _th.Event()
    from protoskipper.builtin_drivers.iec104.master import PendingRequest

    req = PendingRequest(predicate=lambda _: False, replies=[], event=ev)
    with m._state_lock:
        m._pending.append(req)

    # Slave closes; the rx thread should fail the pending.
    t.join(timeout=2.0)
    ev.wait(timeout=2.0)
    assert ev.is_set(), "pending request not unblocked after disconnect"
    m.close()


def test_no_reconnect_after_explicit_close() -> None:
    """Calling close() must stop the reconnect loop even if auto_reconnect=True."""
    port = _free_port()
    ready = threading.Event()

    def slave() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(5.0)
        ready.set()
        conn, _ = srv.accept()
        conn.settimeout(3.0)
        frame = _read_one_apdu(conn)
        apdu = parse_apdu(frame)
        assert apdu.utype is UType.STARTDT_ACT
        conn.sendall(build_u_frame(UType.STARTDT_CON))
        # Stay connected until master closes
        try:
            while True:
                chunk = conn.recv(256)
                if not chunk:
                    break
        except OSError:
            pass
        conn.close()
        srv.close()

    t = threading.Thread(target=slave, daemon=True)
    t.start()
    ready.wait(timeout=2.0)

    cfg = MasterConfig(
        host="127.0.0.1",
        port=port,
        t0=3.0,
        t1=3.0,
        t2=1.0,
        t3=60.0,
        auto_reconnect=True,
        reconnect_delay=0.1,
    )
    m = Iec104MasterSession(cfg)
    m.connect()
    assert m.started

    m.close()
    # Give rx thread time to stop
    time.sleep(0.3)
    assert not m.started
    # rx_thread should be dead
    assert m._rx_thread is None or not m._rx_thread.is_alive()
    t.join(timeout=2.0)
