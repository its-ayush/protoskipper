# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Verify the ProtocolDriver / DriverSession contract is enforced.

These tests are deliberately strict — the contract is the project's most
load-bearing invariant. If they pass, third-party plugins can rely on the
shape of the abstractions.
"""
from __future__ import annotations

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
    dev = DeviceRef(protocol="modbus.tcp", address="1.2.3.4:502/unit=1")
    with pytest.raises(Exception):  # FrozenInstanceError
        dev.address = "other"  # type: ignore[misc]

    obj = ObjectRef(device=dev, object_id="holding:0", data_type="uint16")
    with pytest.raises(Exception):
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
    obj = ObjectRef(device=dev, object_id="holding:0", data_type="uint16",
                    access=Access.READ_WRITE)
    return WriteIntent(
        object_ref=obj, requested_value=42,
        encoded_bytes=b"\x00\x2a", description="test",
    )
