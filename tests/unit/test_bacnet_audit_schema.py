# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for P7.I.1: BACnet audit-log row schema (audit_schema.py)."""

from __future__ import annotations

import pytest

from protoskipper.builtin_drivers.bacnet.audit_schema import (
    AUDIT_SCHEMA_VERSION,
    BacnetEvent,
    alarm_ack_payload,
    alarm_payload,
    connect_payload,
    cov_subscribe_payload,
    device_mgmt_payload,
    discovery_payload,
    error_payload,
    read_payload,
    read_range_payload,
    routing_payload,
    write_payload,
)

# ---------------------------------------------------------------------------
# BacnetEvent constants
# ---------------------------------------------------------------------------


class TestBacnetEventConstants:
    def test_all_constants_are_strings(self) -> None:
        for name in dir(BacnetEvent):
            if name.startswith("_"):
                continue
            val = getattr(BacnetEvent, name)
            assert isinstance(val, str), f"BacnetEvent.{name} should be str"

    def test_all_constants_have_bacnet_prefix(self) -> None:
        for name in dir(BacnetEvent):
            if name.startswith("_"):
                continue
            val = getattr(BacnetEvent, name)
            assert val.startswith("bacnet."), (
                f"BacnetEvent.{name} = {val!r} must start with 'bacnet.'"
            )

    def test_no_duplicate_values(self) -> None:
        values = [getattr(BacnetEvent, n) for n in dir(BacnetEvent) if not n.startswith("_")]
        assert len(values) == len(set(values)), "Duplicate event type constants detected"

    def test_key_events_exist(self) -> None:
        assert BacnetEvent.CONNECT == "bacnet.connect"
        assert BacnetEvent.WRITE == "bacnet.write"
        assert BacnetEvent.ALARM_RECEIVED == "bacnet.alarm_received"
        assert BacnetEvent.READ_BDT == "bacnet.read_bdt"


class TestSchemaVersion:
    def test_is_integer(self) -> None:
        assert isinstance(AUDIT_SCHEMA_VERSION, int)

    def test_is_positive(self) -> None:
        assert AUDIT_SCHEMA_VERSION >= 1


# ---------------------------------------------------------------------------
# connect_payload
# ---------------------------------------------------------------------------


class TestConnectPayload:
    def test_required_fields_present(self) -> None:
        p = connect_payload(1234, "192.168.1.10:47808")
        assert p["schema_version"] == AUDIT_SCHEMA_VERSION
        assert p["device_id"] == 1234
        assert p["address"] == "192.168.1.10:47808"
        assert p["vendor_profile"] == "generic"

    def test_optional_fields_included_when_given(self) -> None:
        p = connect_payload(
            9999,
            "10.0.0.1:47808",
            vendor_id=5,
            max_apdu=1476,
            segmentation="both",
            vendor_profile="jci_metasys_nae",
        )
        assert p["vendor_id"] == 5
        assert p["max_apdu"] == 1476
        assert p["segmentation"] == "both"
        assert p["vendor_profile"] == "jci_metasys_nae"

    def test_optional_fields_absent_when_not_given(self) -> None:
        p = connect_payload(None, "10.0.0.1")
        assert "vendor_id" not in p
        assert "max_apdu" not in p
        assert "segmentation" not in p

    def test_device_id_none_allowed(self) -> None:
        p = connect_payload(None, "10.0.0.1")
        assert p["device_id"] is None


# ---------------------------------------------------------------------------
# read_payload
# ---------------------------------------------------------------------------


class TestReadPayload:
    def test_minimal(self) -> None:
        p = read_payload(1234, "analog-value:1", "present-value")
        assert p["device_id"] == 1234
        assert p["object_id"] == "analog-value:1"
        assert p["property"] == "present-value"
        assert p["quality"] == "good"
        assert "value" not in p
        assert "rtt_ms" not in p

    def test_with_value_and_rtt(self) -> None:
        p = read_payload(1234, "AV:1", "present-value", value=21.5, rtt_ms=18.7)
        assert p["value"] == 21.5
        assert abs(p["rtt_ms"] - 18.7) < 0.01

    def test_rtt_rounded_to_three_decimals(self) -> None:
        p = read_payload(1, "AV:1", "pv", rtt_ms=1.23456789)
        assert p["rtt_ms"] == pytest.approx(1.235, abs=0.001)


# ---------------------------------------------------------------------------
# write_payload
# ---------------------------------------------------------------------------


class TestWritePayload:
    def test_required_fields(self) -> None:
        p = write_payload(1234, "AO:1", "present-value", 21.5, 8)
        assert p["device_id"] == 1234
        assert p["object_id"] == "AO:1"
        assert p["property"] == "present-value"
        assert p["value"] == 21.5
        assert p["priority"] == 8
        assert p["success"] is True
        assert p["service"] == "WriteProperty"

    def test_failure_with_error_message(self) -> None:
        p = write_payload(
            1234, "AO:1", "present-value", 21.5, 8, success=False, error="BACnet Reject"
        )
        assert p["success"] is False
        assert p["error"] == "BACnet Reject"

    def test_wpm_service_label(self) -> None:
        p = write_payload(1, "AV:1", "pv", 0.0, None, service="WritePropertyMultiple")
        assert p["service"] == "WritePropertyMultiple"

    def test_null_priority_allowed(self) -> None:
        p = write_payload(1, "BV:1", "pv", True, None)
        assert p["priority"] is None


# ---------------------------------------------------------------------------
# read_range_payload
# ---------------------------------------------------------------------------


class TestReadRangePayload:
    def test_all_fields_present(self) -> None:
        p = read_range_payload(1234, "trend-log:1", "logBuffer", "p", 100, 87)
        assert p["device_id"] == 1234
        assert p["object_id"] == "trend-log:1"
        assert p["property"] == "logBuffer"
        assert p["range_type"] == "p"
        assert p["count_requested"] == 100
        assert p["count_received"] == 87

    @pytest.mark.parametrize("rt", ["p", "s", "t"])
    def test_valid_range_types(self, rt: str) -> None:
        p = read_range_payload(1, "TL:1", "logBuffer", rt, 10, 10)
        assert p["range_type"] == rt


# ---------------------------------------------------------------------------
# alarm_payload
# ---------------------------------------------------------------------------


class TestAlarmPayload:
    def test_minimal(self) -> None:
        p = alarm_payload(1234, "AV:1", "offnormal")
        assert p["object_id"] == "AV:1"
        assert p["event_state"] == "offnormal"
        assert p["notify_type"] == "alarm"
        assert "notification_class" not in p
        assert "priority" not in p
        assert "message_text" not in p

    def test_with_all_optional_fields(self) -> None:
        p = alarm_payload(
            1234,
            "LS:3",
            "fault",
            notify_type="event",
            notification_class=5,
            priority=10,
            message_text="Fire detected",
        )
        assert p["notification_class"] == 5
        assert p["priority"] == 10
        assert p["message_text"] == "Fire detected"

    @pytest.mark.parametrize(
        "state",
        ["normal", "fault", "offnormal", "highLimit", "lowLimit", "lifeSafetyAlarm"],
    )
    def test_valid_event_states(self, state: str) -> None:
        p = alarm_payload(1, "AV:1", state)
        assert p["event_state"] == state


# ---------------------------------------------------------------------------
# alarm_ack_payload
# ---------------------------------------------------------------------------


class TestAlarmAckPayload:
    def test_all_fields_present(self) -> None:
        p = alarm_ack_payload(1234, "AV:1", "offnormal", "ayush@datasailors.io", 7)
        assert p["device_id"] == 1234
        assert p["object_id"] == "AV:1"
        assert p["event_state"] == "offnormal"
        assert p["acknowledgment_source"] == "ayush@datasailors.io"
        assert p["process_id"] == 7


# ---------------------------------------------------------------------------
# discovery_payload
# ---------------------------------------------------------------------------


class TestDiscoveryPayload:
    def test_minimal(self) -> None:
        p = discovery_payload(1234, "192.168.1.10:47808")
        assert p["device_id"] == 1234
        assert p["address"] == "192.168.1.10:47808"
        assert "vendor_id" not in p
        assert "who_is_range" not in p

    def test_with_all_fields(self) -> None:
        p = discovery_payload(
            1234, "10.0.0.1", vendor_id=5, max_apdu=1476, low_limit=1, high_limit=1000
        )
        assert p["vendor_id"] == 5
        assert p["max_apdu"] == 1476
        assert p["who_is_range"] == [1, 1000]


# ---------------------------------------------------------------------------
# cov_subscribe_payload
# ---------------------------------------------------------------------------


class TestCovSubscribePayload:
    def test_required_fields(self) -> None:
        p = cov_subscribe_payload(1234, "AV:1", 42, 300)
        assert p["object_id"] == "AV:1"
        assert p["process_id"] == 42
        assert p["lifetime_s"] == 300
        assert p["confirmed"] is True

    def test_unconfirmed_flag(self) -> None:
        p = cov_subscribe_payload(1, "BV:1", 1, 0, confirmed=False)
        assert p["confirmed"] is False

    def test_monitored_property_and_increment(self) -> None:
        p = cov_subscribe_payload(1, "AV:1", 1, 300, prop="present-value", cov_increment=0.5)
        assert p["monitored_property"] == "present-value"
        assert p["cov_increment"] == 0.5


# ---------------------------------------------------------------------------
# device_mgmt_payload
# ---------------------------------------------------------------------------


class TestDeviceMgmtPayload:
    def test_reinitialize_device(self) -> None:
        p = device_mgmt_payload(1234, "ReinitializeDevice", state="warmstart")
        assert p["service"] == "ReinitializeDevice"
        assert p["state"] == "warmstart"
        assert p["device_id"] == 1234

    def test_time_sync(self) -> None:
        p = device_mgmt_payload(
            1234,
            "UTCTimeSynchronization",
            utc=True,
            dt_iso="2026-05-04T10:00:00Z",
        )
        assert p["utc"] is True
        assert "dt_iso" in p

    def test_dcc(self) -> None:
        p = device_mgmt_payload(
            1234,
            "DeviceCommunicationControl",
            enable_disable="disable",
            time_duration=30,
        )
        assert p["enable_disable"] == "disable"
        assert p["time_duration"] == 30

    def test_no_password_in_payload(self) -> None:
        # Passwords must NOT be logged — callers must not pass them
        p = device_mgmt_payload(1234, "ReinitializeDevice", state="coldstart")
        assert "password" not in p


# ---------------------------------------------------------------------------
# routing_payload
# ---------------------------------------------------------------------------


class TestRoutingPayload:
    def test_read_bdt(self) -> None:
        entries = [{"address": "192.168.1.1"}, {"address": "10.0.0.1"}]
        p = routing_payload("192.168.1.100:47808", "Read-BDT", entries=entries)
        assert p["bbmd_address"] == "192.168.1.100:47808"
        assert p["service"] == "Read-BDT"
        assert len(p["entries"]) == 2

    def test_register_fd(self) -> None:
        p = routing_payload("10.0.0.254:47808", "Register-Foreign-Device", ttl_s=600)
        assert p["ttl_s"] == 600
        assert "entries" not in p

    def test_entry_count_variant(self) -> None:
        p = routing_payload("10.0.0.254", "Read-FDT", entry_count=3)
        assert p["entry_count"] == 3


# ---------------------------------------------------------------------------
# error_payload
# ---------------------------------------------------------------------------


class TestErrorPayload:
    def test_minimal(self) -> None:
        p = error_payload(1234, "ReadProperty", "object-unknown")
        assert p["device_id"] == 1234
        assert p["service"] == "ReadProperty"
        assert p["error"] == "object-unknown"
        assert "object_id" not in p

    def test_with_object_id(self) -> None:
        p = error_payload(1234, "WriteProperty", "write-access-denied", object_id="AO:1")
        assert p["object_id"] == "AO:1"

    def test_none_device_id(self) -> None:
        p = error_payload(None, "GetEventInformation", "communication-failure")
        assert p["device_id"] is None


# ---------------------------------------------------------------------------
# Schema consistency checks
# ---------------------------------------------------------------------------


class TestSchemaConsistency:
    """Verify that all payload builders include schema_version."""

    def test_all_builders_include_schema_version(self) -> None:
        payloads = [
            connect_payload(1, "10.0.0.1"),
            read_payload(1, "AV:1", "pv"),
            write_payload(1, "AV:1", "pv", 1.0, 8),
            read_range_payload(1, "TL:1", "logBuffer", "p", 10, 10),
            alarm_payload(1, "AV:1", "offnormal"),
            alarm_ack_payload(1, "AV:1", "offnormal", "op", 1),
            discovery_payload(1, "10.0.0.1"),
            cov_subscribe_payload(1, "AV:1", 1, 300),
            device_mgmt_payload(1, "TimeSynchronization"),
            routing_payload("10.0.0.1", "Read-BDT"),
            error_payload(1, "RP", "error"),
        ]
        for p in payloads:
            assert "schema_version" in p, f"Missing schema_version in {p}"
            assert p["schema_version"] == AUDIT_SCHEMA_VERSION

    def test_all_builders_return_dict(self) -> None:
        payloads = [
            connect_payload(1, "10.0.0.1"),
            read_payload(1, "AV:1", "pv"),
            write_payload(1, "AV:1", "pv", 1.0, 8),
            read_range_payload(1, "TL:1", "logBuffer", "p", 10, 10),
            alarm_payload(1, "AV:1", "offnormal"),
            alarm_ack_payload(1, "AV:1", "offnormal", "op", 1),
            discovery_payload(1, "10.0.0.1"),
            cov_subscribe_payload(1, "AV:1", 1, 300),
            device_mgmt_payload(1, "TimeSynchronization"),
            routing_payload("10.0.0.1", "Read-BDT"),
            error_payload(1, "RP", "error"),
        ]
        for p in payloads:
            assert isinstance(p, dict)
