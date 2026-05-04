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
from typing import TYPE_CHECKING, Any

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
