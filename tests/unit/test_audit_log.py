# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Audit log roundtrip + tamper-detection tests.

Tampering is detected by the verify_log helper; these tests confirm that
clean logs verify cleanly, and that an in-place edit is caught.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from protoskipper.core.audit import AuditLog, verify_log


def test_audit_log_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "session.audit.sqlite"

    with AuditLog.create(db, operator="kuldeep@example.com", profile="lab") as audit:
        audit.record(event="connect", protocol="modbus.tcp", address="10.0.0.5:502")
        audit.record(event="read", object_id="holding:0", value=230, quality="good")
        audit.record(event="write_intent", object_id="holding:0", value=235)

    ok, message = verify_log(db)
    assert ok, message
    assert "rows verified" in message


def test_audit_log_detects_tamper(tmp_path: Path) -> None:
    db = tmp_path / "session.audit.sqlite"

    with AuditLog.create(db, operator="kuldeep", profile="commissioning") as audit:
        audit.record(event="read", object_id="holding:0", value=1)
        audit.record(event="read", object_id="holding:1", value=2)

    # Edit one row's payload after the fact.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE audit_log SET payload = ? WHERE event = 'read' AND seq = "
            "(SELECT MIN(seq) FROM audit_log WHERE event = 'read')",
            ('{"object_id": "holding:0", "value": 999}',),
        )
        conn.commit()

    ok, message = verify_log(db)
    assert not ok
    assert "HMAC mismatch" in message or "chain broken" in message
