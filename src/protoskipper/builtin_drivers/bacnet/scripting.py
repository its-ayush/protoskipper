# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet scripting bindings (P7.H.4).

Provides a high-level, REPL-friendly API over :class:`BacnetIpSession` so
that automation scripts can work with BACnet without managing sessions,
asyncio loops, or safety contexts directly.

Usage::

    from protoskipper.builtin_drivers.bacnet.scripting import BACnetScript

    with BACnetScript("192.168.1.100/dev=1234") as s:
        print(s.who_is(1, 1000))
        val = s.read("analog-value:1")
        s.write("analog-output:2", 22.5, priority=8)
        s.release("analog-output:2", priority=8)
        handle = s.subscribe_cov("analog-value:1", callback=print)
        s.unsubscribe_cov(handle)

Every write operation is gated by the SafetyContext (default: LAB profile).
In PRODUCTION profile the session raises :exc:`AuthorizationDenied`.

The scripting layer is not thread-safe — use from a single thread/REPL.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

_logger = logging.getLogger(__name__)

__all__ = ["BACnetScript"]


class BACnetScript:
    """REPL-friendly synchronous BACnet/IP scripting interface (P7.H.4).

    Parameters
    ----------
    address:
        Target device address string — ``"host:port/dev=N"`` or
        ``"host/dev=N"`` (port defaults to 47808).
    profile:
        Safety profile name: ``"LAB"`` (default), ``"COMMISSIONING"``,
        or ``"PRODUCTION"``.
    operator:
        Operator name embedded in every audit row.
    apdu_timeout:
        APDU timeout in seconds.  Defaults to 6 s.
    """

    def __init__(
        self,
        address: str,
        *,
        profile: str = "LAB",
        operator: str = "script",
        apdu_timeout: float = 6.0,
    ) -> None:
        from protoskipper.builtin_drivers.bacnet.client import BacnetIpSession
        from protoskipper.core.driver import DeviceRef, SafetyContext, SessionProfile

        self._address = address
        device = DeviceRef(protocol="bacnet.ip", address=address, label="script-target")
        try:
            sp = SessionProfile[profile.upper()]
        except KeyError:
            sp = SessionProfile.LAB
        self._safety = SafetyContext(
            profile=sp,
            operator=operator,
        )
        self._session = BacnetIpSession(device, self._safety)
        self._session._apdu_timeout = apdu_timeout

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> BACnetScript:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def who_is(
        self,
        low: int | None = None,
        high: int | None = None,
        *,
        timeout: float = 5.0,
    ) -> list[dict[str, Any]]:
        """Send Who-Is and return a list of I-Am response dicts.

        Each dict has keys: ``device_id``, ``address``, ``max_apdu``,
        ``segmentation``, ``vendor_id``.
        """
        from protoskipper.builtin_drivers.bacnet.client import discover_devices

        return discover_devices(
            self._session._loop_thread,
            self._session._app,
            low_limit=low,
            high_limit=high,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Property access
    # ------------------------------------------------------------------

    def read(
        self,
        object_id: str,
        prop: str = "presentValue",
    ) -> Any:
        """Read a single property from the remote device.

        Returns the decoded Python value.  Raises :exc:`DriverError` on error.
        """
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(
            device=self._session.device,
            object_id=object_id,
            data_type="any",
        )
        if prop == "presentValue":
            result = self._session.read(ref)
            return result.value

        # Generic read via RP
        return self._session._loop_thread.submit(
            self._session._async_read_property(object_id, prop),
            timeout=self._session._apdu_timeout + 2,
        )

    def read_many(
        self,
        requests: list[tuple[str, list[str]]],
    ) -> dict[str, dict[str, Any]]:
        """ReadPropertyMultiple over a list of ``(objid, [prop, ...])`` pairs.

        Returns a nested dict: ``{objid: {prop: value, ...}, ...}``.
        """
        return self._session._loop_thread.submit(
            self._session._async_rpm(requests),
            timeout=self._session._apdu_timeout + 4,
        )

    def write(
        self,
        object_id: str,
        value: Any,
        *,
        priority: int = 16,
        prop: str = "presentValue",
        data_type: str = "real",
    ) -> bool:
        """Write *value* to *prop* of *object_id* at *priority* (1-16).

        Returns ``True`` on success.  Raises on safety denial or protocol error.
        """
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(
            device=self._session.device,
            object_id=object_id,
            data_type=data_type,
        )
        intent = self._session.prepare_write(ref, value, priority=priority, data_type=data_type)
        intent.metadata["prop"] = prop
        result = self._session.commit_write(intent)
        return result.success

    def release(
        self,
        object_id: str,
        *,
        priority: int = 16,
        prop: str = "presentValue",
    ) -> bool:
        """Release (relinquish) a commandable property at *priority* by writing Null.

        Returns ``True`` on success.
        """
        from protoskipper.core.driver import ObjectRef, WriteIntent

        ref = ObjectRef(device=self._session.device, object_id=object_id, data_type="null")
        intent = WriteIntent(
            object_ref=ref,
            requested_value=None,
            encoded_bytes=b"",
            description=f"Release {object_id} {prop} priority {priority}",
            metadata={
                "bacnet_value": None,
                "priority": priority,
                "prop": prop,
            },
        )
        result = self._session.commit_write(intent)
        return result.success

    # ------------------------------------------------------------------
    # COV subscriptions
    # ------------------------------------------------------------------

    def subscribe_cov(
        self,
        object_id: str,
        callback: Callable[[Any], None] | None = None,
        *,
        lifetime_s: int = 300,
    ) -> Any:
        """Subscribe to COV notifications for *object_id*.

        Returns a :class:`SubscriptionHandle` that can be passed to
        :meth:`unsubscribe_cov`.
        """
        from protoskipper.core.driver import ObjectRef

        if callback is None:

            def callback(result: Any) -> None:
                _logger.info("COV %s = %s", object_id, result.value)

        ref = ObjectRef(device=self._session.device, object_id=object_id, data_type="any")
        return self._session.subscribe([ref], callback)

    def unsubscribe_cov(self, handle: Any) -> None:
        """Cancel a COV subscription."""
        self._session.unsubscribe(handle)

    # ------------------------------------------------------------------
    # Alarm & event
    # ------------------------------------------------------------------

    def get_event_information(self) -> list[dict[str, Any]]:
        """Return all active/unacknowledged alarms from the remote device."""
        return self._session.get_event_information()

    def acknowledge_alarm(
        self,
        object_id: str,
        event_state: str,
        *,
        source: str = "script",
    ) -> None:
        """Acknowledge an alarm on *object_id*."""
        self._session.acknowledge_alarm(object_id, event_state, source=source)

    def subscribe_events(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register *callback* for incoming EventNotification APDUs."""
        self._session.subscribe_events(callback)

    # ------------------------------------------------------------------
    # Trend logs
    # ------------------------------------------------------------------

    def read_trend_log(
        self,
        object_id: str,
        *,
        count: int = 100,
        range_type: str = "p",
        first: int = 1,
    ) -> list[Any]:
        """Read log records from a TrendLog or TrendLogMultiple object."""
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(device=self._session.device, object_id=object_id, data_type="any")
        return self._session.read_trend_log(ref, range_type=range_type, first=first, count=count)

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------

    def time_sync(self, dt: datetime | None = None, *, utc: bool = False) -> None:
        """Send TimeSynchronization (or UTCTimeSynchronization) to the device."""
        self._session.time_sync(dt, utc=utc)

    def reinitialize(self, state: str = "warmstart", *, password: str | None = None) -> None:
        """Send ReinitializeDevice.  Safety-gated."""
        self._session.reinitialize_device(state, password=password)

    def dcc(
        self,
        enable_disable: str,
        *,
        time_duration: int | None = None,
        password: str | None = None,
    ) -> None:
        """Send DeviceCommunicationControl.  Safety-gated."""
        self._session.device_communication_control(
            enable_disable, time_duration=time_duration, password=password
        )

    # ------------------------------------------------------------------
    # BBMD routing
    # ------------------------------------------------------------------

    def read_bdt(self, bbmd_addr: str) -> list[dict[str, Any]]:
        """Read Broadcast Distribution Table from a BBMD."""
        return self._session.read_bdt(bbmd_addr)

    def read_fdt(self, bbmd_addr: str) -> list[dict[str, Any]]:
        """Read Foreign Device Table from a BBMD."""
        return self._session.read_fdt(bbmd_addr)

    # ------------------------------------------------------------------
    # File services
    # ------------------------------------------------------------------

    def read_file(self, object_id: str, *, chunk_size: int = 1400) -> bytes:
        """Read the entire content of a BACnet File object."""
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(device=self._session.device, object_id=object_id, data_type="any")
        return self._session.read_file(ref, chunk_size=chunk_size)

    def write_file(self, object_id: str, data: bytes) -> bool:
        """Write *data* to a BACnet File object.  Safety-gated."""
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(device=self._session.device, object_id=object_id, data_type="any")
        result = self._session.write_file(ref, data)
        return result.success

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the session and release the background asyncio loop."""
        with __import__("contextlib").suppress(Exception):
            self._session.close()

    def __repr__(self) -> str:
        return f"BACnetScript(address={self._address!r})"
