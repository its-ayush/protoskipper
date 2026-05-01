# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""High-level session lifecycle: connect a driver, wire up safety + audit, close cleanly.

This is the entry point the GUI and CLI reach for when they want to *do*
something with a device. It glues together:

* the protocol driver (chosen from :func:`protoskipper.core.plugin_loader.load_protocol_drivers`),
* a :class:`SafetyContext` configured for the requested profile,
* an :class:`AuditLog` that captures the session,
* and the :class:`DriverSession` returned by the driver.

A :class:`Session` instance owns these resources and releases them on close
(or on context-manager exit). Drivers should never be invoked outside this
wrapper in production code paths.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from protoskipper.core.audit import AuditLog
from protoskipper.core.driver import (
    DeviceRef,
    SafetyContext,
    SessionProfile,
    WriteIntent,
)

if TYPE_CHECKING:
    from protoskipper.core.driver import DriverSession, ProtocolDriver

_logger = logging.getLogger(__name__)


ConfirmFn = Callable[[WriteIntent, SessionProfile], bool]


def default_confirm(intent: WriteIntent, profile: SessionProfile) -> bool:
    """Headless default: deny every write.

    The GUI replaces this with a real confirmation dialog. The CLI replaces
    it with a typed-tag prompt. The default is *deny*, on purpose — a script
    that forgot to wire up confirmation cannot accidentally write to a
    device.
    """
    _logger.warning(
        "Write intent rejected by default_confirm (profile=%s, target=%s). "
        "Wire up a real confirm callback before running interactive sessions.",
        profile.value,
        intent.object_ref.object_id,
    )
    return False


@dataclass
class Session:
    """Live, audited connection to one device.

    Construct via :func:`open_session` rather than directly so the audit log
    and safety context are wired correctly.
    """

    driver: ProtocolDriver
    device: DeviceRef
    profile: SessionProfile
    operator: str
    audit: AuditLog
    safety: SafetyContext
    driver_session: DriverSession

    def close(self) -> None:
        try:
            self.driver_session.close()
        finally:
            self.audit.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def open_session(
    driver: ProtocolDriver,
    device: DeviceRef,
    *,
    profile: SessionProfile,
    operator: str,
    audit_dir: Path,
    confirm: ConfirmFn = default_confirm,
) -> Session:
    """Open an audited session against ``device`` using ``driver``.

    A new audit-log file is created in ``audit_dir`` named after the session
    timestamp. The caller is responsible for managing retention; ProtoSkipper
    never deletes audit logs on its own.
    """
    audit_path = audit_dir / _audit_filename(device, operator)
    audit_log = AuditLog.create(audit_path, operator=operator, profile=profile.value)

    safety = SafetyContext(
        profile=profile,
        confirm_callback=confirm,
        audit_callback=lambda **fields: audit_log.record(**fields),
    )

    audit_log.record(
        event="connect_attempt",
        protocol=device.protocol,
        address=device.address,
    )
    try:
        driver_session = driver.connect(device, safety)
    except Exception as exc:
        audit_log.record(event="connect_failed", error=repr(exc))
        audit_log.close()
        raise

    audit_log.record(event="connected", protocol=device.protocol, address=device.address)

    return Session(
        driver=driver,
        device=device,
        profile=profile,
        operator=operator,
        audit=audit_log,
        safety=safety,
        driver_session=driver_session,
    )


def _audit_filename(device: DeviceRef, operator: str) -> str:
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_op = "".join(c if c.isalnum() else "_" for c in operator)[:32]
    safe_proto = device.protocol.replace(".", "_")
    return f"{ts}__{safe_op}__{safe_proto}.audit.sqlite"
