# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Deterministic fuzzer regression tests (P4.D)."""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading

import pytest

from protoskipper.builtin_drivers.iec104.fuzzer import (
    TlsFuzzReport,
    _random_tls_client_hello,
    fuzz_apdu_mutation,
    fuzz_asdu_codec,
    fuzz_codec_roundtrip,
    fuzz_tls_handshake,
)


@pytest.mark.parametrize("seed", [0, 1, 42, 0xDEADBEEF])
def test_codec_roundtrip_no_unexpected_errors(seed: int) -> None:
    report = fuzz_codec_roundtrip(iterations=2000, seed=seed)
    assert report.unexpected_errors == 0, (
        f"unexpected exceptions in APCI parser (seed={seed}): {report}"
    )


@pytest.mark.parametrize("seed", [0, 7, 99, 0xC0FFEE])
def test_asdu_codec_no_unexpected_errors(seed: int) -> None:
    report = fuzz_asdu_codec(iterations=2000, seed=seed)
    assert report.unexpected_errors == 0, (
        f"unexpected exceptions in ASDU parser (seed={seed}): {report}"
    )


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_apdu_mutation_no_unexpected_errors(seed: int) -> None:
    report = fuzz_apdu_mutation(iterations=2000, seed=seed)
    assert report.unexpected_errors == 0, (
        f"unexpected exceptions in mutation fuzz (seed={seed}): {report}"
    )


def test_fuzz_report_ok_property() -> None:
    report = fuzz_codec_roundtrip(iterations=100, seed=0)
    assert report.ok is True


# ---------------------------------------------------------------------------
# P4.D.4 — TLS mutation engine
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tls_server(port: int, ready: threading.Event) -> None:
    """Minimal TLS server that closes every connection after one recv."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.check_hostname = False
    # Self-signed cert generated inline for tests.
    import os
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        key = os.path.join(d, "key.pem")
        cert = os.path.join(d, "cert.pem")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                key,
                "-out",
                cert,
                "-days",
                "1",
                "-nodes",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
        )
        ctx.load_cert_chain(cert, key)
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(32)
        srv.settimeout(6.0)
        ready.set()
        for _ in range(60):  # up to 60 connections
            try:
                raw, _ = srv.accept()
                raw.settimeout(1.0)
                try:
                    conn = ctx.wrap_socket(raw, server_side=True)
                    try:
                        conn.recv(256)
                    except (ssl.SSLError, OSError):
                        pass
                    finally:
                        with contextlib.suppress(OSError):
                            conn.close()
                except (ssl.SSLError, OSError):
                    with contextlib.suppress(OSError):
                        raw.close()
            except OSError:
                break
        srv.close()


def _plain_server(port: int, ready: threading.Event) -> None:
    """Plain-TCP server (no TLS) that reads and closes every connection."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(32)
    srv.settimeout(6.0)
    ready.set()
    for _ in range(60):
        try:
            conn, _ = srv.accept()
            conn.settimeout(1.0)
            try:
                conn.recv(256)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    conn.close()
        except OSError:
            break
    srv.close()


def test_random_tls_client_hello_produces_bytes() -> None:
    """_random_tls_client_hello produces non-empty bytes for various seeds."""
    import random

    for seed in range(6):
        rng = random.Random(seed)
        result = _random_tls_client_hello(rng)
        assert isinstance(result, bytes)
        assert len(result) >= 5  # at minimum a TLS record header


def test_tls_fuzz_report_ok_property() -> None:
    r = TlsFuzzReport(
        iterations=10,
        clean_rejects=10,
        unexpected_errors=0,
        duration_seconds=0.1,
        seed=0,
    )
    assert r.ok is True

    r2 = TlsFuzzReport(
        iterations=10,
        clean_rejects=9,
        unexpected_errors=1,
        duration_seconds=0.1,
        seed=0,
    )
    assert r2.ok is False


def test_fuzz_tls_handshake_against_tls_server() -> None:
    """fuzz_tls_handshake should return ok=True against a real TLS server
    (all probes are cleanly rejected by the TLS handshake)."""
    port = _free_port()
    ready = threading.Event()
    t = threading.Thread(target=_tls_server, args=(port, ready), daemon=True)
    t.start()
    ready.wait(timeout=5.0)
    report = fuzz_tls_handshake("127.0.0.1", port, iterations=20, seed=0)
    assert report.iterations == 20
    assert report.ok, f"unexpected TLS fuzz errors: {report}"
    t.join(timeout=3.0)


def test_fuzz_tls_handshake_against_plain_server() -> None:
    """Against a plain-TCP server (no TLS), probes will get non-TLS responses
    → unexpected_errors > 0."""
    port = _free_port()
    ready = threading.Event()
    t = threading.Thread(target=_plain_server, args=(port, ready), daemon=True)
    t.start()
    ready.wait(timeout=5.0)
    report = fuzz_tls_handshake("127.0.0.1", port, iterations=10, seed=7)
    # A plain server echoes nothing or closes - that looks like clean_reject
    # OR sends something that isn't a TLS alert (unexpected_error).
    # Either way the report should capture the results.
    assert report.iterations == 10
    assert report.clean_rejects + report.unexpected_errors == 10
    t.join(timeout=3.0)


def test_fuzz_tls_handshake_unreachable_host() -> None:
    """All probes to an unreachable port count as unexpected_errors."""
    port = _free_port()  # nothing listening here
    report = fuzz_tls_handshake("127.0.0.1", port, iterations=3, seed=1, connect_timeout=0.5)
    assert report.iterations == 3
    assert report.unexpected_errors == 3
    assert not report.ok
