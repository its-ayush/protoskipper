# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Audit metadata coverage for IEC 60870-5-104 writes (P4 task #4)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from protoskipper.builtin_drivers.iec104.asdu import TypeID
from protoskipper.builtin_drivers.iec104.driver import Iec104TcpDriver
from protoskipper.builtin_drivers.iec104.slave import (
    Iec104SlaveServer,
    SlaveConfig,
    SlavePoint,
)
from protoskipper.core.driver import (
    DeviceRef,
    SessionProfile,
)
from protoskipper.core.session import open_session


@pytest.fixture
def slave() -> Iterator[Iec104SlaveServer]:
    srv = Iec104SlaveServer(SlaveConfig(host="127.0.0.1", port=0, ca=1))
    srv.add_points(
        [
            SlavePoint(ioa=2001, type_id=TypeID.M_SP_NA_1, value=False),
            SlavePoint(ioa=4001, type_id=TypeID.M_ME_NC_1, value=10.0),
        ]
    )
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _read_audit_rows(audit_path: Path) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    with sqlite3.connect(str(audit_path)) as conn:
        cur = conn.execute("SELECT event, payload FROM audit_log ORDER BY seq")
        for event, payload in cur.fetchall():
            rows.append((event, json.loads(payload)))
    return rows


def test_audit_records_asdu_type_and_cot_for_writes(
    tmp_path: Path, slave: Iec104SlaveServer
) -> None:
    driver = Iec104TcpDriver()
    device = DeviceRef(
        protocol="iec104.tcp",
        address=f"127.0.0.1:{slave.port}/ca={slave.ca}",
    )
    sess = open_session(
        driver,
        device,
        profile=SessionProfile.LAB,
        operator="tester",
        audit_dir=tmp_path,
        confirm=lambda intent, profile: True,
    )
    try:
        # Trigger a write so audit has write_authorization + write_committed.
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(device=device, object_id="C_SC_NA_1:2001", data_type="boolean")
        intent = sess.driver_session.prepare_write(ref, True)
        # intent.metadata must surface the asdu_type and cot keys.
        assert intent.metadata.get("asdu_type") == "C_SC_NA_1"
        assert intent.metadata.get("cot") == "ACT"
        result = sess.driver_session.commit_write(intent)
        assert result.success is True
        # Result.metadata must also expose the reply.
        assert result.metadata.get("reply_asdu_type") == "C_SC_NA_1"
        assert result.metadata.get("reply_cot") == "ACTCON"
        assert result.metadata.get("asdu_type") == "C_SC_NA_1"
    finally:
        sess.close()
    audit_files = list(tmp_path.glob("*.audit.sqlite"))
    assert audit_files, "expected an audit file"
    rows = _read_audit_rows(audit_files[0])
    events = [e for e, _ in rows]
    # write_authorization + write_committed both present.
    assert "write_authorization" in events
    assert "write_committed" in events
    # write_authorization payload's intent.metadata has asdu_type + cot.
    auth = next(p for e, p in rows if e == "write_authorization")
    assert auth["intent"]["metadata"]["asdu_type"] == "C_SC_NA_1"
    assert auth["intent"]["metadata"]["cot"] == "ACT"
    # write_committed has the reply outcome including reply_cot/reply_asdu_type.
    committed = next(p for e, p in rows if e == "write_committed")
    outcome = committed["outcome"]
    assert outcome["asdu_type"] == "C_SC_NA_1"
    assert outcome["cot"] == "ACT"
    assert outcome["reply_asdu_type"] == "C_SC_NA_1"
    assert outcome["reply_cot"] == "ACTCON"
    assert outcome["reply_negative"] is False


def test_audit_records_reply_cot_for_negative_outcome(
    tmp_path: Path, slave: Iec104SlaveServer
) -> None:
    # Configure the slave to reject IOA=2001 commands.
    slave.set_command_handler(
        lambda req: False if req.objects and req.objects[0].ioa == 2001 else None
    )
    driver = Iec104TcpDriver()
    device = DeviceRef(
        protocol="iec104.tcp",
        address=f"127.0.0.1:{slave.port}/ca={slave.ca}",
    )
    sess = open_session(
        driver,
        device,
        profile=SessionProfile.LAB,
        operator="tester",
        audit_dir=tmp_path,
        confirm=lambda intent, profile: True,
    )
    try:
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(device=device, object_id="C_SC_NA_1:2001", data_type="boolean")
        intent = sess.driver_session.prepare_write(ref, True)
        result = sess.driver_session.commit_write(intent)
        assert result.success is False
        assert result.metadata.get("reply_negative") is True
    finally:
        sess.close()
    audit_files = list(tmp_path.glob("*.audit.sqlite"))
    rows = _read_audit_rows(audit_files[0])
    failed = next(p for e, p in rows if e == "write_failed")
    assert failed["outcome"]["reply_cot"] == "ACTCON"
    assert failed["outcome"]["reply_negative"] is True
