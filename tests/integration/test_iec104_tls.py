# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""TLS transport (IEC 62351-3) integration test for IEC 60870-5-104.

Generates a self-signed CA + server cert + client cert in-test with the
``cryptography`` library, builds matching :class:`ssl.SSLContext` objects
for slave (server-side, requiring client cert) and master (client-side,
verifying the slave cert), and verifies the handshake completes and a
General Interrogation succeeds end-to-end over TLS.
"""

from __future__ import annotations

import datetime as _dt
import ssl
from collections.abc import Iterator
from pathlib import Path

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from protoskipper.builtin_drivers.iec104.asdu import COT, TypeID  # noqa: E402
from protoskipper.builtin_drivers.iec104.master import (  # noqa: E402
    Iec104MasterSession,
    MasterConfig,
)
from protoskipper.builtin_drivers.iec104.slave import (  # noqa: E402
    Iec104SlaveServer,
    SlaveConfig,
    SlavePoint,
)

# ---------------------------------------------------------------------------
# In-test PKI
# ---------------------------------------------------------------------------


def _gen_keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _build_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = _gen_keypair()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ProtoSkipper Test CA")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _issue(
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
    cn: str,
    san_dns: list[str] | None = None,
    san_ip: list[str] | None = None,
    server: bool = False,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = _gen_keypair()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = _dt.datetime.now(_dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    sans: list[x509.GeneralName] = []
    for d in san_dns or []:
        sans.append(x509.DNSName(d))
    for ip in san_ip or []:
        import ipaddress

        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    if server:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    else:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
    cert = builder.sign(ca_key, hashes.SHA256())
    return key, cert


def _write_pem(path: Path, key: rsa.RSAPrivateKey, cert: x509.Certificate) -> None:
    path.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


@pytest.fixture
def pki(tmp_path: Path) -> dict[str, Path]:
    ca_key, ca_cert = _build_ca()
    server_key, server_cert = _issue(
        ca_key, ca_cert, "iec104.test", san_ip=["127.0.0.1"], server=True
    )
    client_key, client_cert = _issue(ca_key, ca_cert, "client.test", server=False)
    paths = {
        "ca": tmp_path / "ca.pem",
        "server": tmp_path / "server.pem",
        "client": tmp_path / "client.pem",
    }
    _write_cert(paths["ca"], ca_cert)
    _write_pem(paths["server"], server_key, server_cert)
    _write_pem(paths["client"], client_key, client_cert)
    return paths


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def tls_slave(pki: dict[str, Path]) -> Iterator[Iec104SlaveServer]:
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_ctx.load_cert_chain(certfile=str(pki["server"]))
    server_ctx.load_verify_locations(cafile=str(pki["ca"]))
    server_ctx.verify_mode = ssl.CERT_REQUIRED  # mutual TLS
    srv = Iec104SlaveServer(
        SlaveConfig(host="127.0.0.1", port=0, ca=1, tls=True, tls_context=server_ctx)
    )
    srv.add_points(
        [
            SlavePoint(ioa=4001, type_id=TypeID.M_ME_NC_1, value=230.5),
        ]
    )
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def test_master_can_handshake_and_interrogate_over_tls(
    tls_slave: Iec104SlaveServer, pki: dict[str, Path]
) -> None:
    client_ctx = ssl.create_default_context(cafile=str(pki["ca"]))
    client_ctx.load_cert_chain(certfile=str(pki["client"]))
    cfg = MasterConfig(
        host="127.0.0.1",
        port=tls_slave.port,
        ca=tls_slave.ca,
        t1=3.0,
        t2=1.0,
        t3=5.0,
        tls=True,
        tls_context=client_ctx,
        tls_server_hostname="iec104.test",
    )
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        assert m.started is True
        replies = m.general_interrogation(timeout=5.0)
        assert any(r.cot is COT.ACTCON and r.type_id is TypeID.C_IC_NA_1 for r in replies)
        floats = [obj for r in replies if r.type_id is TypeID.M_ME_NC_1 for obj in r.objects]
        ioas = {obj.ioa: obj.value for obj in floats}
        assert ioas[4001] == pytest.approx(230.5)
    finally:
        m.close()


def test_master_without_client_cert_is_rejected(
    tls_slave: Iec104SlaveServer, pki: dict[str, Path]
) -> None:
    # No client cert — server requires CERT_REQUIRED, so handshake must fail.
    client_ctx = ssl.create_default_context(cafile=str(pki["ca"]))
    cfg = MasterConfig(
        host="127.0.0.1",
        port=tls_slave.port,
        ca=tls_slave.ca,
        t1=2.0,
        t2=1.0,
        t3=5.0,
        tls=True,
        tls_context=client_ctx,
        tls_server_hostname="iec104.test",
    )
    m = Iec104MasterSession(cfg)
    from protoskipper.core.errors import ConnectionFailure

    with pytest.raises(ConnectionFailure):
        m.connect()
    m.close()
