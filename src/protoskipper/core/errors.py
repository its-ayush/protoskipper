# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Exception hierarchy used by every ProtoSkipper component.

All ProtoSkipper-originated errors derive from :class:`ProtoSkipperError`, so
the GUI and CLI can catch one type and still distinguish causes via the
specific subclasses.
"""
from __future__ import annotations


class ProtoSkipperError(Exception):
    """Base class for every error raised by ProtoSkipper itself."""


class DriverError(ProtoSkipperError):
    """Raised by a protocol driver when an operation cannot be completed."""


class ConnectionFailure(DriverError):
    """A driver could not establish or maintain a session to a device."""


class EncodingError(DriverError):
    """A value could not be encoded for the wire (or decoded from it)."""


class UnsupportedOperation(DriverError):
    """The driver does not implement the requested capability.

    Drivers should raise this from optional capability methods (subscribe,
    simulate, capture) rather than failing the abstract class instantiation,
    so that a Modbus-only driver does not fail to load just because it cannot
    simulate a slave.
    """


class AuthorizationDenied(ProtoSkipperError):
    """A write or control operation was denied by the active SafetyContext.

    Distinct from DriverError on purpose: this is a *policy* failure, not a
    protocol failure. The bytes never went on the wire.
    """
