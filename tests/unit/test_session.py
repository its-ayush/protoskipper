# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Session lifecycle test: open, deny a write, close, verify audit chain."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from protoskipper.core.audit import verify_log
from protoskipper.core.driver import (
    Access,
    DeviceRef,
    DriverSession,
    ObjectRef,
    ProtocolDriver,
    Quality,
    ReadResult,
    SafetyContext,
    SessionProfile,
    WriteIntent,
    WriteResult,
)
from protoskipper.core.errors import AuthorizationDenied
from protoskipper.core.session import open_session

# ---------------------------------------------------------------------------
# Minimal in-memory driver, used as a stand-in for any real protocol driver.
# ---------------------------------------------------------------------------


class _FakeSession(DriverSession):
    def __init__(self, device: DeviceRef, safety: SafetyContext) -> None:
        self.device = device
        self.safety = safety
        self.closed = False

    def enumerate_objects(self) -> Iterator[ObjectRef]:
        yield ObjectRef(
            device=self.device,
            object_id="x",
            data_type="uint16",
            access=Access.READ_WRITE,
        )

    def read(self, ref: ObjectRef) -> ReadResult:
        from datetime import datetime, timezone

        return ReadResult(
            object_ref=ref, value=42, quality=Quality.GOOD, timestamp=datetime.now(timezone.utc)
        )

    def prepare_write(self, ref: ObjectRef, value: Any) -> WriteIntent:
        return WriteIntent(
            object_ref=ref,
            requested_value=value,
            encoded_bytes=int(value).to_bytes(2, "big"),
            description=f"set {ref.object_id} := {value}",
        )

    def commit_write(self, intent: WriteIntent) -> WriteResult:
        from datetime import datetime, timezone

        if not self.safety.require_write_authorization(intent):
            raise AuthorizationDenied("denied")
        result = WriteResult(intent=intent, success=True, timestamp=datetime.now(timezone.utc))
        self.safety.record_write_outcome(result)
        return result

    def close(self) -> None:
        self.closed = True


class _FakeDriver(ProtocolDriver):
    PROTOCOL_ID: ClassVar[str] = "fake.proto"
    DISPLAY_NAME: ClassVar[str] = "Fake Protocol"

    def discover(self, target: str) -> Iterator[DeviceRef]:
        yield DeviceRef(protocol=self.PROTOCOL_ID, address=target)

    def connect(self, device: DeviceRef, safety: SafetyContext) -> DriverSession:
        return _FakeSession(device, safety)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_session_default_confirm_denies_writes(tmp_path: Path) -> None:
    """The default confirm callback is deny-by-default; ensures a script that
    forgot to wire up confirmation cannot accidentally write."""
    drv = _FakeDriver()
    device = DeviceRef(protocol=drv.PROTOCOL_ID, address="lab1")

    with open_session(
        drv,
        device,
        profile=SessionProfile.LAB,
        operator="tester",
        audit_dir=tmp_path,
    ) as session:
        ref = next(session.driver_session.enumerate_objects())
        intent = session.driver_session.prepare_write(ref, 99)
        with pytest.raises(AuthorizationDenied):
            session.driver_session.commit_write(intent)

    # One audit log file should exist and verify cleanly even though a write
    # was denied — denial events are part of the chain.
    logs = list(tmp_path.glob("*.audit.sqlite"))
    assert len(logs) == 1
    ok, message = verify_log(logs[0])
    assert ok, message


def test_session_with_explicit_confirm_allows_writes(tmp_path: Path) -> None:
    drv = _FakeDriver()
    device = DeviceRef(protocol=drv.PROTOCOL_ID, address="lab1")

    with open_session(
        drv,
        device,
        profile=SessionProfile.LAB,
        operator="tester",
        audit_dir=tmp_path,
        confirm=lambda intent, profile: True,
    ) as session:
        ref = next(session.driver_session.enumerate_objects())
        intent = session.driver_session.prepare_write(ref, 99)
        result = session.driver_session.commit_write(intent)
        assert result.success


def test_successful_write_produces_write_committed_audit_row(tmp_path: Path) -> None:
    """After a successful write, the audit log must contain a write_committed
    row that follows the write_authorization row.  The absence of this row
    means the audit log provides only authorisation evidence — not proof of
    transmission — which is insufficient for a commissioning audit trail."""
    import sqlite3

    drv = _FakeDriver()
    device = DeviceRef(protocol=drv.PROTOCOL_ID, address="lab1")

    with open_session(
        drv,
        device,
        profile=SessionProfile.LAB,
        operator="tester",
        audit_dir=tmp_path,
        confirm=lambda intent, profile: True,
    ) as session:
        ref = next(session.driver_session.enumerate_objects())
        intent = session.driver_session.prepare_write(ref, 42)
        result = session.driver_session.commit_write(intent)
        assert result.success

    log = next(tmp_path.glob("*.audit.sqlite"))
    ok, msg = verify_log(log)
    assert ok, msg

    with sqlite3.connect(log) as conn:
        rows = conn.execute("SELECT event FROM audit_log ORDER BY seq").fetchall()
    events = [r[0] for r in rows]

    # write_authorization must appear before write_committed.
    assert "write_authorization" in events
    assert "write_committed" in events
    auth_idx = events.index("write_authorization")
    committed_idx = events.index("write_committed")
    assert auth_idx < committed_idx, (
        f"write_authorization (seq {auth_idx}) must precede write_committed (seq {committed_idx})"
    )


def test_denied_write_has_no_write_committed_row(tmp_path: Path) -> None:
    """When the operator denies a write, no bytes are transmitted and
    therefore no write_committed or write_failed row must appear — only
    the write_authorization(authorized=False) row."""
    import sqlite3

    drv = _FakeDriver()
    device = DeviceRef(protocol=drv.PROTOCOL_ID, address="lab1")

    with open_session(
        drv,
        device,
        profile=SessionProfile.LAB,
        operator="tester",
        audit_dir=tmp_path,
    ) as session:
        ref = next(session.driver_session.enumerate_objects())
        intent = session.driver_session.prepare_write(ref, 99)
        with pytest.raises(AuthorizationDenied):
            session.driver_session.commit_write(intent)

    log = next(tmp_path.glob("*.audit.sqlite"))
    ok, msg = verify_log(log)
    assert ok, msg

    with sqlite3.connect(log) as conn:
        rows = conn.execute("SELECT event FROM audit_log ORDER BY seq").fetchall()
    events = [r[0] for r in rows]

    assert "write_authorization" in events
    assert "write_committed" not in events
    assert "write_failed" not in events


# ---------------------------------------------------------------------------
# P1.F.1 — audit_reads flag
# ---------------------------------------------------------------------------


def test_reads_not_audited_by_default(tmp_path: Path) -> None:
    """Reads must NOT appear in the audit log unless audit_reads=True."""
    import sqlite3

    drv = _FakeDriver()
    device = DeviceRef(protocol=drv.PROTOCOL_ID, address="lab1")

    with open_session(
        drv,
        device,
        profile=SessionProfile.LAB,
        operator="tester",
        audit_dir=tmp_path,
    ) as session:
        ref = next(session.driver_session.enumerate_objects())
        session.driver_session.read(ref)

    log = next(tmp_path.glob("*.audit.sqlite"))
    with sqlite3.connect(log) as conn:
        events = [r[0] for r in conn.execute("SELECT event FROM audit_log ORDER BY seq").fetchall()]

    assert "read_completed" not in events


def test_reads_audited_when_flag_set(tmp_path: Path) -> None:
    """Every read must appear in the audit chain when audit_reads=True."""
    import sqlite3

    drv = _FakeDriver()
    device = DeviceRef(protocol=drv.PROTOCOL_ID, address="lab1")

    with open_session(
        drv,
        device,
        profile=SessionProfile.LAB,
        operator="tester",
        audit_dir=tmp_path,
        audit_reads=True,
    ) as session:
        ref = next(session.driver_session.enumerate_objects())
        session.driver_session.read(ref)
        session.driver_session.read(ref)

    log = next(tmp_path.glob("*.audit.sqlite"))
    ok, msg = verify_log(log)
    assert ok, msg

    with sqlite3.connect(log) as conn:
        events = [r[0] for r in conn.execute("SELECT event FROM audit_log ORDER BY seq").fetchall()]

    assert events.count("read_completed") == 2


def test_audit_reads_verify_log_clean_either_way(tmp_path: Path) -> None:
    """verify_log must pass regardless of the audit_reads setting."""
    drv = _FakeDriver()
    device = DeviceRef(protocol=drv.PROTOCOL_ID, address="lab1")

    for flag in (False, True):
        sub = tmp_path / str(flag)
        sub.mkdir()
        with open_session(
            drv,
            device,
            profile=SessionProfile.LAB,
            operator="tester",
            audit_dir=sub,
            audit_reads=flag,
        ) as session:
            ref = next(session.driver_session.enumerate_objects())
            session.driver_session.read(ref)

        log = next(sub.glob("*.audit.sqlite"))
        ok, msg = verify_log(log)
        assert ok, f"audit_reads={flag}: {msg}"
