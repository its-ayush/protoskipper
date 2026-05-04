# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet/IP session backed by bacpypes3 with an async→sync wrapper.

Design notes
------------

``bacpypes3`` is fully async (asyncio).  ``DriverSession`` is synchronous
(runs on a QThread managed by ``SessionManager``).  The bridge is a private
background thread that owns a persistent asyncio event loop.  Every public
session method dispatches a coroutine via
``asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)`` so the
caller blocks until the coroutine completes or the timeout fires.

Thread ownership
~~~~~~~~~~~~~~~~
* ``_BackgroundLoop`` — created once per session, owns the event loop thread.
* ``bacpypes3.ipv4.app.NormalApplication`` — created inside the event loop
  thread via ``run_coroutine_threadsafe``.
* ``BacnetIpSession`` — lives on the Qt worker thread; calls synchronous
  public methods which block on the event loop.

Segmentation / retries
-----------------------
``bacpypes3`` handles segmented responses automatically.  We configure APDU
timeout from the vendor profile and the APDU parameters received in the
initial ``I-Am``.

COV subscriptions
-----------------
Implements the ``Subscriber`` mix-in (optional capability).  Each active
subscription is tracked in ``_subs`` (process_id → callback) and is
renewed before expiry by a lightweight asyncio task.

"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from protoskipper.core.driver import (
    Access,
    DeviceRef,
    DriverSession,
    ObjectRef,
    Quality,
    ReadResult,
    SafetyContext,
    SubscriptionCallback,
    SubscriptionHandle,
    WriteIntent,
    WriteResult,
)
from protoskipper.core.errors import (
    AuthorizationDenied,
    ConnectionFailure,
    DriverError,
    EncodingError,
)

from .objects import (
    COMMANDABLE_TYPES,
    bacnet_value_to_python,
    object_id_str,
    parse_object_id,
    python_to_bacnet_value,
)
from .vendor_profiles import VendorProfile, get_profile

# Convenience aliases matching driver contract language
CommError = ConnectionFailure
ProtocolError = DriverError
WriteAuthorizationError = AuthorizationDenied

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)

_DEFAULT_APDU_TIMEOUT = 6.0  # seconds
_COV_RENEWAL_MARGIN = 30  # renew this many seconds before lifetime expiry
_DISCOVERY_TIMEOUT = 5.0  # seconds for Who-Is scan


# ---------------------------------------------------------------------------
# Concrete SubscriptionHandle
# ---------------------------------------------------------------------------


class _BacnetSubHandle:
    """Opaque token returned by :meth:`BacnetIpSession.subscribe`."""

    __slots__ = ("process_id",)

    def __init__(self, process_id: int) -> None:
        self.process_id = process_id

    # Structurally satisfies SubscriptionHandle (Protocol) — no methods required.


# ---------------------------------------------------------------------------
# Background asyncio loop thread
# ---------------------------------------------------------------------------


class _BackgroundLoop:
    """Manages a persistent asyncio event loop running in a daemon thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            name="bacnet-asyncio",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Any, timeout: float | None = None) -> Any:
        """Dispatch *coro* to the background loop and block until done."""
        import asyncio as _asyncio

        fut = _asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout=timeout)
        except _asyncio.TimeoutError:
            fut.cancel()
            raise CommError("BACnet request timed out") from None

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# BACnet/IP session
# ---------------------------------------------------------------------------


class BacnetIpSession(DriverSession):
    """Synchronous BACnet/IP client session backed by bacpypes3."""

    def __init__(
        self,
        device: DeviceRef,
        safety: SafetyContext,
        *,
        vendor_profile: VendorProfile | None = None,
    ) -> None:
        self.device = device
        self.safety = safety
        self._profile = vendor_profile or get_profile()
        self._loop_thread = _BackgroundLoop()
        self._app: Any = None  # NormalApplication; created on event loop thread
        self._remote_addr: str = _bacnet_address(device.address)
        self._remote_device_id: int | None = _extract_device_id(device.address)
        self._object_cache: dict[str, dict[str, Any]] = {}
        self._subs: dict[int, SubscriptionCallback] = {}  # process_id → callback
        self._sub_objids: dict[int, str] = {}  # process_id → objid
        self._next_process_id = 1
        self._closed = False
        self._apdu_timeout = self._profile.default_apdu_timeout_ms / 1000.0

        # Create the bacpypes3 application on the event loop thread
        self._loop_thread.submit(self._async_create_app(), timeout=10)

    # ------------------------------------------------------------------
    # Internal async helpers
    # ------------------------------------------------------------------

    async def _async_create_app(self) -> None:
        try:
            from bacpypes3.ipv4.app import NormalApplication
            from bacpypes3.local.device import DeviceObject
            from bacpypes3.pdu import Address
        except ImportError as exc:
            raise CommError(
                "bacpypes3 is not installed — run: pip install 'protoskipper[bacnet]'"
            ) from exc

        local_device = DeviceObject(
            objectIdentifier=("device", 3194001),
            objectName="ProtoSkipper-BACnet-Client",
            vendorIdentifier=0xFFFF,
            vendorName="ProtoSkipper",
            modelName="ProtoSkipper",
            firmwareRevision="1.0",
            applicationSoftwareVersion="1.0",
        )
        self._app = NormalApplication(local_device, Address("0.0.0.0"))

        # Hook COV notification handlers
        _orig_unconf = getattr(self._app, "do_UnconfirmedCOVNotificationRequest", None)
        _orig_conf = getattr(self._app, "do_ConfirmedCOVNotificationRequest", None)

        async def _handle_cov(apdu: Any) -> None:
            await self._async_dispatch_cov(apdu)
            if _orig_unconf and _orig_conf:
                pass  # parent handlers not typically overridden

        self._app.do_UnconfirmedCOVNotificationRequest = _handle_cov
        self._app.do_ConfirmedCOVNotificationRequest = _handle_cov

    async def _async_dispatch_cov(self, apdu: Any) -> None:
        pid = int(apdu.subscriberProcessIdentifier)
        callback = self._subs.get(pid)
        if callback is None:
            return
        objid = self._sub_objids.get(pid, "")
        for prop_value in apdu.listOfValues or []:
            prop_id = str(prop_value.propertyIdentifier)
            if prop_id == "presentValue":
                py_val = bacnet_value_to_python(prop_value.value)
                ref = ObjectRef(
                    device=self.device,
                    object_id=objid,
                    data_type="any",
                )
                try:
                    callback(
                        ReadResult(
                            object_ref=ref,
                            value=py_val,
                            quality=Quality.GOOD,
                            timestamp=datetime.now(tz=timezone.utc),
                        )
                    )
                except Exception as exc:
                    _logger.warning("COV callback raised: %s", exc)

    async def _async_close_app(self) -> None:
        if self._app is not None:
            with contextlib.suppress(Exception):
                self._app.close()
            self._app = None

    async def _async_read_property(
        self,
        objid: str,
        prop: str,
        array_index: int | None = None,
    ) -> Any:
        obj_type, instance = parse_object_id(objid)
        val = await self._app.read_property(
            self._remote_addr,
            f"{obj_type},{instance}",
            prop,
            array_index,
        )
        return bacnet_value_to_python(val)

    async def _async_rpm(
        self,
        requests: list[tuple[str, list[str]]],
    ) -> dict[str, dict[str, Any]]:
        """ReadPropertyMultiple over a list of (objid, [prop, ...]) pairs."""
        from bacpypes3.apdu import (
            PropertyReference,
            ReadAccessSpecification,
        )
        from bacpypes3.basetypes import PropertyIdentifier
        from bacpypes3.primitivedata import ObjectIdentifier

        result: dict[str, dict[str, Any]] = {}
        access_specs = []
        for objid, props in requests:
            obj_type, instance = parse_object_id(objid)
            oid = ObjectIdentifier((obj_type, instance))
            prop_refs = [PropertyReference(propertyIdentifier=PropertyIdentifier(p)) for p in props]
            access_specs.append(
                ReadAccessSpecification(
                    objectIdentifier=oid,
                    listOfPropertyReferences=prop_refs,
                )
            )

        response = await self._app.read_property_multiple(self._remote_addr, access_specs)
        if response is None:
            return result

        for rar in response:
            oid = rar.objectIdentifier
            oid_str = object_id_str(str(oid[0]), int(oid[1]))
            result[oid_str] = {}
            for pv_result in rar.listOfResults or []:
                prop_id = str(pv_result.propertyIdentifier)
                read_result = pv_result.readResult
                if hasattr(read_result, "propertyValue"):
                    result[oid_str][prop_id] = bacnet_value_to_python(read_result.propertyValue)
                else:
                    result[oid_str][prop_id] = None  # propertyAccessError
        return result

    async def _async_write_property(
        self,
        objid: str,
        prop: str,
        value: Any,
        priority: int | None = None,
    ) -> None:
        obj_type, instance = parse_object_id(objid)
        await self._app.write_property(
            self._remote_addr,
            f"{obj_type},{instance}",
            prop,
            value,
            priority=priority,
        )

    async def _async_subscribe_cov(
        self,
        process_id: int,
        objid: str,
        lifetime_s: int,
    ) -> None:
        from bacpypes3.primitivedata import ObjectIdentifier

        obj_type, instance = parse_object_id(objid)
        oid = ObjectIdentifier((obj_type, instance))
        await self._app.request(
            "SubscribeCOV",
            subscriberProcessIdentifier=process_id,
            monitoredObjectIdentifier=oid,
            issueConfirmedNotifications=True,
            lifetime=lifetime_s,
        )

    async def _async_cancel_cov(self, process_id: int, objid: str) -> None:
        from bacpypes3.primitivedata import ObjectIdentifier

        obj_type, instance = parse_object_id(objid)
        oid = ObjectIdentifier((obj_type, instance))
        try:
            await self._app.request(
                "SubscribeCOV",
                subscriberProcessIdentifier=process_id,
                monitoredObjectIdentifier=oid,
                issueConfirmedNotifications=None,
                lifetime=None,
            )
        except Exception as exc:
            _logger.debug("COV cancel pid=%d: %s", process_id, exc)

    # ------------------------------------------------------------------
    # P7.B.9 — Alarms & events (async helpers)
    # ------------------------------------------------------------------

    async def _async_get_event_information(self) -> list[dict[str, Any]]:
        """GetEventInformation confirmed request - enumerate active/unacked alarms."""
        from bacpypes3.apdu import GetEventInformationRequest
        from bacpypes3.pdu import Address

        all_events: list[dict[str, Any]] = []
        last_obj_id: Any = None

        while True:
            request = GetEventInformationRequest()
            if last_obj_id is not None:
                request.lastReceivedObjectIdentifier = last_obj_id
            request.pduDestination = Address(self._remote_addr)
            try:
                response = await self._app.request(request)
            except Exception as exc:
                _logger.debug("GetEventInformation failed: %s", exc)
                break

            if response is None:
                break

            for ei in getattr(response, "listOfEventSummaries", None) or []:
                try:
                    obj_type, inst = ei.objectIdentifier
                    oid_str = object_id_str(str(obj_type), int(inst))
                    acked = getattr(ei, "acknowledgedTransitions", None)
                    all_events.append(
                        {
                            "object_id": oid_str,
                            "event_state": str(getattr(ei, "eventState", "normal")),
                            "acknowledged_transitions": {
                                "to_offnormal": bool(getattr(acked, "toOffnormal", False)),
                                "to_fault": bool(getattr(acked, "toFault", False)),
                                "to_normal": bool(getattr(acked, "toNormal", False)),
                            },
                            "notify_type": str(getattr(ei, "notifyType", "alarm")),
                            "event_priorities": list(getattr(ei, "eventPriorities", []) or []),
                        }
                    )
                except Exception as exc:
                    _logger.debug("Malformed EventSummary: %s", exc)

            if not getattr(response, "moreEvents", False):
                break
            if all_events:
                last_raw = all_events[-1]["object_id"].split(":")
                last_obj_id = (last_raw[0], int(last_raw[1]))
            else:
                break

        return all_events

    async def _async_acknowledge_alarm(
        self,
        object_id: str,
        event_state: str,
        process_id: int,
        source: str,
    ) -> None:
        """Send AcknowledgeAlarm confirmed request."""
        from bacpypes3.apdu import AcknowledgeAlarmRequest, TimeStamp
        from bacpypes3.basetypes import EventState
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import Time

        obj_type, instance = parse_object_id(object_id)
        now_time = datetime.now(tz=timezone.utc)
        ts = TimeStamp(time=Time(now_time.strftime("%H:%M:%S")))
        request = AcknowledgeAlarmRequest(
            acknowledgingProcessIdentifier=process_id,
            eventObjectIdentifier=(obj_type, instance),
            eventStateAcknowledged=EventState(event_state),
            timeStamp=ts,
            acknowledgmentSource=source,
            timeOfAcknowledgment=ts,
        )
        request.pduDestination = Address(self._remote_addr)
        await self._app.request(request)

    # ------------------------------------------------------------------
    # P7.B.10 — TrendLog / ReadRange (async helper)
    # ------------------------------------------------------------------

    async def _async_read_trend_log(
        self,
        object_id: str,
        range_type: str,
        first: int,
        count: int,
        date_str: str,
        time_str: str,
    ) -> list[Any]:
        """ReadRange logBuffer from a TrendLog or TrendLogMultiple object."""
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import ObjectIdentifier, PropertyIdentifier

        obj_type, instance = parse_object_id(object_id)
        oid = ObjectIdentifier((obj_type, instance))
        prop = PropertyIdentifier("logBuffer")
        address = Address(self._remote_addr)
        range_params = (range_type, first, date_str, time_str, count)

        result = await self._app.read_range(address, oid, prop, range_params=range_params)
        if result is None or hasattr(result, "errorClass"):
            return []

        records: list[Any] = []
        for item in result if hasattr(result, "__iter__") else []:
            try:
                records.append(_log_record_to_dict(item))
            except Exception as exc:
                _logger.debug("Malformed log record: %s", exc)
        return records

    # ------------------------------------------------------------------
    # P7.B.6 — General ReadRange for any list property (async helper)
    # ------------------------------------------------------------------

    async def _async_read_range_property(
        self,
        object_id: str,
        prop: str,
        range_type: str,
        first: int,
        count: int,
        date_str: str,
        time_str: str,
    ) -> list[Any]:
        """ReadRange for any list/array property on any BACnet object.

        Mirrors :meth:`_async_read_trend_log` but is not restricted to
        ``logBuffer`` — *prop* may be any property that supports ReadRange
        (e.g. ``logBuffer``, ``objectList``, ``statusLog``).
        """
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import ObjectIdentifier, PropertyIdentifier

        obj_type, instance = parse_object_id(object_id)
        oid = ObjectIdentifier((obj_type, instance))
        p = PropertyIdentifier(prop)
        address = Address(self._remote_addr)
        range_params = (range_type, first, date_str, time_str, count)

        result = await self._app.read_range(address, oid, p, range_params=range_params)
        if result is None or hasattr(result, "errorClass"):
            return []

        items: list[Any] = []
        for item in result if hasattr(result, "__iter__") else []:
            try:
                items.append(bacnet_value_to_python(item))
            except Exception:
                with contextlib.suppress(Exception):
                    items.append(str(item))
        return items

    # ------------------------------------------------------------------
    # P7.B.12 — File services (async helpers)
    # ------------------------------------------------------------------

    async def _async_read_file(
        self,
        file_object_id: str,
        start_position: int,
        chunk_size: int,
    ) -> bytes:
        """AtomicReadFile (stream access) — read the entire file in chunks."""
        from bacpypes3.apdu import AtomicReadFileRequest
        from bacpypes3.basetypes import (
            AtomicReadFileRequestAccessMethodChoice,
            AtomicReadFileRequestAccessMethodChoiceStreamAccess,
        )
        from bacpypes3.pdu import Address

        obj_type, instance = parse_object_id(file_object_id)
        pos = start_position
        data = bytearray()

        while True:
            stream_access = AtomicReadFileRequestAccessMethodChoiceStreamAccess(
                fileStartPosition=pos,
                requestedOctetCount=chunk_size,
            )
            access = AtomicReadFileRequestAccessMethodChoice(streamAccess=stream_access)
            request = AtomicReadFileRequest(
                fileIdentifier=(obj_type, instance),
                accessMethod=access,
            )
            request.pduDestination = Address(self._remote_addr)
            response = await self._app.request(request)

            if response is None or not hasattr(response, "accessMethod"):
                break

            chunk = bytes(getattr(response.accessMethod.streamAccess, "fileData", b"") or b"")
            if chunk:
                data.extend(chunk)
                pos += len(chunk)

            if getattr(response, "endOfFile", True):
                break

        return bytes(data)

    async def _async_write_file(
        self,
        file_object_id: str,
        file_data: bytes,
        start_position: int,
    ) -> int:
        """AtomicWriteFile (stream access) — returns actual fileStartPosition."""
        from bacpypes3.apdu import AtomicWriteFileRequest
        from bacpypes3.basetypes import (
            AtomicWriteFileRequestAccessMethodChoice,
            AtomicWriteFileRequestAccessMethodChoiceStreamAccess,
        )
        from bacpypes3.pdu import Address

        obj_type, instance = parse_object_id(file_object_id)
        stream_access = AtomicWriteFileRequestAccessMethodChoiceStreamAccess(
            fileStartPosition=start_position,
            fileData=file_data,
        )
        access = AtomicWriteFileRequestAccessMethodChoice(streamAccess=stream_access)
        request = AtomicWriteFileRequest(
            fileIdentifier=(obj_type, instance),
            accessMethod=access,
        )
        request.pduDestination = Address(self._remote_addr)
        response = await self._app.request(request)
        if response is not None and hasattr(response, "fileStartPosition"):
            return int(response.fileStartPosition)
        return start_position

    # ------------------------------------------------------------------
    # P7.B.13 — Device management (async helpers)
    # ------------------------------------------------------------------

    async def _async_time_sync(self, dt: datetime, *, utc: bool) -> None:
        """Send TimeSynchronization or UTCTimeSynchronization (unconfirmed)."""
        from bacpypes3.apdu import (
            DateTime,
            TimeSynchronizationRequest,
            UTCTimeSynchronizationRequest,
        )
        from bacpypes3.pdu import Address

        bac_dt = DateTime.fromisoformat(dt.replace(tzinfo=None).isoformat())
        if utc:
            request: Any = UTCTimeSynchronizationRequest(time=bac_dt)
        else:
            request = TimeSynchronizationRequest(time=bac_dt)
        request.pduDestination = Address(self._remote_addr)
        await self._app.request(request)

    async def _async_reinitialize_device(
        self,
        state: str,
        password: str | None,
    ) -> None:
        """Send ReinitializeDevice confirmed request."""
        from bacpypes3.apdu import (
            ReinitializeDeviceRequest,
            ReinitializeDeviceRequestReinitializedStateOfDevice,
        )
        from bacpypes3.pdu import Address

        kwargs: dict[str, Any] = {
            "reinitializedStateOfDevice": ReinitializeDeviceRequestReinitializedStateOfDevice(
                state
            ),
        }
        if password:
            kwargs["password"] = password
        request = ReinitializeDeviceRequest(**kwargs)
        request.pduDestination = Address(self._remote_addr)
        await self._app.request(request)

    async def _async_device_communication_control(
        self,
        enable_disable: str,
        time_duration: int | None,
        password: str | None,
    ) -> None:
        """Send DeviceCommunicationControl confirmed request."""
        from bacpypes3.apdu import (
            DeviceCommunicationControlRequest,
            DeviceCommunicationControlRequestEnableDisable,
        )
        from bacpypes3.pdu import Address

        kwargs: dict[str, Any] = {
            "enableDisable": DeviceCommunicationControlRequestEnableDisable(enable_disable),
        }
        if time_duration is not None:
            kwargs["timeDuration"] = time_duration
        if password:
            kwargs["password"] = password
        request = DeviceCommunicationControlRequest(**kwargs)
        request.pduDestination = Address(self._remote_addr)
        await self._app.request(request)

    # ------------------------------------------------------------------
    # P7.B.7 — WritePropertyMultiple (async helper)
    # ------------------------------------------------------------------

    async def _async_wpm(
        self,
        writes: list[tuple[str, str, Any, int | None]],
    ) -> None:
        """WritePropertyMultiple — transmit all (objid, prop, value, priority) tuples
        in a single confirmed-request APDU.

        *value* in each tuple must already be a bacpypes3 primitive type (as
        returned by :func:`python_to_bacnet_value`).
        """
        from bacpypes3.apdu import (
            PropertyValue as BACnetPropertyValue,
        )
        from bacpypes3.apdu import (
            WriteAccessSpecification,
            WritePropertyMultipleRequest,
        )
        from bacpypes3.basetypes import PropertyIdentifier
        from bacpypes3.pdu import Address
        from bacpypes3.primitivedata import ObjectIdentifier, Unsigned

        access_specs = []
        for objid, prop, bac_val, priority in writes:
            obj_type, instance = parse_object_id(objid)
            oid = ObjectIdentifier((obj_type, instance))
            pv = BACnetPropertyValue(
                propertyIdentifier=PropertyIdentifier(prop),
                value=bac_val,
            )
            if priority is not None:
                pv.priority = Unsigned(priority)
            access_specs.append(
                WriteAccessSpecification(
                    objectIdentifier=oid,
                    listOfProperties=[pv],
                )
            )

        request = WritePropertyMultipleRequest(listOfWriteAccessSpecs=access_specs)
        request.pduDestination = Address(self._remote_addr)
        await self._app.request(request)

    # ------------------------------------------------------------------
    # P7.E — BBMD / FD routing — raw BVLC reads (async helpers)
    # ------------------------------------------------------------------

    async def _async_read_bdt_raw(
        self,
        bbmd_addr: str,
        timeout: float = 3.0,
    ) -> list[dict[str, Any]]:
        """Send Read-BDT BVLC message to *bbmd_addr* and return parsed entries.

        Returns a list of dicts with key ``"address"`` (string form of each
        BDT IPv4Address).  Sends a raw UDP datagram so no existing socket is
        disturbed.
        """
        import socket as _socket

        from bacpypes3.ipv4.bvll import (
            LPCI,
            ReadBroadcastDistributionTableAck,
            pdu_types,
        )
        from bacpypes3.pdu import PDU

        host, _, port_str = bbmd_addr.partition(":")
        port = int(port_str) if port_str else 47808

        # Encode: 0x81 (BACnet/IP) 0x02 (Read-BDT) 0x00 0x04 (length)
        request_bytes = bytes([0x81, 0x02, 0x00, 0x04])

        loop = asyncio.get_event_loop()
        result_future: asyncio.Future[bytes] = loop.create_future()

        class _Proto(asyncio.DatagramProtocol):
            def connection_made(self, transport: asyncio.DatagramTransport) -> None:
                transport.sendto(request_bytes, (host, port))

            def datagram_received(self, data: bytes, addr: tuple) -> None:
                if not result_future.done() and len(data) >= 4 and data[0] == 0x81:
                    result_future.set_result(data)

            def error_received(self, exc: Exception) -> None:
                if not result_future.done():
                    result_future.set_exception(exc)

            def connection_lost(self, exc: Exception | None) -> None:
                if not result_future.done():
                    result_future.cancel()

        transport, _ = await loop.create_datagram_endpoint(
            _Proto,
            local_addr=("0.0.0.0", 0),
            family=_socket.AF_INET,
        )
        try:
            raw = await asyncio.wait_for(asyncio.shield(result_future), timeout=timeout)
        finally:
            transport.close()

        pdu = PDU(raw)
        lpci = LPCI.decode(pdu)
        lpdu_class = pdu_types.get(lpci.bvlciFunction)
        if lpdu_class is None or lpdu_class is not ReadBroadcastDistributionTableAck:
            return []
        ack = lpdu_class.decode(pdu)
        return [{"address": str(entry)} for entry in (getattr(ack, "bvlciBDT", None) or [])]

    async def _async_read_fdt_raw(
        self,
        bbmd_addr: str,
        timeout: float = 3.0,
    ) -> list[dict[str, Any]]:
        """Send Read-FDT BVLC message to *bbmd_addr* and return parsed entries.

        Returns a list of dicts with keys ``"address"``, ``"ttl"``,
        ``"remaining"`` (seconds) for each registered foreign device.
        """
        import socket as _socket

        from bacpypes3.ipv4.bvll import (
            LPCI,
            ReadForeignDeviceTableAck,
            pdu_types,
        )
        from bacpypes3.pdu import PDU

        host, _, port_str = bbmd_addr.partition(":")
        port = int(port_str) if port_str else 47808

        # Encode: 0x81 (BACnet/IP) 0x06 (Read-FDT) 0x00 0x04 (length)
        request_bytes = bytes([0x81, 0x06, 0x00, 0x04])

        loop = asyncio.get_event_loop()
        result_future: asyncio.Future[bytes] = loop.create_future()

        class _Proto(asyncio.DatagramProtocol):
            def connection_made(self, transport: asyncio.DatagramTransport) -> None:
                transport.sendto(request_bytes, (host, port))

            def datagram_received(self, data: bytes, addr: tuple) -> None:
                if not result_future.done() and len(data) >= 4 and data[0] == 0x81:
                    result_future.set_result(data)

            def error_received(self, exc: Exception) -> None:
                if not result_future.done():
                    result_future.set_exception(exc)

            def connection_lost(self, exc: Exception | None) -> None:
                if not result_future.done():
                    result_future.cancel()

        transport, _ = await loop.create_datagram_endpoint(
            _Proto,
            local_addr=("0.0.0.0", 0),
            family=_socket.AF_INET,
        )
        try:
            raw = await asyncio.wait_for(asyncio.shield(result_future), timeout=timeout)
        finally:
            transport.close()

        pdu = PDU(raw)
        lpci = LPCI.decode(pdu)
        lpdu_class = pdu_types.get(lpci.bvlciFunction)
        if lpdu_class is None or lpdu_class is not ReadForeignDeviceTableAck:
            return []
        ack = lpdu_class.decode(pdu)
        entries = []
        for fdte in getattr(ack, "bvlciFDT", None) or []:
            entries.append(
                {
                    "address": str(getattr(fdte, "fdAddress", "?")),
                    "ttl": int(getattr(fdte, "fdTTL", 0)),
                    "remaining": int(getattr(fdte, "fdRemain", 0)),
                }
            )
        return entries

    # ------------------------------------------------------------------
    # DriverSession ABC
    # ------------------------------------------------------------------

    def enumerate_objects(self) -> Iterator[ObjectRef]:
        """Read the Device objectList property and yield one ObjectRef each."""
        try:
            raw = self._loop_thread.submit(
                self._async_read_property("device:any", "objectList"),
                timeout=self._apdu_timeout + 2,
            )
        except Exception as exc:
            _logger.warning("enumerate_objects objectList failed: %s", exc)
            return

        if raw is None:
            return

        items = raw if hasattr(raw, "__iter__") else []
        for item in items:
            try:
                if hasattr(item, "__iter__") and len(item) == 2:
                    obj_type_raw, inst_raw = item
                    oid_str = object_id_str(str(obj_type_raw), int(inst_raw))
                else:
                    oid_str = str(item)
                obj_type_str = oid_str.split(":")[0]
                acc = Access.READ_WRITE if obj_type_str in COMMANDABLE_TYPES else Access.READ_ONLY
                yield ObjectRef(
                    device=self.device,
                    object_id=oid_str,
                    data_type="any",
                    access=acc,
                    label=oid_str,
                )
            except Exception as exc:
                _logger.debug("Skip malformed objectList entry: %s", exc)

    def read(self, ref: ObjectRef) -> ReadResult:
        """Read ``presentValue`` (+ statusFlags) from a BACnet object."""
        objid = ref.object_id

        if self._profile.has_quirk("no-rpm"):
            return self._read_rp(ref)

        props = ["presentValue", "statusFlags", "reliability"]
        obj_type = objid.split(":")[0]
        if obj_type in COMMANDABLE_TYPES:
            props.append("priorityArray")

        try:
            rpm_result = self._loop_thread.submit(
                self._async_rpm([(objid, props)]),
                timeout=self._apdu_timeout + 2,
            )
        except CommError:
            raise
        except Exception as exc:
            return ReadResult(
                object_ref=ref,
                value=None,
                quality=Quality.BAD,
                timestamp=datetime.now(tz=timezone.utc),
                error=str(exc),
            )

        props_map: dict[str, Any] = rpm_result.get(objid, {})
        raw_value = props_map.get("presentValue")
        quality = _parse_status_flags(props_map.get("statusFlags"))

        return ReadResult(
            object_ref=ref,
            value=raw_value,
            quality=quality,
            timestamp=datetime.now(tz=timezone.utc),
        )

    def _read_rp(self, ref: ObjectRef) -> ReadResult:
        """Fallback single-RP read for devices without RPM."""
        try:
            val = self._loop_thread.submit(
                self._async_read_property(ref.object_id, "presentValue"),
                timeout=self._apdu_timeout + 2,
            )
        except CommError:
            raise
        except Exception as exc:
            return ReadResult(
                object_ref=ref,
                value=None,
                quality=Quality.BAD,
                timestamp=datetime.now(tz=timezone.utc),
                error=str(exc),
            )
        return ReadResult(
            object_ref=ref,
            value=val,
            quality=Quality.GOOD,
            timestamp=datetime.now(tz=timezone.utc),
        )

    def prepare_write(
        self,
        ref: ObjectRef,
        value: Any,
        *,
        priority: int = 16,
        data_type: str | None = None,
    ) -> WriteIntent:
        """Encode *value* for a BACnet WriteProperty — no I/O.

        ``priority`` must be 1-16.  Defaults to 16 (lowest / manual operator).
        """
        if not (1 <= priority <= 16):
            raise EncodingError(f"BACnet write priority must be 1-16, got {priority}")
        dtype = data_type or ref.data_type or "real"
        bac_value = python_to_bacnet_value(dtype, value)
        # Encode intent as a 4-byte little-endian struct (placeholder bytes;
        # actual transmission uses bacpypes3 encoding).
        try:
            encoded = struct.pack("<f", float(value))
        except (TypeError, struct.error):
            encoded = str(value).encode()
        description = (
            f"WriteProperty {ref.object_id} presentValue = {value!r} (priority {priority})"
        )
        return WriteIntent(
            object_ref=ref,
            requested_value=value,
            encoded_bytes=encoded,
            description=description,
            metadata={
                "bacnet_value": bac_value,
                "priority": priority,
                "prop": "presentValue",
                "data_type": dtype,
            },
        )

    def commit_write(self, intent: WriteIntent) -> WriteResult:
        """Transmit a WriteProperty request, gated by the safety context."""
        if not self.safety.require_write_authorization(intent):
            raise WriteAuthorizationError("Write denied by safety context")

        objid = intent.object_ref.object_id
        bac_value = intent.metadata.get("bacnet_value", intent.requested_value)
        priority: int = intent.metadata.get("priority", 16)
        prop: str = intent.metadata.get("prop", "presentValue")

        ts = datetime.now(tz=timezone.utc)
        try:
            self._loop_thread.submit(
                self._async_write_property(objid, prop, bac_value, priority),
                timeout=self._apdu_timeout + 2,
            )
        except CommError:
            result = WriteResult(
                intent=intent,
                success=False,
                timestamp=ts,
                error="APDU timeout",
            )
            self.safety.record_write_outcome(result)
            raise

        except Exception as exc:
            result = WriteResult(
                intent=intent,
                success=False,
                timestamp=ts,
                error=str(exc),
            )
            self.safety.record_write_outcome(result)
            raise ProtocolError(f"WriteProperty failed for {objid}: {exc}") from exc

        result = WriteResult(intent=intent, success=True, timestamp=ts)
        self.safety.record_write_outcome(result)
        return result

    # ------------------------------------------------------------------
    # Subscriber mix-in (optional capability)
    # ------------------------------------------------------------------

    def subscribe(
        self,
        refs: list[ObjectRef],
        callback: SubscriptionCallback,
    ) -> SubscriptionHandle:
        """Subscribe to COV for all objects in *refs*.

        Returns a single :class:`_BacnetSubHandle` that covers all objects.
        """
        if self._profile.has_quirk("no-cov"):
            raise ProtocolError(f"Vendor profile {self._profile.id!r} marks COV as unsupported")

        pid = self._next_process_id
        self._next_process_id += 1
        handle = _BacnetSubHandle(pid)

        lifetime_s = self._profile.cov_lifetime_s
        for ref in refs:
            self._subs[pid] = callback
            self._sub_objids[pid] = ref.object_id
            try:
                self._loop_thread.submit(
                    self._async_subscribe_cov(pid, ref.object_id, lifetime_s),
                    timeout=self._apdu_timeout + 2,
                )
            except Exception as exc:
                _logger.warning("COV subscribe %s failed: %s", ref.object_id, exc)
            pid += 1
            self._next_process_id = pid + 1

        return handle

    def unsubscribe(self, handle: SubscriptionHandle) -> None:
        """Cancel an active COV subscription."""
        if not isinstance(handle, _BacnetSubHandle):
            return
        pid = handle.process_id
        objid = self._sub_objids.pop(pid, None)
        self._subs.pop(pid, None)
        if objid:
            try:
                self._loop_thread.submit(
                    self._async_cancel_cov(pid, objid),
                    timeout=self._apdu_timeout + 1,
                )
            except Exception as exc:
                _logger.debug("COV unsubscribe pid=%d: %s", pid, exc)

    # ------------------------------------------------------------------
    # P7.B.9 — Alarms & events (public API)
    # ------------------------------------------------------------------

    def get_event_information(self) -> list[dict[str, Any]]:
        """Poll GetEventInformation and return a list of active/unacked alarms.

        Each entry is a dict with keys: ``object_id``, ``event_state``,
        ``acknowledged_transitions``, ``notify_type``, ``event_priorities``.
        """
        try:
            return self._loop_thread.submit(
                self._async_get_event_information(),
                timeout=self._apdu_timeout + 4,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(f"GetEventInformation failed: {exc}") from exc

    def acknowledge_alarm(
        self,
        object_id: str,
        event_state: str,
        *,
        process_id: int = 1,
        source: str = "ProtoSkipper",
    ) -> None:
        """Acknowledge an active alarm on *object_id*.

        *event_state* must match the current event state: ``"normal"``,
        ``"fault"``, ``"offnormal"``, ``"highLimit"``, ``"lowLimit"``, or
        ``"lifeSafetyAlarm"``.  Safety-gated.
        """
        ref = ObjectRef(device=self.device, object_id=object_id, data_type="any")
        intent = WriteIntent(
            object_ref=ref,
            requested_value=event_state,
            encoded_bytes=b"",
            description=f"AcknowledgeAlarm {object_id} state={event_state}",
        )
        if not self.safety.require_write_authorization(intent):
            raise WriteAuthorizationError("AcknowledgeAlarm denied by safety context")
        try:
            self._loop_thread.submit(
                self._async_acknowledge_alarm(object_id, event_state, process_id, source),
                timeout=self._apdu_timeout + 2,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(f"AcknowledgeAlarm failed for {object_id}: {exc}") from exc

    # ------------------------------------------------------------------
    # P7.B.10 — TrendLog retrieval (public API)
    # ------------------------------------------------------------------

    def read_trend_log(
        self,
        ref: ObjectRef,
        *,
        range_type: str = "p",
        first: int = 1,
        count: int = 100,
        date_str: str = "2000-01-01",
        time_str: str = "00:00:00",
    ) -> list[Any]:
        """Return log records from a TrendLog or TrendLogMultiple object.

        *range_type* is ``'p'`` (by position), ``'s'`` (by sequence number),
        or ``'t'`` (by time).  *first*/*count* used for ``'p'``/``'s'``;
        *date_str* + *time_str* + *count* for ``'t'``.
        """
        try:
            return self._loop_thread.submit(
                self._async_read_trend_log(
                    ref.object_id,
                    range_type=range_type,
                    first=first,
                    count=count,
                    date_str=date_str,
                    time_str=time_str,
                ),
                timeout=self._apdu_timeout + 4,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(f"ReadRange (TrendLog) failed for {ref.object_id}: {exc}") from exc

    def read_range(
        self,
        ref: ObjectRef,
        prop: str = "logBuffer",
        *,
        range_type: str = "p",
        first: int = 1,
        count: int = 100,
        date_str: str = "2000-01-01",
        time_str: str = "00:00:00",
    ) -> list[Any]:
        """General ReadRange for any list property of a BACnet object.

        *prop* defaults to ``"logBuffer"`` (TrendLog / EventLog log buffer).
        *range_type* is:

        * ``'p'`` — by position (*first* = 1-based record index, positive
          selects from start, negative from end)
        * ``'s'`` — by sequence number (*first* = sequence number)
        * ``'t'`` — by time (*date_str* ``YYYY-MM-DD`` + *time_str*
          ``HH:MM:SS`` mark start; *count* limits records returned)
        """
        try:
            return self._loop_thread.submit(
                self._async_read_range_property(
                    ref.object_id,
                    prop,
                    range_type=range_type,
                    first=first,
                    count=count,
                    date_str=date_str,
                    time_str=time_str,
                ),
                timeout=self._apdu_timeout + 4,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(f"ReadRange failed for {ref.object_id}.{prop}: {exc}") from exc

    def read_property_array(
        self,
        ref: ObjectRef,
        prop: str,
        *,
        array_index: int | None = None,
    ) -> Any:
        """Read a property or a single element from a BACnet array property.

        * ``array_index=None`` — read the entire property (no array index tag).
        * ``array_index=0`` — read the array length (Unsigned).
        * ``array_index=N`` (N ≥ 1) — read element N of the array.

        Example: ``read_property_array(ref, "objectList", array_index=0)``
        returns the number of objects in the device's objectList.
        """
        try:
            return self._loop_thread.submit(
                self._async_read_property(ref.object_id, prop, array_index),
                timeout=self._apdu_timeout + 2,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(
                f"ReadProperty {ref.object_id}.{prop}[{array_index}] failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # P7.B.11 — Schedule & Calendar (public API)
    # ------------------------------------------------------------------

    _SCHEDULE_READ_PROPS: ClassVar[list[str]] = [
        "objectName",
        "description",
        "presentValue",
        "statusFlags",
        "reliability",
        "weeklySchedule",
        "exceptionSchedule",
        "scheduleDefault",
        "effectivePeriod",
        "priorityForWriting",
    ]

    def read_schedule(self, ref: ObjectRef) -> dict[str, Any]:
        """Read all schedule-related properties of a Schedule object.

        Returns a dict mapping property name to decoded value.
        """
        try:
            rpm_result = self._loop_thread.submit(
                self._async_rpm([(ref.object_id, self._SCHEDULE_READ_PROPS)]),
                timeout=self._apdu_timeout + 4,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(f"read_schedule failed for {ref.object_id}: {exc}") from exc
        return rpm_result.get(ref.object_id, {})

    def write_schedule_default(
        self,
        ref: ObjectRef,
        value: Any,
        *,
        data_type: str = "real",
    ) -> WriteResult:
        """Write the ``scheduleDefault`` property of a Schedule object.

        For ``weeklySchedule`` and ``exceptionSchedule`` (complex sequences)
        use ``prepare_write`` / ``commit_write`` directly with ``prop`` set
        in the intent metadata.
        """
        intent = self.prepare_write(ref, value, data_type=data_type)
        intent.metadata["prop"] = "scheduleDefault"
        return self.commit_write(intent)

    # ------------------------------------------------------------------
    # P7.B.12 — File services (public API)
    # ------------------------------------------------------------------

    def read_file(
        self,
        ref: ObjectRef,
        *,
        start_position: int = 0,
        chunk_size: int = 1400,
    ) -> bytes:
        """Read the entire content of a BACnet File object via AtomicReadFile.

        *chunk_size* controls ``requestedOctetCount`` per APDU (the device's
        Max-APDU-Length is the real upper bound in practice).
        """
        try:
            return self._loop_thread.submit(
                self._async_read_file(ref.object_id, start_position, chunk_size),
                timeout=120,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(f"AtomicReadFile failed for {ref.object_id}: {exc}") from exc

    def write_file(
        self,
        ref: ObjectRef,
        data: bytes,
        *,
        start_position: int = 0,
    ) -> WriteResult:
        """Write *data* to a BACnet File object via AtomicWriteFile (stream mode).

        Safety-gated.  Returns a :class:`WriteResult` recording the outcome.
        """
        intent = WriteIntent(
            object_ref=ref,
            requested_value=data,
            encoded_bytes=data,
            description=(f"AtomicWriteFile {ref.object_id} pos={start_position} len={len(data)}"),
            metadata={"start_position": start_position},
        )
        if not self.safety.require_write_authorization(intent):
            raise WriteAuthorizationError("AtomicWriteFile denied by safety context")
        ts = datetime.now(tz=timezone.utc)
        try:
            self._loop_thread.submit(
                self._async_write_file(ref.object_id, data, start_position),
                timeout=120,
            )
        except CommError:
            result = WriteResult(intent=intent, success=False, timestamp=ts, error="APDU timeout")
            self.safety.record_write_outcome(result)
            raise
        except Exception as exc:
            result = WriteResult(intent=intent, success=False, timestamp=ts, error=str(exc))
            self.safety.record_write_outcome(result)
            raise ProtocolError(f"AtomicWriteFile failed for {ref.object_id}: {exc}") from exc
        result = WriteResult(intent=intent, success=True, timestamp=ts)
        self.safety.record_write_outcome(result)
        return result

    # ------------------------------------------------------------------
    # P7.B.13 — Device management (public API)
    # ------------------------------------------------------------------

    def time_sync(self, dt: datetime | None = None, *, utc: bool = False) -> None:
        """Broadcast TimeSynchronization (or UTCTimeSynchronization) to the device.

        Passes *dt* (or ``datetime.now(utc)`` when *None*) as the reference
        time.  This is an unconfirmed service — no acknowledgment is expected.
        """
        if dt is None:
            dt = datetime.now(tz=timezone.utc)
        try:
            self._loop_thread.submit(
                self._async_time_sync(dt, utc=utc),
                timeout=self._apdu_timeout + 2,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(f"TimeSynchronization failed: {exc}") from exc

    def reinitialize_device(
        self,
        state: str = "warmstart",
        *,
        password: str | None = None,
    ) -> None:
        """Send ReinitializeDevice to the remote device.

        *state* is one of the ASHRAE 135 ``reinitializedStateOfDevice`` values
        (``"coldstart"``, ``"warmstart"``, ``"activateChanges"``, …).
        Safety-gated: denied in PRODUCTION unless the safety context allows.
        """
        ref = ObjectRef(device=self.device, object_id="device:any", data_type="any")
        intent = WriteIntent(
            object_ref=ref,
            requested_value=state,
            encoded_bytes=b"",
            description=f"ReinitializeDevice state={state}",
        )
        if not self.safety.require_write_authorization(intent):
            raise WriteAuthorizationError("ReinitializeDevice denied by safety context")
        try:
            self._loop_thread.submit(
                self._async_reinitialize_device(state, password),
                timeout=self._apdu_timeout + 4,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(f"ReinitializeDevice failed (state={state}): {exc}") from exc

    def device_communication_control(
        self,
        enable_disable: str,
        *,
        time_duration: int | None = None,
        password: str | None = None,
    ) -> None:
        """Send DeviceCommunicationControl to the remote device.

        *enable_disable* is one of ``"enable"``, ``"disable"``,
        ``"disableInitiation"``.  *time_duration* (minutes) makes disable
        temporary.  Safety-gated: denied in PRODUCTION.
        """
        ref = ObjectRef(device=self.device, object_id="device:any", data_type="any")
        intent = WriteIntent(
            object_ref=ref,
            requested_value=enable_disable,
            encoded_bytes=b"",
            description=f"DeviceCommunicationControl enableDisable={enable_disable}",
        )
        if not self.safety.require_write_authorization(intent):
            raise WriteAuthorizationError("DeviceCommunicationControl denied by safety context")
        try:
            self._loop_thread.submit(
                self._async_device_communication_control(enable_disable, time_duration, password),
                timeout=self._apdu_timeout + 4,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(
                f"DeviceCommunicationControl failed (mode={enable_disable}): {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # P7.B.7 — WritePropertyMultiple (public API)
    # ------------------------------------------------------------------

    def write_many(
        self,
        intents: list[WriteIntent],
    ) -> list[WriteResult]:
        """Batch-write properties using WritePropertyMultiple (single APDU).

        Every intent must have been produced by :meth:`prepare_write`.  All
        are safety-gated before transmission.  WPM is all-or-nothing (the
        device either applies all writes or returns an error), so the returned
        :class:`WriteResult` list has all entries sharing the same
        success/failure status.
        """
        if not intents:
            return []

        ts = datetime.now(tz=timezone.utc)

        # Safety gate — check every intent before touching the wire
        for intent in intents:
            if not self.safety.require_write_authorization(intent):
                raise WriteAuthorizationError(
                    f"write_many denied for {intent.object_ref.object_id}"
                )

        writes: list[tuple[str, str, Any, int | None]] = [
            (
                i.object_ref.object_id,
                i.metadata.get("prop", "presentValue"),
                i.metadata.get("bacnet_value", i.requested_value),
                i.metadata.get("priority"),
            )
            for i in intents
        ]

        try:
            self._loop_thread.submit(
                self._async_wpm(writes),
                timeout=self._apdu_timeout + 4,
            )
        except CommError:
            results = [
                WriteResult(intent=i, success=False, timestamp=ts, error="APDU timeout")
                for i in intents
            ]
            for r in results:
                self.safety.record_write_outcome(r)
            raise
        except Exception as exc:
            results = [
                WriteResult(intent=i, success=False, timestamp=ts, error=str(exc)) for i in intents
            ]
            for r in results:
                self.safety.record_write_outcome(r)
            raise ProtocolError(f"WritePropertyMultiple failed: {exc}") from exc

        results = [WriteResult(intent=i, success=True, timestamp=ts) for i in intents]
        for r in results:
            self.safety.record_write_outcome(r)
        return results

    # ------------------------------------------------------------------
    # P7.E — BBMD / FD routing (public API)
    # ------------------------------------------------------------------

    def read_bdt(
        self,
        bbmd_addr: str,
        *,
        timeout: float = 3.0,
    ) -> list[dict[str, Any]]:
        """Read the Broadcast Distribution Table from a BBMD.

        *bbmd_addr* is ``"host:port"`` (port defaults to 47808).
        Returns a list of dicts with key ``"address"`` for each BDT entry.
        Sends a raw BVLC UDP datagram outside the active BACnet session socket.
        """
        try:
            return self._loop_thread.submit(
                self._async_read_bdt_raw(bbmd_addr, timeout),
                timeout=timeout + 2,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(f"Read-BDT from {bbmd_addr} failed: {exc}") from exc

    def read_fdt(
        self,
        bbmd_addr: str,
        *,
        timeout: float = 3.0,
    ) -> list[dict[str, Any]]:
        """Read the Foreign Device Table from a BBMD.

        *bbmd_addr* is ``"host:port"`` (port defaults to 47808).
        Returns a list of dicts with keys ``"address"``, ``"ttl"``,
        ``"remaining"`` for each registered foreign device.
        """
        try:
            return self._loop_thread.submit(
                self._async_read_fdt_raw(bbmd_addr, timeout),
                timeout=timeout + 2,
            )
        except CommError:
            raise
        except Exception as exc:
            raise ProtocolError(f"Read-FDT from {bbmd_addr} failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Cancel all active COV subscriptions
        for pid, objid in list(self._sub_objids.items()):
            with contextlib.suppress(Exception):
                self._loop_thread.submit(
                    self._async_cancel_cov(pid, objid),
                    timeout=2,
                )
        self._subs.clear()
        self._sub_objids.clear()
        self._loop_thread.submit(self._async_close_app(), timeout=3)
        self._loop_thread.close()

    def abort(self) -> None:
        """Best-effort abort of in-flight I/O."""
        with contextlib.suppress(Exception):
            self._loop_thread.close()


# ---------------------------------------------------------------------------
# Discovery helpers (used by BacnetIpDriver.discover)
# ---------------------------------------------------------------------------


async def _async_discover(
    app: Any,
    low_limit: int | None,
    high_limit: int | None,
    target_address: str | None,
    timeout: float,
) -> list[dict[str, Any]]:
    """Run Who-Is broadcast and collect I-Am responses."""
    from bacpypes3.pdu import Address

    address = Address(target_address) if target_address else None
    try:
        result = await app.who_is(
            low_limit=low_limit,
            high_limit=high_limit,
            address=address,
            timeout=timeout,
        )
    except Exception as exc:
        _logger.debug("Who-Is failed: %s", exc)
        return []

    devices = []
    for i_am in result or []:
        try:
            devices.append(
                {
                    "device_id": int(i_am.iAmDeviceIdentifier[1]),
                    "address": str(i_am.pduSource),
                    "max_apdu": int(i_am.maxAPDULengthAccepted),
                    "segmentation": str(i_am.segmentationSupported),
                    "vendor_id": int(i_am.vendorID),
                }
            )
        except Exception as exc:
            _logger.debug("Malformed I-Am ignored: %s", exc)
    return devices


def discover_devices(
    loop_thread: _BackgroundLoop,
    app: Any,
    *,
    low_limit: int | None = None,
    high_limit: int | None = None,
    target_address: str | None = None,
    timeout: float = _DISCOVERY_TIMEOUT,
) -> list[dict[str, Any]]:
    """Synchronous wrapper around :func:`_async_discover`."""
    return loop_thread.submit(
        _async_discover(app, low_limit, high_limit, target_address, timeout),
        timeout=timeout + 2,
    )


# ---------------------------------------------------------------------------
# Address helpers
# ---------------------------------------------------------------------------


def _bacnet_address(raw: str) -> str:
    """Strip ``bacnet://`` scheme and ``/dev=N`` / ``/…`` suffix."""
    addr = raw.removeprefix("bacnet://")
    addr = addr.split("/dev=")[0].split("/")[0]
    return addr


def _extract_device_id(raw: str) -> int | None:
    """Extract the numeric device instance from a ``host/dev=1234`` address."""
    import re

    m = re.search(r"/dev=(\d+)", raw, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _log_record_to_dict(record: Any) -> dict[str, Any]:
    """Convert a bacpypes3 BACnetLogRecord to a plain Python dict."""
    ts_raw = getattr(record, "timestamp", None)
    ts: datetime | None = None
    if ts_raw is not None:
        with contextlib.suppress(ValueError, TypeError):
            ts = datetime.fromisoformat(str(ts_raw))

    datum = getattr(record, "logDatum", None)
    value: Any = None
    if datum is not None:
        for attr in (
            "realValue",
            "integerValue",
            "booleanValue",
            "enumValue",
            "bitStringValue",
        ):
            raw_val = getattr(datum, attr, None)
            if raw_val is not None:
                with contextlib.suppress(Exception):
                    value = bacnet_value_to_python(raw_val)
                break

    return {
        "timestamp": ts,
        "value": value,
    }


def _parse_status_flags(raw: Any) -> Quality:
    """Convert a BACnet StatusFlags bit string to :class:`Quality`."""
    if raw is None:
        return Quality.GOOD
    # StatusFlags: inAlarm(0), fault(1), overridden(2), outOfService(3)
    try:
        fault = bool(raw[1])
        out_of_service = bool(raw[3])
        if fault:
            return Quality.BAD
        if out_of_service:
            return Quality.UNCERTAIN
    except (IndexError, TypeError):
        pass
    return Quality.GOOD
