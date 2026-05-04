# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet object-model helpers.

Keeps the rest of the driver free of raw bacpypes3 type strings by providing
Python-friendly enumerations and conversion helpers.

All bacpypes3 imports are kept inside functions so this module is importable
on a minimal install.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Object type catalogue (per ASHRAE 135-2020 cl. 12)
# ---------------------------------------------------------------------------


class BACnetObjectType(str, Enum):
    """String-form object type tags used by bacpypes3."""

    ANALOG_INPUT = "analog-input"
    ANALOG_OUTPUT = "analog-output"
    ANALOG_VALUE = "analog-value"
    BINARY_INPUT = "binary-input"
    BINARY_OUTPUT = "binary-output"
    BINARY_VALUE = "binary-value"
    BINARY_LIGHTING_OUTPUT = "binary-lighting-output"
    MULTI_STATE_INPUT = "multi-state-input"
    MULTI_STATE_OUTPUT = "multi-state-output"
    MULTI_STATE_VALUE = "multi-state-value"
    BITSTRING_VALUE = "bitstring-value"
    CHARACTERSTRING_VALUE = "characterstring-value"
    DATE_VALUE = "date-value"
    INTEGER_VALUE = "integer-value"
    LARGE_ANALOG_VALUE = "large-analog-value"
    OCTETSTRING_VALUE = "octetstring-value"
    POSITIVE_INTEGER_VALUE = "positive-integer-value"
    TIME_VALUE = "time-value"
    DATETIME_VALUE = "datetime-value"
    DATE_PATTERN_VALUE = "date-pattern-value"
    TIME_PATTERN_VALUE = "time-pattern-value"
    DATETIME_PATTERN_VALUE = "datetime-pattern-value"
    DEVICE = "device"
    PROGRAM = "program"
    COMMAND = "command"
    GROUP = "group"
    GLOBAL_GROUP = "global-group"
    LOOP = "loop"
    CHANNEL = "channel"
    STRUCTURED_VIEW = "structured-view"
    NOTIFICATION_CLASS = "notification-class"
    NOTIFICATION_FORWARDER = "notification-forwarder"
    EVENT_ENROLLMENT = "event-enrollment"
    EVENT_LOG = "event-log"
    TREND_LOG = "trend-log"
    TREND_LOG_MULTIPLE = "trend-log-multiple"
    ACCUMULATOR = "accumulator"
    PULSE_CONVERTER = "pulse-converter"
    AVERAGING = "averaging"
    TIMER = "timer"
    LIFE_SAFETY_POINT = "life-safety-point"
    LIFE_SAFETY_ZONE = "life-safety-zone"
    LIGHTING_OUTPUT = "lighting-output"
    SCHEDULE = "schedule"
    CALENDAR = "calendar"
    FILE = "file"
    ACCESS_DOOR = "access-door"
    ACCESS_CREDENTIAL = "access-credential"
    ACCESS_POINT = "access-point"
    ACCESS_RIGHTS = "access-rights"
    ACCESS_USER = "access-user"
    ACCESS_ZONE = "access-zone"
    ALERT_ENROLLMENT = "alert-enrollment"
    CREDENTIAL_DATA_INPUT = "credential-data-input"
    ELEVATOR_GROUP = "elevator-group"
    ESCALATOR = "escalator"
    LIFT = "lift"
    NETWORK_PORT = "network-port"
    NETWORK_SECURITY = "network-security"
    # Vendor-proprietary (≥ 128) rendered as their numeric type id string
    # by bacpypes3 when no local definition exists.


# Commandable object types — have a Priority_Array property.
COMMANDABLE_TYPES: frozenset[str] = frozenset(
    {
        BACnetObjectType.ANALOG_OUTPUT,
        BACnetObjectType.ANALOG_VALUE,
        BACnetObjectType.BINARY_OUTPUT,
        BACnetObjectType.BINARY_VALUE,
        BACnetObjectType.BINARY_LIGHTING_OUTPUT,
        BACnetObjectType.MULTI_STATE_OUTPUT,
        BACnetObjectType.MULTI_STATE_VALUE,
        BACnetObjectType.LIGHTING_OUTPUT,
        BACnetObjectType.CHANNEL,
        BACnetObjectType.LARGE_ANALOG_VALUE,
    }
)

# Standard property identifiers we always fetch in RPM when enumerating.
STANDARD_PROPERTIES: tuple[str, ...] = (
    "objectIdentifier",
    "objectName",
    "objectType",
    "description",
    "presentValue",
    "statusFlags",
    "reliability",
    "outOfService",
    "units",
    "stateText",
    "relinquishDefault",
    "covIncrement",
)

# Properties requested for a Device object on first connection.
DEVICE_PROPERTIES: tuple[str, ...] = (
    "objectIdentifier",
    "objectName",
    "vendorIdentifier",
    "vendorName",
    "modelName",
    "firmwareRevision",
    "applicationSoftwareVersion",
    "description",
    "location",
    "systemStatus",
    "maxApduLengthAccepted",
    "segmentationSupported",
    "protocolVersion",
    "protocolRevision",
    "databaseRevision",
    "numberOfApduRetries",
    "apduTimeout",
    "profileName",
)


def object_id_str(obj_type: str, instance: int) -> str:
    """Return a canonical ``"type:instance"`` string, e.g. ``"analog-value:1"``."""
    return f"{obj_type}:{instance}"


def parse_object_id(objid: str) -> tuple[str, int]:
    """Parse ``"type:instance"`` → ``(type_str, instance_int)``."""
    parts = objid.split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected 'type:instance', got {objid!r}")
    return parts[0].strip(), int(parts[1].strip())


def bacnet_value_to_python(value: Any) -> Any:
    """Convert a bacpypes3 property value to a plain Python scalar.

    Returns numbers as int/float, booleans as bool, enumerations as their
    string label, and everything else as its str() representation.  Never
    raises — unknown types fall back to str().
    """
    # Import here to stay lazy.
    try:
        from bacpypes3.primitivedata import (
            Boolean,
            CharacterString,
            Double,
            Enumerated,
            Integer,
            Null,
            ObjectIdentifier,
            Real,
            Unsigned,
        )

        if isinstance(value, Null):
            return None
        if isinstance(value, Boolean):
            return bool(value)
        if isinstance(value, (Integer, Unsigned)):
            return int(value)
        if isinstance(value, (Real, Double)):
            return float(value)
        if isinstance(value, CharacterString):
            return str(value)
        if isinstance(value, ObjectIdentifier):
            # Return as "type:instance" string
            return object_id_str(str(value[0]), int(value[1]))
        if isinstance(value, Enumerated):
            # bacpypes3 enumerations stringify to their label
            return str(value)
    except ImportError:
        pass
    return str(value) if value is not None else None


def python_to_bacnet_value(type_hint: str, value: Any) -> Any:
    """Convert a Python scalar to the appropriate bacpypes3 primitive.

    ``type_hint`` is a lower-case data-type string from the points list
    (``"real"``, ``"boolean"``, ``"unsigned"``, ``"integer"``,
    ``"characterstring"``, ``"enumerated"``).
    """
    try:
        from bacpypes3.primitivedata import (
            Boolean,
            CharacterString,
            Double,
            Integer,
            Null,
            Real,
            Unsigned,
        )

        ht = type_hint.lower().replace("-", "").replace("_", "")
        if value is None:
            return Null(())
        if ht in ("real", "float32", "float"):
            return Real(float(value))
        if ht in ("double", "float64"):
            return Double(float(value))
        if ht in ("unsigned", "uint", "uint32"):
            return Unsigned(int(value))
        if ht in ("integer", "int", "int32"):
            return Integer(int(value))
        if ht in ("boolean", "bool"):
            return Boolean(bool(value))
        if ht in ("characterstring", "str", "string"):
            return CharacterString(str(value))
        # Fallback: try Real
        return Real(float(value))
    except ImportError:
        return value
