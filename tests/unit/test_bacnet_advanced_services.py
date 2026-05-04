# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for P7.B.9-P7.B.13: alarms, trend-log, schedule, file, device-mgmt."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from protoskipper.builtin_drivers.bacnet.client import (
    BacnetIpSession,
    _BackgroundLoop,
    _log_record_to_dict,
)
from protoskipper.core.driver import (
    Access,
    DeviceRef,
    ObjectRef,
    SafetyContext,
)
from protoskipper.core.errors import AuthorizationDenied

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DEVICE = DeviceRef(
    protocol="bacnet.ip",
    address="127.0.0.1:47808/dev=1",
    label="test-device",
)


def _make_safety(*, allow_writes: bool = True) -> SafetyContext:
    s = MagicMock(spec=SafetyContext)
    s.require_write_authorization.return_value = allow_writes
    s.record_write_outcome.return_value = None
    return s


def _make_session(safety: SafetyContext | None = None) -> BacnetIpSession:
    """Create a BacnetIpSession with a mocked app and background loop."""
    if safety is None:
        safety = _make_safety()

    with (
        patch.object(BacnetIpSession, "_async_create_app", new_callable=AsyncMock),
        patch.object(_BackgroundLoop, "__init__", return_value=None),
        patch.object(_BackgroundLoop, "start", create=True, return_value=None),
    ):
        session = BacnetIpSession.__new__(BacnetIpSession)
        # Minimal __init__ without actually connecting
        session.device = _DEVICE
        session.safety = safety
        from protoskipper.builtin_drivers.bacnet.vendor_profiles import get_profile

        session._profile = get_profile()
        session._loop_thread = MagicMock(spec=_BackgroundLoop)
        session._app = MagicMock()
        session._remote_addr = "127.0.0.1"
        session._remote_device_id = 1
        session._object_cache = {}
        session._subs = {}
        session._sub_objids = {}
        session._next_process_id = 1
        session._closed = False
        session._apdu_timeout = 6.0
        return session


# ---------------------------------------------------------------------------
# _log_record_to_dict
# ---------------------------------------------------------------------------


class TestLogRecordToDict:
    def test_returns_dict_with_keys(self) -> None:
        record = MagicMock()
        record.timestamp = "2026-01-01T00:00:00"
        datum = MagicMock()
        datum.realValue = 23.5
        for attr in ("integerValue", "booleanValue", "enumValue", "bitStringValue"):
            setattr(datum, attr, None)
        record.logDatum = datum

        result = _log_record_to_dict(record)
        assert "timestamp" in result
        assert "value" in result

    def test_timestamp_none_when_missing(self) -> None:
        record = MagicMock()
        record.timestamp = None
        record.logDatum = None
        result = _log_record_to_dict(record)
        assert result["timestamp"] is None

    def test_bad_timestamp_gracefully_none(self) -> None:
        record = MagicMock()
        record.timestamp = "not-a-date"
        record.logDatum = None
        result = _log_record_to_dict(record)
        assert result["timestamp"] is None

    def test_value_none_when_no_datum(self) -> None:
        record = MagicMock()
        record.timestamp = None
        record.logDatum = None
        result = _log_record_to_dict(record)
        assert result["value"] is None


# ---------------------------------------------------------------------------
# P7.B.9 — Alarms & events
# ---------------------------------------------------------------------------


class TestGetEventInformation:
    def test_returns_empty_list_on_error(self) -> None:
        session = _make_session()

        async def _fail_request(req: Any) -> None:
            raise RuntimeError("network error")

        session._app.request = _fail_request

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(session._async_get_event_information())
        finally:
            loop.close()
        assert result == []

    def test_returns_empty_list_on_none_response(self) -> None:
        session = _make_session()
        session._app.request = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(session._async_get_event_information())
        finally:
            loop.close()
        assert result == []

    def test_parses_event_summaries(self) -> None:
        session = _make_session()

        ei = MagicMock()
        ei.objectIdentifier = ("analog-value", 1)
        ei.eventState = "active"
        acked = MagicMock()
        acked.toOffnormal = True
        acked.toFault = False
        acked.toNormal = False
        ei.acknowledgedTransitions = acked
        ei.notifyType = "alarm"
        ei.eventPriorities = [5, 5, 5]

        response = MagicMock()
        response.listOfEventSummaries = [ei]
        response.moreEvents = False
        session._app.request = AsyncMock(return_value=response)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(session._async_get_event_information())
        finally:
            loop.close()

        assert len(result) == 1
        assert result[0]["object_id"] == "analog-value:1"
        assert result[0]["event_state"] == "active"
        assert result[0]["acknowledged_transitions"]["to_offnormal"] is True
        assert result[0]["notify_type"] == "alarm"

    def test_get_event_information_public_raises_on_session_error(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = RuntimeError("bad")

        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError):
            session.get_event_information()

    def test_get_event_information_public_returns_list(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = [{"object_id": "av:1", "event_state": "active"}]
        result = session.get_event_information()
        assert isinstance(result, list)


class TestAcknowledgeAlarm:
    def test_denied_when_safety_disallows(self) -> None:
        safety = _make_safety(allow_writes=False)
        session = _make_session(safety=safety)
        with pytest.raises(AuthorizationDenied):
            session.acknowledge_alarm("analog-value:1", "active")

    def test_allowed_when_safety_permits(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        # should not raise
        session.acknowledge_alarm("analog-value:1", "offnormal", source="ops")
        session._loop_thread.submit.assert_called_once()

    def test_propagates_protocol_error(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("APDU error")

        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError):
            session.acknowledge_alarm("analog-value:1", "offnormal")

    def test_async_sends_request(self) -> None:
        session = _make_session()
        session._app.request = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                session._async_acknowledge_alarm(
                    "analog-value:1", "offnormal", process_id=1, source="test"
                )
            )
        finally:
            loop.close()

        session._app.request.assert_called_once()


# ---------------------------------------------------------------------------
# P7.B.10 — TrendLog / ReadRange
# ---------------------------------------------------------------------------


class TestReadTrendLog:
    def test_returns_empty_on_none_result(self) -> None:
        session = _make_session()
        session._app.read_range = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                session._async_read_trend_log("trend-log:1", "p", 1, 100, "2000-01-01", "00:00:00")
            )
        finally:
            loop.close()
        assert result == []

    def test_returns_empty_on_error_response(self) -> None:
        session = _make_session()
        error_resp = MagicMock()
        error_resp.errorClass = "object"
        session._app.read_range = AsyncMock(return_value=error_resp)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                session._async_read_trend_log("trend-log:1", "p", 1, 100, "2000-01-01", "00:00:00")
            )
        finally:
            loop.close()
        assert result == []

    def test_converts_records_to_dicts(self) -> None:
        session = _make_session()

        record = MagicMock()
        record.timestamp = "2026-01-01T12:00:00"
        datum = MagicMock()
        datum.realValue = 42.0
        for attr in ("integerValue", "booleanValue", "enumValue", "bitStringValue"):
            setattr(datum, attr, None)
        record.logDatum = datum

        session._app.read_range = AsyncMock(return_value=[record])

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                session._async_read_trend_log("trend-log:1", "p", 1, 1, "2000-01-01", "00:00:00")
            )
        finally:
            loop.close()

        assert len(result) == 1
        assert "timestamp" in result[0]
        assert "value" in result[0]

    def test_public_read_trend_log_returns_list(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = []
        ref = ObjectRef(device=_DEVICE, object_id="trend-log:1", data_type="any")
        result = session.read_trend_log(ref)
        assert result == []

    def test_public_read_trend_log_raises_on_error(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("timeout")
        ref = ObjectRef(device=_DEVICE, object_id="trend-log:1", data_type="any")

        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError):
            session.read_trend_log(ref)


# ---------------------------------------------------------------------------
# P7.B.11 — Schedule
# ---------------------------------------------------------------------------


class TestReadSchedule:
    def test_returns_dict(self) -> None:
        session = _make_session()
        expected = {"scheduleDefault": 21.0}
        session._loop_thread.submit.return_value = {"schedule:1": expected}
        ref = ObjectRef(device=_DEVICE, object_id="schedule:1", data_type="any")
        result = session.read_schedule(ref)
        assert result == expected

    def test_returns_empty_dict_when_not_in_rpm(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = {}
        ref = ObjectRef(device=_DEVICE, object_id="schedule:1", data_type="any")
        assert session.read_schedule(ref) == {}

    def test_raises_on_rpm_error(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("rpm failed")
        ref = ObjectRef(device=_DEVICE, object_id="schedule:1", data_type="any")

        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError):
            session.read_schedule(ref)


class TestWriteScheduleDefault:
    def test_denied_when_safety_disallows(self) -> None:
        safety = _make_safety(allow_writes=False)
        session = _make_session(safety=safety)
        ref = ObjectRef(
            device=_DEVICE,
            object_id="schedule:1",
            data_type="real",
            access=Access.READ_WRITE,
        )
        with pytest.raises(AuthorizationDenied):
            session.write_schedule_default(ref, 21.0)

    def test_succeeds_when_safety_permits(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        ref = ObjectRef(
            device=_DEVICE,
            object_id="schedule:1",
            data_type="real",
            access=Access.READ_WRITE,
        )
        result = session.write_schedule_default(ref, 21.0)
        assert result.success is True


# ---------------------------------------------------------------------------
# P7.B.12 — File services
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_returns_bytes(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = b"hello world"
        ref = ObjectRef(device=_DEVICE, object_id="file:1", data_type="any")
        data = session.read_file(ref)
        assert data == b"hello world"

    def test_raises_on_error(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("file read error")
        ref = ObjectRef(device=_DEVICE, object_id="file:1", data_type="any")

        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError):
            session.read_file(ref)

    def test_async_loops_until_end_of_file(self) -> None:
        session = _make_session()

        call_count = 0

        async def fake_request(req: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.accessMethod = MagicMock()
            resp.accessMethod.streamAccess = MagicMock()
            resp.accessMethod.streamAccess.fileData = b"chunk"
            resp.endOfFile = call_count >= 2  # two chunks then EOF
            return resp

        session._app.request = fake_request

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                session._async_read_file("file:1", start_position=0, chunk_size=1400)
            )
        finally:
            loop.close()

        assert result == b"chunkchunk"
        assert call_count == 2


class TestWriteFile:
    def test_denied_when_safety_disallows(self) -> None:
        safety = _make_safety(allow_writes=False)
        session = _make_session(safety=safety)
        ref = ObjectRef(
            device=_DEVICE, object_id="file:1", data_type="any", access=Access.READ_WRITE
        )
        with pytest.raises(AuthorizationDenied):
            session.write_file(ref, b"data")

    def test_succeeds_when_safety_permits(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = 0
        ref = ObjectRef(
            device=_DEVICE, object_id="file:1", data_type="any", access=Access.READ_WRITE
        )
        result = session.write_file(ref, b"hello")
        assert result.success is True
        session.safety.record_write_outcome.assert_called_once()

    def test_records_failure_on_apdu_timeout(self) -> None:
        session = _make_session()
        from protoskipper.core.errors import ConnectionFailure

        session._loop_thread.submit.side_effect = ConnectionFailure("timeout")
        ref = ObjectRef(
            device=_DEVICE, object_id="file:1", data_type="any", access=Access.READ_WRITE
        )
        with pytest.raises(ConnectionFailure):
            session.write_file(ref, b"hello")
        session.safety.record_write_outcome.assert_called_once()
        recorded = session.safety.record_write_outcome.call_args[0][0]
        assert recorded.success is False


# ---------------------------------------------------------------------------
# P7.B.13 — Device management
# ---------------------------------------------------------------------------


class TestTimeSync:
    def test_sends_time_sync(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        session.time_sync(dt)
        session._loop_thread.submit.assert_called_once()

    def test_uses_utc_now_when_dt_is_none(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        session.time_sync()
        session._loop_thread.submit.assert_called_once()

    def test_utc_flag_passes_through(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        session.time_sync(utc=True)
        session._loop_thread.submit.assert_called_once()

    def test_raises_protocol_error_on_failure(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("send failed")

        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError):
            session.time_sync()


class TestReinitializeDevice:
    def test_denied_when_safety_disallows(self) -> None:
        safety = _make_safety(allow_writes=False)
        session = _make_session(safety=safety)
        with pytest.raises(AuthorizationDenied):
            session.reinitialize_device("warmstart")

    def test_allowed_when_safety_permits(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        session.reinitialize_device("warmstart")
        session._loop_thread.submit.assert_called_once()

    def test_coldstart_variant(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        session.reinitialize_device("coldstart", password="secret")
        session._loop_thread.submit.assert_called_once()

    def test_raises_protocol_error_on_comm_failure(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("device unreachable")

        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError):
            session.reinitialize_device("warmstart")


class TestDeviceCommunicationControl:
    def test_denied_when_safety_disallows(self) -> None:
        safety = _make_safety(allow_writes=False)
        session = _make_session(safety=safety)
        with pytest.raises(AuthorizationDenied):
            session.device_communication_control("disable")

    def test_enable_variant(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        session.device_communication_control("enable")
        session._loop_thread.submit.assert_called_once()

    def test_disable_with_duration(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        session.device_communication_control("disable", time_duration=30)
        session._loop_thread.submit.assert_called_once()

    def test_disable_initiation_variant(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        session.device_communication_control("disableInitiation", password="pw")
        session._loop_thread.submit.assert_called_once()

    def test_raises_protocol_error_on_failure(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("send failed")

        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError):
            session.device_communication_control("disable")


# ---------------------------------------------------------------------------
# P7.B.13 — async device management helpers (smoke tests)
# ---------------------------------------------------------------------------


class TestAsyncDeviceManagementHelpers:
    def test_async_time_sync_utc(self) -> None:
        session = _make_session()
        session._app.request = AsyncMock(return_value=None)
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(session._async_time_sync(dt, utc=True))
        finally:
            loop.close()

        session._app.request.assert_called_once()

    def test_async_reinitialize_device_warmstart(self) -> None:
        session = _make_session()
        session._app.request = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(session._async_reinitialize_device("warmstart", None))
        finally:
            loop.close()

        session._app.request.assert_called_once()

    def test_async_device_communication_control_disable(self) -> None:
        session = _make_session()
        session._app.request = AsyncMock(return_value=None)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                session._async_device_communication_control("disable", 30, None)
            )
        finally:
            loop.close()

        session._app.request.assert_called_once()
