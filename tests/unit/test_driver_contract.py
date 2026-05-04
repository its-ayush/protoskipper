# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Verify the ProtocolDriver / DriverSession contract is enforced.

These tests are deliberately strict — the contract is the project's most
load-bearing invariant. If they pass, third-party plugins can rely on the
shape of the abstractions.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from protoskipper.core.driver import (
    Access,
    DeviceRef,
    DriverSession,
    ObjectRef,
    ProtocolDriver,
    Quality,
    SafetyContext,
    SessionProfile,
    WriteIntent,
    WriteResult,
)


def test_protocol_driver_is_abstract() -> None:
    with pytest.raises(TypeError):
        ProtocolDriver()  # type: ignore[abstract]


def test_driver_session_is_abstract() -> None:
    with pytest.raises(TypeError):
        DriverSession()  # type: ignore[abstract]


def test_session_profile_values_are_stable() -> None:
    # Public values are part of the wire-stable audit log format; do not change.
    assert {p.value for p in SessionProfile} == {"lab", "commissioning", "production"}


def test_quality_enum_includes_simulated() -> None:
    # Simulated must be distinguishable from GOOD; the GUI colours it differently.
    assert Quality.SIMULATED.value == "simulated"
    assert Quality.SIMULATED is not Quality.GOOD


def test_device_and_object_refs_are_frozen() -> None:
    # frozen=True dataclasses raise FrozenInstanceError (Python 3.11+), which
    # is a subclass of AttributeError on all supported Python versions (3.10+).
    dev = DeviceRef(protocol="modbus.tcp", address="1.2.3.4:502/unit=1")
    with pytest.raises(AttributeError):
        dev.address = "other"  # type: ignore[misc]

    obj = ObjectRef(device=dev, object_id="holding:0", data_type="uint16")
    with pytest.raises(AttributeError):
        obj.object_id = "other"  # type: ignore[misc]


def test_safety_context_audits_authorization_outcomes() -> None:
    audited: list[dict] = []
    intent = _make_intent()

    safety_allow = SafetyContext(
        profile=SessionProfile.LAB,
        confirm_callback=lambda i, p: True,
        audit_callback=lambda **fields: audited.append(fields),
    )
    assert safety_allow.require_write_authorization(intent) is True

    safety_deny = SafetyContext(
        profile=SessionProfile.PRODUCTION,
        confirm_callback=lambda i, p: False,
        audit_callback=lambda **fields: audited.append(fields),
    )
    assert safety_deny.require_write_authorization(intent) is False

    # Both authorisation attempts must be in the audit trail.
    assert len(audited) == 2
    assert audited[0]["authorized"] is True
    assert audited[1]["authorized"] is False
    assert audited[1]["profile"] == SessionProfile.PRODUCTION


def _make_intent() -> WriteIntent:
    dev = DeviceRef(protocol="modbus.tcp", address="1.2.3.4:502/unit=1")
    obj = ObjectRef(device=dev, object_id="holding:0", data_type="uint16", access=Access.READ_WRITE)
    return WriteIntent(
        object_ref=obj,
        requested_value=42,
        encoded_bytes=b"\x00\x2a",
        description="test",
    )


def test_safety_context_record_write_outcome_committed() -> None:
    """record_write_outcome emits write_committed when the write succeeded."""
    audited: list[dict] = []
    intent = _make_intent()
    ts = datetime.now(timezone.utc)
    result = WriteResult(intent=intent, success=True, timestamp=ts)

    safety = SafetyContext(
        profile=SessionProfile.LAB,
        confirm_callback=lambda i, p: True,
        audit_callback=lambda **fields: audited.append(fields),
    )
    safety.require_write_authorization(intent)
    safety.record_write_outcome(result)

    assert len(audited) == 2
    assert audited[0]["event"] == "write_authorization"
    assert audited[0]["authorized"] is True
    assert audited[1]["event"] == "write_committed"
    assert audited[1]["timestamp"] == ts


def test_safety_context_record_write_outcome_failed() -> None:
    """record_write_outcome emits write_failed when the transmission failed."""
    audited: list[dict] = []
    intent = _make_intent()
    ts = datetime.now(timezone.utc)
    result = WriteResult(intent=intent, success=False, timestamp=ts, error="timeout")

    safety = SafetyContext(
        profile=SessionProfile.LAB,
        confirm_callback=lambda i, p: True,
        audit_callback=lambda **fields: audited.append(fields),
    )
    safety.require_write_authorization(intent)
    safety.record_write_outcome(result)

    assert len(audited) == 2
    assert audited[1]["event"] == "write_failed"
    assert audited[1]["error"] == "timeout"


def test_denied_write_has_no_outcome_row() -> None:
    """When authorisation is denied, record_write_outcome must NOT be called;
    the audit trail contains only the write_authorization(authorized=False) row."""
    audited: list[dict] = []
    intent = _make_intent()

    safety = SafetyContext(
        profile=SessionProfile.PRODUCTION,
        confirm_callback=lambda i, p: False,
        audit_callback=lambda **fields: audited.append(fields),
    )
    result = safety.require_write_authorization(intent)

    assert result is False
    assert len(audited) == 1
    assert audited[0]["authorized"] is False
    # Confirm that only one row exists — no spurious write_committed or write_failed.
    events = [r["event"] for r in audited]
    assert "write_committed" not in events
    assert "write_failed" not in events


def test_record_event_routes_to_audit_callback() -> None:
    """SafetyContext.record_event() appends an audit row for non-write events."""
    audited: list[dict] = []

    safety = SafetyContext(
        profile=SessionProfile.LAB,
        confirm_callback=lambda i, p: True,
        audit_callback=lambda **fields: audited.append(fields),
    )
    safety.record_event("iec61850_browse", subevent="enumerate_objects", target="10.0.0.1:102")

    assert len(audited) == 1
    row = audited[0]
    assert row["event"] == "iec61850_browse"
    assert row["subevent"] == "enumerate_objects"
    assert row["target"] == "10.0.0.1:102"


def test_record_event_does_not_require_write_fields() -> None:
    """record_event() accepts arbitrary keyword args without the write-intent fields."""
    audited: list[dict] = []
    safety = SafetyContext(
        profile=SessionProfile.LAB,
        confirm_callback=lambda i, p: True,
        audit_callback=lambda **fields: audited.append(fields),
    )
    safety.record_event("custom_event", foo="bar", count=5)

    assert audited[0]["event"] == "custom_event"
    assert audited[0]["foo"] == "bar"
    assert audited[0]["count"] == 5
