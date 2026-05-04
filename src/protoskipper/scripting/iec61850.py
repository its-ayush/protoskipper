# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 61850 scripting namespace for ProtoSkipper (P8.I.4).

This module is the *sole* place where the scripting layer touches
``protoskipper_iec61850``.  Everything is lazy-imported so the core package
remains importable even when the IEC 61850 plugin is not installed.

Exposed top-level names (``iec61850.<name>``):

``Session``
    Thin synchronous wrapper around :class:`MmsClient`.  Opens an MMS
    association, drives reads/writes, closes cleanly.

``Goose``
    Namespace of GOOSE helpers: ``Goose.Subscriber``, ``Goose.Publisher``.

``Sv``
    Namespace of SV helpers: ``Sv.Subscriber``, ``Sv.decode_frame``,
    ``Sv.encode_frame``.

``Scd``
    Namespace of SCL/SCD helpers: ``Scd.parse``, ``Scd.diff``,
    ``Scd.SclDocument``.

``dissect``
    The :mod:`protoskipper_iec61850.dissect` sub-package, re-exported as-is.

``conformance``
    The :mod:`protoskipper_iec61850.conformance` sub-package, re-exported.

``simulator``
    The :mod:`protoskipper_iec61850.simulator` module, re-exported.

Usage in a script
-----------------
::

    session = iec61850.Session("192.168.1.10")
    session.connect()
    val = session.read("IED1LD0/MMXU1.TotW.mag.f", fc="MX")
    print(val)
    session.close()

    sub = iec61850.Goose.Subscriber(interface="eth0")
    sub.subscribe("IED1/GoEna", on_message=print)
    sub.start()
"""

from __future__ import annotations

import importlib
import types
from typing import Any

# ---------------------------------------------------------------------------
# Session — thin synchronous MmsClient wrapper
# ---------------------------------------------------------------------------


class Session:
    """Synchronous IEC 61850 MMS session for scripting.

    Parameters
    ----------
    host:
        IED hostname or IP address.
    port:
        MMS/TCP port (default 102).
    connect_timeout_ms:
        Connect timeout in milliseconds (default 10 000).
    allow_writes:
        When *False* (the default), :meth:`write` raises :exc:`PermissionError`.
    """

    def __init__(
        self,
        host: str,
        port: int = 102,
        connect_timeout_ms: int = 10_000,
        *,
        allow_writes: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._connect_timeout_ms = connect_timeout_ms
        self._allow_writes = allow_writes
        self._client: Any = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the MMS association."""
        try:
            from protoskipper_iec61850._mms_client import MmsClient
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "protoskipper-iec61850 plugin is not installed. "
                "Install it with: pip install protoskipper-iec61850"
            ) from exc

        self._client = MmsClient(
            host=self._host,
            port=self._port,
            connect_timeout_ms=self._connect_timeout_ms,
        )
        self._client.connect()

    def close(self) -> None:
        """Close the MMS association gracefully."""
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def __enter__(self) -> Session:
        self.connect()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("Session is not connected. Call connect() first.")
        return self._client

    def read(self, reference: str, *, fc: str = "MX") -> Any:
        """Read a single data attribute.

        Parameters
        ----------
        reference:
            IEC 61850 object reference, e.g. ``"IED1LD0/MMXU1.TotW.mag.f"``.
        fc:
            Functional constraint (e.g. ``"MX"``, ``"ST"``, ``"CF"``).

        Returns
        -------
        The decoded value (float, bool, str, list, or dict).
        """
        return self._require_client().read(reference, fc=fc)

    def read_all(self, logical_device: str, logical_node: str, fc: str = "MX") -> Any:
        """Read all data attributes of *logical_node* filtered by *fc*.

        Returns a ``{object_ref: value}`` mapping.
        """
        client = self._require_client()
        return client.read_all(logical_device, logical_node, fc=fc)

    def write(self, reference: str, value: Any, *, fc: str = "SP") -> None:
        """Write a data attribute.

        Raises :exc:`PermissionError` if the session was created with
        ``allow_writes=False``.
        """
        if not self._allow_writes:
            raise PermissionError(
                "Writes are disabled on this session. "
                "Create the session with allow_writes=True to enable writes."
            )
        self._require_client().write(reference, value, fc=fc)

    def get_server_directory(self) -> Any:
        """Return the list of logical-device names exposed by the server."""
        return self._require_client().get_server_directory()

    def get_logical_device_directory(self, logical_device: str) -> Any:
        """Return the logical-node names within *logical_device*."""
        return self._require_client().get_logical_device_directory(logical_device)

    def get_data_directory(self, logical_device: str, logical_node: str) -> Any:
        """Return data-object names within *logical_node*."""
        return self._require_client().get_data_directory(logical_device, logical_node)

    def get_data_definition(self, reference: str, *, fc: str = "MX") -> Any:
        """Return the variable-access-attributes of *reference*."""
        return self._require_client().get_variable_access_attributes(reference, fc=fc)

    def enable_reporting(
        self,
        rcb_ref: str,
        report_id: str,
        *,
        on_report: Any = None,
    ) -> None:
        """Enable an unbuffered report control block."""
        client = self._require_client()
        client.enable_unbuffered_reporting(rcb_ref, report_id, on_report=on_report)

    def disable_reporting(self, rcb_ref: str) -> None:
        """Disable an unbuffered report control block."""
        self._require_client().disable_reporting(rcb_ref)


# ---------------------------------------------------------------------------
# Goose namespace
# ---------------------------------------------------------------------------


class _LazyGoose:
    """Lazy-loading proxy for GOOSE classes."""

    @property
    def Subscriber(self) -> Any:
        from protoskipper_iec61850.goose.subscriber import (
            GooseSubscriberService,
        )

        return GooseSubscriberService

    @property
    def Publisher(self) -> Any:
        from protoskipper_iec61850.goose.publisher import (
            GoosePublisherService,
        )

        return GoosePublisherService


def _build_goose_ns() -> _LazyGoose:
    """Return the ``iec61850.Goose`` sub-namespace (lazy import)."""
    return _LazyGoose()


# ---------------------------------------------------------------------------
# Sv namespace
# ---------------------------------------------------------------------------


class _LazySv:
    """Lazy-loading proxy for SV classes."""

    @property
    def Subscriber(self) -> Any:
        from protoskipper_iec61850.sv.subscriber import (
            SvSubscriberService,
        )

        return SvSubscriberService

    def decode_frame(self, frame: bytes) -> Any:
        """Decode a raw SV Ethernet frame."""
        from protoskipper_iec61850.sv.decoder import decode_frame

        return decode_frame(frame)

    def encode_frame(self, *args: Any, **kwargs: Any) -> bytes:
        """Encode a SV Ethernet frame."""
        from protoskipper_iec61850.sv.encoder import encode_frame

        return encode_frame(*args, **kwargs)


def _build_sv_ns() -> _LazySv:
    """Return the ``iec61850.Sv`` sub-namespace (lazy import)."""
    return _LazySv()


# ---------------------------------------------------------------------------
# Scd namespace
# ---------------------------------------------------------------------------


class _LazyScd:
    """Lazy-loading proxy for SCL/SCD helpers."""

    def parse(self, path: str) -> Any:
        """Parse an SCD/IID/CID file and return an :class:`SclDocument`."""
        from pathlib import Path as _Path

        from protoskipper_iec61850.scl.parser import parse

        return parse(_Path(path))

    def diff(self, old: Any, new: Any) -> Any:
        """Return a :class:`SclDiff` object between *old* and *new*."""
        from protoskipper_iec61850.scl.diff import diff

        return diff(old, new)

    @property
    def SclDocument(self) -> Any:
        from protoskipper_iec61850.scl.model import SclDocument

        return SclDocument


def _build_scd_ns() -> _LazyScd:
    """Return the ``iec61850.Scd`` sub-namespace (lazy import)."""
    return _LazyScd()


# ---------------------------------------------------------------------------
# Lazy module proxy
# ---------------------------------------------------------------------------


class _LazyModule:
    """Lazy accessor for a top-level module by dotted name."""

    def __init__(self, dotted: str) -> None:
        self._dotted = dotted
        self._mod: Any = None

    def _load(self) -> Any:
        if self._mod is None:
            self._mod = importlib.import_module(self._dotted)
        return self._mod

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


# ---------------------------------------------------------------------------
# Public factory — called from scripting/__init__.py
# ---------------------------------------------------------------------------


def make_iec61850_ns(allow_writes: bool = False) -> types.SimpleNamespace:
    """Build and return the ``iec61850`` scripting namespace.

    Returns a :class:`types.SimpleNamespace` containing ``Session``,
    ``Goose``, ``Sv``, ``Scd``, ``dissect``, ``conformance``, and
    ``simulator`` attributes.  All protocol-specific imports are deferred so
    this function is safe to call even when ``protoskipper_iec61850`` is not
    installed — the attributes simply resolve at access time.

    Parameters
    ----------
    allow_writes:
        Forwarded to :class:`Session` as its default for ``allow_writes``.
    """

    # Session is exposed directly; callers pass allow_writes manually.
    # The _allow_writes default is baked in via a subclass so scripts that
    # do not pass allow_writes=True cannot accidentally enable writes.
    class _SafeSession(Session):
        def __init__(
            self, host: str, port: int = 102, connect_timeout_ms: int = 10_000, **kwargs: Any
        ) -> None:
            kwargs.setdefault("allow_writes", allow_writes)
            super().__init__(host, port, connect_timeout_ms, **kwargs)

    return types.SimpleNamespace(
        Session=_SafeSession,
        Goose=_build_goose_ns(),
        Sv=_build_sv_ns(),
        Scd=_build_scd_ns(),
        dissect=_LazyModule("protoskipper_iec61850.dissect"),
        conformance=_LazyModule("protoskipper_iec61850.conformance"),
        simulator=_LazyModule("protoskipper_iec61850.simulator"),
    )
