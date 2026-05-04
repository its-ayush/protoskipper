# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet audit-log row schema and structured payload builders.

Every BACnet operation recorded in an :class:`~protoskipper.core.audit.AuditLog`
should use the helpers in this module so audit rows are consistent, searchable,
and forward-compatible across protocol versions.

Design rules
------------
* All payload dicts carry ``schema_version`` so a future reader can detect
  schema drift.
* Field names are lower-case with underscores.  No camelCase.
* BACnet object identifiers are always in ``"TYPE:INSTANCE"`` format (e.g.
  ``"analog-value:1"``).
* Timestamps are ISO-8601 UTC strings when included.
* No secrets (passwords) are ever included in payloads.

Usage::

    from protoskipper.builtin_drivers.bacnet.audit_schema import BacnetEvent, write_payload
    audit.record(event=BacnetEvent.WRITE, **write_payload(
        device_id=1234,
        object_id="analog-value:1",
        prop="present-value",
        value=21.5,
        priority=8,
    ))
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Schema version — bump when payload shape changes incompatibly
# ---------------------------------------------------------------------------

AUDIT_SCHEMA_VERSION: int = 1

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------


class BacnetEvent:
    """String constants for BACnet-specific ``event`` column values.

    Pass these as the ``event=`` keyword argument to
    :meth:`~protoskipper.core.audit.AuditLog.record`.
    """

    # Session lifecycle
    CONNECT = "bacnet.connect"
    DISCONNECT = "bacnet.disconnect"

    # Object access — reads
    READ = "bacnet.read"
    READ_MANY = "bacnet.read_many"
    READ_RANGE = "bacnet.read_range"

    # Object access — writes
    WRITE = "bacnet.write"
    WRITE_MANY = "bacnet.write_many"
    WRITE_FILE = "bacnet.write_file"

    # Discovery
    WHO_IS = "bacnet.who_is"
    I_AM = "bacnet.i_am"

    # COV (Change-of-Value)
    COV_SUBSCRIBE = "bacnet.cov_subscribe"
    COV_UNSUBSCRIBE = "bacnet.cov_unsubscribe"
    COV_NOTIFICATION = "bacnet.cov_notification"

    # Alarm & event services
    ALARM_RECEIVED = "bacnet.alarm_received"
    ALARM_ACKNOWLEDGED = "bacnet.alarm_acknowledge"
    GET_EVENT_INFO = "bacnet.get_event_information"

    # Trend & log retrieval
    READ_TREND_LOG = "bacnet.read_trend_log"
    READ_EVENT_LOG = "bacnet.read_event_log"

    # Schedule & Calendar
    READ_SCHEDULE = "bacnet.read_schedule"
    WRITE_SCHEDULE = "bacnet.write_schedule"

    # Device management services
    TIME_SYNC = "bacnet.time_sync"
    REINITIALIZE = "bacnet.reinitialize_device"
    DCC = "bacnet.device_communication_control"

    # BBMD / FD routing
    READ_BDT = "bacnet.read_bdt"
    READ_FDT = "bacnet.read_fdt"
    REGISTER_FD = "bacnet.register_foreign_device"

    # Protocol errors
    PROTOCOL_ERROR = "bacnet.error"


# ---------------------------------------------------------------------------
# Payload builder functions
# ---------------------------------------------------------------------------


def connect_payload(
    device_id: int | None,
    address: str,
    *,
    vendor_id: int | None = None,
    max_apdu: int | None = None,
    segmentation: str | None = None,
    vendor_profile: str = "generic",
) -> dict[str, Any]:
    """Payload for :attr:`BacnetEvent.CONNECT`."""
    p: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "device_id": device_id,
        "address": address,
        "vendor_profile": vendor_profile,
    }
    if vendor_id is not None:
        p["vendor_id"] = vendor_id
    if max_apdu is not None:
        p["max_apdu"] = max_apdu
    if segmentation is not None:
        p["segmentation"] = segmentation
    return p


def read_payload(
    device_id: int | None,
    object_id: str,
    prop: str,
    *,
    value: Any = None,
    quality: str = "good",
    rtt_ms: float | None = None,
) -> dict[str, Any]:
    """Payload for :attr:`BacnetEvent.READ`."""
    p: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "device_id": device_id,
        "object_id": object_id,
        "property": prop,
        "quality": quality,
    }
    if value is not None:
        p["value"] = value
    if rtt_ms is not None:
        p["rtt_ms"] = round(rtt_ms, 3)
    return p


def write_payload(
    device_id: int | None,
    object_id: str,
    prop: str,
    value: Any,
    priority: int | None,
    *,
    service: str = "WriteProperty",
    success: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    """Payload for :attr:`BacnetEvent.WRITE` and :attr:`BacnetEvent.WRITE_MANY`."""
    p: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "service": service,
        "device_id": device_id,
        "object_id": object_id,
        "property": prop,
        "value": value,
        "priority": priority,
        "success": success,
    }
    if error:
        p["error"] = error
    return p


def read_range_payload(
    device_id: int | None,
    object_id: str,
    prop: str,
    range_type: str,
    count_requested: int,
    count_received: int,
) -> dict[str, Any]:
    """Payload for :attr:`BacnetEvent.READ_RANGE` and :attr:`BacnetEvent.READ_TREND_LOG`."""
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "device_id": device_id,
        "object_id": object_id,
        "property": prop,
        "range_type": range_type,
        "count_requested": count_requested,
        "count_received": count_received,
    }


def alarm_payload(
    device_id: int | None,
    object_id: str,
    event_state: str,
    notify_type: str = "alarm",
    *,
    notification_class: int | None = None,
    priority: int | None = None,
    message_text: str | None = None,
) -> dict[str, Any]:
    """Payload for :attr:`BacnetEvent.ALARM_RECEIVED`."""
    p: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "device_id": device_id,
        "object_id": object_id,
        "event_state": event_state,
        "notify_type": notify_type,
    }
    if notification_class is not None:
        p["notification_class"] = notification_class
    if priority is not None:
        p["priority"] = priority
    if message_text is not None:
        p["message_text"] = message_text
    return p


def alarm_ack_payload(
    device_id: int | None,
    object_id: str,
    event_state: str,
    source: str,
    process_id: int,
) -> dict[str, Any]:
    """Payload for :attr:`BacnetEvent.ALARM_ACKNOWLEDGED`."""
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "device_id": device_id,
        "object_id": object_id,
        "event_state": event_state,
        "acknowledgment_source": source,
        "process_id": process_id,
    }


def discovery_payload(
    device_id: int,
    address: str,
    *,
    vendor_id: int | None = None,
    max_apdu: int | None = None,
    low_limit: int | None = None,
    high_limit: int | None = None,
) -> dict[str, Any]:
    """Payload for :attr:`BacnetEvent.I_AM` (discovered device)."""
    p: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "device_id": device_id,
        "address": address,
    }
    if vendor_id is not None:
        p["vendor_id"] = vendor_id
    if max_apdu is not None:
        p["max_apdu"] = max_apdu
    if low_limit is not None or high_limit is not None:
        p["who_is_range"] = [low_limit, high_limit]
    return p


def cov_subscribe_payload(
    device_id: int | None,
    object_id: str,
    process_id: int,
    lifetime_s: int,
    *,
    confirmed: bool = True,
    prop: str | None = None,
    cov_increment: float | None = None,
) -> dict[str, Any]:
    """Payload for :attr:`BacnetEvent.COV_SUBSCRIBE` / :attr:`BacnetEvent.COV_UNSUBSCRIBE`."""
    p: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "device_id": device_id,
        "object_id": object_id,
        "process_id": process_id,
        "lifetime_s": lifetime_s,
        "confirmed": confirmed,
    }
    if prop is not None:
        p["monitored_property"] = prop
    if cov_increment is not None:
        p["cov_increment"] = cov_increment
    return p


def device_mgmt_payload(
    device_id: int | None,
    service: str,
    **params: Any,
) -> dict[str, Any]:
    """Payload for device management events (DCC, reinit, time-sync, etc.).

    Extra keyword arguments are merged into the dict (e.g.
    ``state="warmstart"`` for reinitialize, ``enable_disable="disable"`` for
    DCC, ``utc=True`` for time synchronization).  Do not include passwords.
    """
    p: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "device_id": device_id,
        "service": service,
    }
    p.update(params)
    return p


def routing_payload(
    bbmd_address: str,
    service: str,
    *,
    entries: list[dict[str, Any]] | None = None,
    entry_count: int | None = None,
    ttl_s: int | None = None,
) -> dict[str, Any]:
    """Payload for BBMD / FD routing events (Read-BDT, Read-FDT, Register-FD)."""
    p: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "bbmd_address": bbmd_address,
        "service": service,
    }
    if entries is not None:
        p["entries"] = entries
    if entry_count is not None:
        p["entry_count"] = entry_count
    if ttl_s is not None:
        p["ttl_s"] = ttl_s
    return p


def error_payload(
    device_id: int | None,
    service: str,
    error_message: str,
    *,
    object_id: str | None = None,
) -> dict[str, Any]:
    """Payload for :attr:`BacnetEvent.PROTOCOL_ERROR`."""
    p: dict[str, Any] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "device_id": device_id,
        "service": service,
        "error": error_message,
    }
    if object_id is not None:
        p["object_id"] = object_id
    return p
