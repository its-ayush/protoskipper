# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Core ProtoSkipper machinery: plugin contract, session lifecycle, audit log.

The :mod:`protoskipper.core` package is import-safe without GUI or protocol
extras installed, so headless tools, CI, and lightweight installations only
pull in the abstractions they need.
"""

from protoskipper.core.driver import (
    Access,
    Capturer,
    CaptureSink,
    DeviceRef,
    DriverSession,
    ObjectRef,
    ProtocolDriver,
    Quality,
    ReadResult,
    SafetyContext,
    SessionProfile,
    Simulator,
    Subscriber,
    WriteIntent,
    WriteResult,
)
from protoskipper.core.errors import (
    AuthorizationDenied,
    ConnectionFailure,
    DriverError,
    EncodingError,
    ProtoSkipperError,
    UnsupportedOperation,
)

__all__ = [
    "Access",
    "AuthorizationDenied",
    "CaptureSink",
    "Capturer",
    "ConnectionFailure",
    "DeviceRef",
    "DriverError",
    "DriverSession",
    "EncodingError",
    "ObjectRef",
    "ProtoSkipperError",
    "ProtocolDriver",
    "Quality",
    "ReadResult",
    "SafetyContext",
    "SessionProfile",
    "Simulator",
    "Subscriber",
    "UnsupportedOperation",
    "WriteIntent",
    "WriteResult",
]
