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
from typing import TYPE_CHECKING, Any

from protoskipper.core.audit import AuditLog
from protoskipper.core.driver import (
    ConfirmCallback,
    DeviceRef,
    ObjectRef,
    ReadResult,
    SafetyContext,
    SessionProfile,
    WriteIntent,
)

if TYPE_CHECKING:
    from protoskipper.core.driver import DriverSession, ProtocolDriver

_logger = logging.getLogger(__name__)


ConfirmFn = ConfirmCallback


class _AuditingDriverSession:
    """Thin proxy around a DriverSession that records each read in the audit log.

    Only instantiated when ``audit_reads=True`` is passed to
    :func:`open_session`.  The proxy forwards every attribute access to the
    underlying session, intercepting only :meth:`read` and :meth:`read_many`
    to insert audit records.

    Design note: using ``__getattr__`` for delegation keeps the proxy
    future-proof — new methods added to ``DriverSession`` are forwarded
    automatically without touching this class.
    """

    def __init__(self, inner: DriverSession, audit_callback: Callable[..., None]) -> None:
        # Store in __dict__ directly to bypass __getattr__.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_audit", audit_callback)

    def _inner_session(self) -> DriverSession:
        return object.__getattribute__(self, "_inner")  # type: ignore[no-any-return]

    def _audit_fn(self) -> Callable[..., None]:
        return object.__getattribute__(self, "_audit")  # type: ignore[no-any-return]

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_inner"), name)

    def read(self, ref: ObjectRef) -> ReadResult:
        result: ReadResult = self._inner_session().read(ref)
        self._audit_fn()(
            event="read_completed",
            object_id=ref.object_id,
            quality=result.quality.value,
            value=repr(result.value),
        )
        return result

    def read_many(self, refs: list[ObjectRef]) -> list[ReadResult]:
        results: list[ReadResult] = self._inner_session().read_many(refs)
        audit = self._audit_fn()
        for result in results:
            audit(
                event="read_completed",
                object_id=result.object_ref.object_id,
                quality=result.quality.value,
                value=repr(result.value),
            )
        return results


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
    on_audit_record: Callable[[], None] | None = None,
    audit_reads: bool = False,
) -> Session:
    """Open an audited session against ``device`` using ``driver``.

    A new audit-log file is created in ``audit_dir`` named after the session
    timestamp. The caller is responsible for managing retention; ProtoSkipper
    never deletes audit logs on its own.

    ``on_audit_record`` is an optional zero-argument callback invoked
    *after* each row is appended to the audit log.  The GUI uses this to
    keep a live row-count display without polling the SQLite file.

    ``audit_reads`` (default ``False``) controls whether individual read
    operations are recorded in the audit chain.  Reads are excluded by
    default to avoid log bloat on watchlist-heavy sessions.  Set to
    ``True`` for forensic sessions where a complete input record is needed.
    """
    audit_path = audit_dir / _audit_filename(device, operator)
    audit_log = AuditLog.create(audit_path, operator=operator, profile=profile.value)

    def _audit_record(**fields: Any) -> None:
        audit_log.record(**fields)
        if on_audit_record is not None:
            on_audit_record()

    safety = SafetyContext(
        profile=profile,
        confirm_callback=confirm,
        audit_callback=_audit_record,
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

    # When audit_reads is enabled, wrap the driver session so every read is
    # recorded in the audit chain.
    active_driver_session: DriverSession = driver_session
    if audit_reads:
        active_driver_session = _AuditingDriverSession(  # type: ignore[assignment]
            driver_session, _audit_record
        )

    return Session(
        driver=driver,
        device=device,
        profile=profile,
        operator=operator,
        audit=audit_log,
        safety=safety,
        driver_session=active_driver_session,
    )


def _audit_filename(device: DeviceRef, operator: str) -> str:
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_op = "".join(c if c.isalnum() else "_" for c in operator)[:32]
    safe_proto = device.protocol.replace(".", "_")
    return f"{ts}__{safe_op}__{safe_proto}.audit.sqlite"
