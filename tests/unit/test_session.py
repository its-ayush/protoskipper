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
    SafetyContext,
    SessionProfile,
    WriteIntent,
    WriteResult,
    Quality,
    ReadResult,
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
            device=self.device, object_id="x", data_type="uint16",
            access=Access.READ_WRITE,
        )

    def read(self, ref: ObjectRef) -> ReadResult:
        from datetime import datetime, timezone
        return ReadResult(object_ref=ref, value=42, quality=Quality.GOOD,
                          timestamp=datetime.now(timezone.utc))

    def prepare_write(self, ref: ObjectRef, value: Any) -> WriteIntent:
        return WriteIntent(
            object_ref=ref, requested_value=value,
            encoded_bytes=int(value).to_bytes(2, "big"),
            description=f"set {ref.object_id} := {value}",
        )

    def commit_write(self, intent: WriteIntent) -> WriteResult:
        from datetime import datetime, timezone
        if not self.safety.require_write_authorization(intent):
            raise AuthorizationDenied("denied")
        return WriteResult(intent=intent, success=True,
                           timestamp=datetime.now(timezone.utc))

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
        drv, device,
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
        drv, device,
        profile=SessionProfile.LAB,
        operator="tester",
        audit_dir=tmp_path,
        confirm=lambda intent, profile: True,
    ) as session:
        ref = next(session.driver_session.enumerate_objects())
        intent = session.driver_session.prepare_write(ref, 99)
        result = session.driver_session.commit_write(intent)
        assert result.success
