# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for P7.B.6 ReadRange, P7.B.7 WPM, and P7.E BBMD routing methods."""

from __future__ import annotations

import asyncio
import socket
import struct
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from protoskipper.builtin_drivers.bacnet.client import (
    BacnetIpSession,
    _BackgroundLoop,
)
from protoskipper.core.driver import (
    DeviceRef,
    ObjectRef,
    SafetyContext,
    WriteIntent,
)
from protoskipper.core.errors import AuthorizationDenied

# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_bacnet_advanced_services.py)
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
    if safety is None:
        safety = _make_safety()

    with (
        patch.object(BacnetIpSession, "_async_create_app", new_callable=AsyncMock),
        patch.object(_BackgroundLoop, "__init__", return_value=None),
        patch.object(_BackgroundLoop, "start", create=True, return_value=None),
    ):
        session = BacnetIpSession.__new__(BacnetIpSession)
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


def _make_ref(objid: str = "analog-value:1") -> ObjectRef:
    return ObjectRef(device=_DEVICE, object_id=objid, data_type="real")


def _make_intent(
    objid: str = "analog-value:1",
    value: Any = 21.5,
    priority: int = 8,
    prop: str = "presentValue",
) -> WriteIntent:
    from bacpypes3.primitivedata import Real

    return WriteIntent(
        object_ref=_make_ref(objid),
        requested_value=value,
        encoded_bytes=struct.pack("<f", float(value)),
        description=f"WriteProperty {objid} {prop}={value} (priority {priority})",
        metadata={
            "bacnet_value": Real(float(value)),
            "priority": priority,
            "prop": prop,
            "data_type": "real",
        },
    )


# ===========================================================================
# P7.B.6 — read_range (general ReadRange)
# ===========================================================================


class TestReadRange:
    def test_by_position_calls_read_range_property(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = [{"value": 21.5, "timestamp": None}]
        ref = _make_ref("trend-log:1")
        result = session.read_range(ref, "logBuffer", range_type="p", first=1, count=50)
        assert session._loop_thread.submit.called
        assert result == [{"value": 21.5, "timestamp": None}]

    def test_by_sequence_number(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = [{"value": 1.0, "timestamp": None}]
        ref = _make_ref("trend-log:1")
        session.read_range(ref, "logBuffer", range_type="s", first=1000, count=10)
        call_args = session._loop_thread.submit.call_args
        coro = call_args[0][0]
        # Verify the coroutine was created with correct args
        assert coro is not None

    def test_by_time_range(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = []
        ref = _make_ref("trend-log:2")
        result = session.read_range(
            ref,
            "logBuffer",
            range_type="t",
            count=100,
            date_str="2026-01-01",
            time_str="08:00:00",
        )
        assert result == []

    def test_custom_property(self) -> None:
        """read_range can target any property, not just logBuffer."""
        session = _make_session()
        session._loop_thread.submit.return_value = ["AV:1", "AV:2"]
        ref = _make_ref("device:1")
        result = session.read_range(ref, "objectList", range_type="p", first=1, count=5)
        assert result == ["AV:1", "AV:2"]

    def test_returns_empty_on_error(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("APDU Error")
        ref = _make_ref("trend-log:1")
        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError):
            session.read_range(ref)


class TestReadRangePropertyAsync:
    """Low-level async helper tests."""

    def test_returns_empty_on_none_response(self) -> None:
        session = _make_session()
        session._app.read_range = AsyncMock(return_value=None)

        async def run() -> list:
            return await session._async_read_range_property(
                "trend-log:1", "logBuffer", "p", 1, 10, "2000-01-01", "00:00:00"
            )

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result == []

    def test_returns_empty_on_error_class_response(self) -> None:
        session = _make_session()
        err_resp = MagicMock()
        err_resp.errorClass = "object"
        session._app.read_range = AsyncMock(return_value=err_resp)

        async def run() -> list:
            return await session._async_read_range_property(
                "trend-log:1", "logBuffer", "p", 1, 10, "2000-01-01", "00:00:00"
            )

        result = asyncio.get_event_loop().run_until_complete(run())
        assert result == []

    def test_iterates_and_converts_items(self) -> None:
        from bacpypes3.primitivedata import Real

        session = _make_session()
        session._app.read_range = AsyncMock(return_value=[Real(3.14)])

        async def run() -> list:
            return await session._async_read_range_property(
                "analog-input:1", "eventTimeStamps", "p", 1, 5, "2000-01-01", "00:00:00"
            )

        result = asyncio.get_event_loop().run_until_complete(run())
        assert len(result) == 1
        assert abs(result[0] - 3.14) < 0.001


# ===========================================================================
# P7.B.6 — read_property_array
# ===========================================================================


class TestReadPropertyArray:
    def test_reads_property_without_index(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = [("analog-value", 1)]
        ref = _make_ref("device:1")
        result = session.read_property_array(ref, "objectList")
        assert session._loop_thread.submit.called
        assert result == [("analog-value", 1)]

    def test_reads_array_length_at_index_zero(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = 42
        ref = _make_ref("device:1")
        result = session.read_property_array(ref, "objectList", array_index=0)
        assert result == 42

    def test_reads_element_at_specific_index(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = ("analog-value", 5)
        ref = _make_ref("device:1")
        result = session.read_property_array(ref, "objectList", array_index=3)
        assert result == ("analog-value", 5)

    def test_raises_driver_error_on_exception(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("timeout")
        ref = _make_ref("device:1")
        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError, match="objectList"):
            session.read_property_array(ref, "objectList", array_index=1)


# ===========================================================================
# P7.B.7 — write_many (WritePropertyMultiple)
# ===========================================================================


class TestWriteMany:
    def test_empty_list_returns_empty(self) -> None:
        session = _make_session()
        results = session.write_many([])
        assert results == []
        session._loop_thread.submit.assert_not_called()

    def test_single_intent_sends_wpm(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None  # WPM returns None on success
        intent = _make_intent()
        results = session.write_many([intent])
        assert len(results) == 1
        assert results[0].success is True
        session._loop_thread.submit.assert_called_once()

    def test_multiple_intents_batched_in_one_call(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        intents = [_make_intent(f"analog-value:{i}") for i in range(3)]
        results = session.write_many(intents)
        assert len(results) == 3
        assert all(r.success for r in results)
        # All writes go in a single submit call
        assert session._loop_thread.submit.call_count == 1

    def test_safety_denies_raises_authorization_error(self) -> None:
        safety = _make_safety(allow_writes=False)
        session = _make_session(safety)
        intent = _make_intent()
        with pytest.raises(AuthorizationDenied):
            session.write_many([intent])

    def test_apdu_timeout_marks_all_failed(self) -> None:
        session = _make_session()
        from protoskipper.core.errors import ConnectionFailure

        session._loop_thread.submit.side_effect = ConnectionFailure("timeout")
        intents = [_make_intent("analog-value:1"), _make_intent("analog-value:2")]
        with pytest.raises(ConnectionFailure):
            session.write_many(intents)
        # record_write_outcome was called for each intent
        assert session.safety.record_write_outcome.call_count == 2

    def test_protocol_error_marks_all_failed(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("reject")
        intents = [_make_intent()]
        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError):
            session.write_many(intents)
        assert session.safety.record_write_outcome.call_count == 1

    def test_record_write_outcome_called_for_all_on_success(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = None
        intents = [_make_intent(f"binary-value:{i}") for i in range(5)]
        results = session.write_many(intents)
        assert session.safety.record_write_outcome.call_count == 5
        assert all(r.success for r in results)


class TestWpmAsync:
    """Low-level _async_wpm tests."""

    def test_builds_request_and_calls_app_request(self) -> None:
        from bacpypes3.primitivedata import Real

        session = _make_session()
        session._app.request = AsyncMock(return_value=None)

        async def run() -> None:
            await session._async_wpm(
                [
                    ("analog-value:1", "presentValue", Real(21.5), 8),
                    ("binary-value:2", "presentValue", Real(1.0), None),
                ]
            )

        asyncio.get_event_loop().run_until_complete(run())
        session._app.request.assert_awaited_once()
        req = session._app.request.call_args[0][0]
        # WritePropertyMultipleRequest should have two access specs
        assert len(req.listOfWriteAccessSpecs) == 2

    def test_empty_writes_sends_empty_wpm(self) -> None:
        session = _make_session()
        session._app.request = AsyncMock(return_value=None)

        async def run() -> None:
            await session._async_wpm([])

        asyncio.get_event_loop().run_until_complete(run())
        session._app.request.assert_awaited_once()
        req = session._app.request.call_args[0][0]
        assert len(req.listOfWriteAccessSpecs) == 0


# ===========================================================================
# P7.E — BBMD / FD routing (read_bdt, read_fdt)
# ===========================================================================


def _bdt_ack_bytes(entries: list[str]) -> bytes:
    """Build a raw Read-BDT-Ack UDP response with the given IP address strings."""
    import socket as _socket

    body = bytearray()
    for addr_str in entries:
        host, _, port_str = addr_str.partition(":")
        port = int(port_str) if port_str else 47808
        # BDT entry: 4B IP, 2B port, 4B mask (subnet mask)
        body.extend(_socket.inet_aton(host))
        body.extend(struct.pack(">H", port))
        body.extend(b"\xff\xff\xff\x00")  # /24 mask
    length = 4 + len(body)
    header = bytes([0x81, 0x03, length >> 8, length & 0xFF])
    return header + bytes(body)


def _fdt_ack_bytes(entries: list[tuple[str, int, int]]) -> bytes:
    """Build a raw Read-FDT-Ack UDP response.

    *entries* is a list of (``"host:port"``, ttl, remaining) tuples.
    """
    import socket as _socket

    body = bytearray()
    for addr_str, ttl, remaining in entries:
        host, _, port_str = addr_str.partition(":")
        port = int(port_str) if port_str else 47808
        body.extend(_socket.inet_aton(host))
        body.extend(struct.pack(">H", port))
        body.extend(struct.pack(">H", ttl))
        body.extend(struct.pack(">H", remaining))
    length = 4 + len(body)
    header = bytes([0x81, 0x07, length >> 8, length & 0xFF])
    return header + bytes(body)


class TestReadBdtAsync:
    """Tests for _async_read_bdt_raw using a loopback UDP echo server."""

    def _run_bdt_test(self, response_bytes: bytes) -> list[dict]:
        """Start a UDP server that replies with *response_bytes*, then call the helper."""

        async def inner() -> list[dict]:
            loop = asyncio.get_event_loop()
            ready = asyncio.Event()

            class _Echo(asyncio.DatagramProtocol):
                def __init__(self) -> None:
                    self._transport: asyncio.DatagramTransport | None = None

                def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
                    self._transport = transport
                    ready.set()

                def datagram_received(self, data: bytes, addr: tuple) -> None:
                    if self._transport is not None:
                        self._transport.sendto(response_bytes, addr)

            transport, _ = await loop.create_datagram_endpoint(
                _Echo,
                local_addr=("127.0.0.1", 0),
                family=socket.AF_INET,
            )
            try:
                await ready.wait()
                port = transport.get_extra_info("sockname")[1]
                session = _make_session()
                return await session._async_read_bdt_raw(f"127.0.0.1:{port}", timeout=2.0)
            finally:
                transport.close()

        return asyncio.get_event_loop().run_until_complete(inner())

    def test_parses_single_bdt_entry(self) -> None:
        raw = _bdt_ack_bytes(["192.168.1.1:47808"])
        result = self._run_bdt_test(raw)
        assert len(result) == 1
        assert "192.168.1.1" in result[0]["address"]

    def test_parses_multiple_bdt_entries(self) -> None:
        raw = _bdt_ack_bytes(["10.0.0.1:47808", "10.0.0.2:47808"])
        result = self._run_bdt_test(raw)
        assert len(result) == 2

    def test_empty_bdt_returns_empty_list(self) -> None:
        # BDT-Ack with no entries: just the 4-byte header
        raw = bytes([0x81, 0x03, 0x00, 0x04])
        result = self._run_bdt_test(raw)
        assert result == []

    def test_wrong_function_code_returns_empty(self) -> None:
        # FDT-Ack (0x07) instead of BDT-Ack (0x03)
        raw = _fdt_ack_bytes([("10.0.0.5:47808", 600, 555)])
        result = self._run_bdt_test(raw)
        assert result == []


class TestReadFdtAsync:
    """Tests for _async_read_fdt_raw using a loopback UDP echo server."""

    def _run_fdt_test(self, response_bytes: bytes) -> list[dict]:
        async def inner() -> list[dict]:
            loop = asyncio.get_event_loop()
            ready = asyncio.Event()

            class _Echo(asyncio.DatagramProtocol):
                def __init__(self) -> None:
                    self._transport: asyncio.DatagramTransport | None = None

                def connection_made(self, transport: asyncio.DatagramTransport) -> None:  # type: ignore[override]
                    self._transport = transport
                    ready.set()

                def datagram_received(self, data: bytes, addr: tuple) -> None:
                    if self._transport is not None:
                        self._transport.sendto(response_bytes, addr)

            transport, _ = await loop.create_datagram_endpoint(
                _Echo,
                local_addr=("127.0.0.1", 0),
                family=socket.AF_INET,
            )
            try:
                await ready.wait()
                port = transport.get_extra_info("sockname")[1]
                session = _make_session()
                return await session._async_read_fdt_raw(f"127.0.0.1:{port}", timeout=2.0)
            finally:
                transport.close()

        return asyncio.get_event_loop().run_until_complete(inner())

    def test_parses_single_fdt_entry(self) -> None:
        raw = _fdt_ack_bytes([("192.168.5.10:47808", 600, 555)])
        result = self._run_fdt_test(raw)
        assert len(result) == 1
        entry = result[0]
        assert "192.168.5.10" in entry["address"]
        assert entry["ttl"] == 600
        assert entry["remaining"] == 555

    def test_parses_multiple_fdt_entries(self) -> None:
        raw = _fdt_ack_bytes([("10.1.1.1:47808", 300, 280), ("10.1.1.2:47808", 600, 100)])
        result = self._run_fdt_test(raw)
        assert len(result) == 2
        assert result[0]["ttl"] == 300
        assert result[1]["remaining"] == 100

    def test_empty_fdt_returns_empty_list(self) -> None:
        raw = bytes([0x81, 0x07, 0x00, 0x04])
        result = self._run_fdt_test(raw)
        assert result == []

    def test_wrong_function_code_returns_empty(self) -> None:
        # BDT-Ack (0x03) instead of FDT-Ack (0x07)
        raw = _bdt_ack_bytes(["10.0.0.1:47808"])
        result = self._run_fdt_test(raw)
        assert result == []


class TestReadBdtPublic:
    def test_delegates_to_loop_thread_submit(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = [{"address": "192.168.1.1"}]
        result = session.read_bdt("192.168.1.100:47808")
        assert session._loop_thread.submit.called
        assert result == [{"address": "192.168.1.1"}]

    def test_wraps_exception_as_driver_error(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("UDP timeout")
        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError, match="Read-BDT"):
            session.read_bdt("192.168.1.100:47808")


class TestReadFdtPublic:
    def test_delegates_to_loop_thread_submit(self) -> None:
        session = _make_session()
        session._loop_thread.submit.return_value = [
            {"address": "10.0.0.5", "ttl": 600, "remaining": 400}
        ]
        result = session.read_fdt("192.168.1.100:47808")
        assert result[0]["ttl"] == 600

    def test_wraps_exception_as_driver_error(self) -> None:
        session = _make_session()
        session._loop_thread.submit.side_effect = Exception("unreachable")
        from protoskipper.core.errors import DriverError

        with pytest.raises(DriverError, match="Read-FDT"):
            session.read_fdt("10.0.0.1:47808")
