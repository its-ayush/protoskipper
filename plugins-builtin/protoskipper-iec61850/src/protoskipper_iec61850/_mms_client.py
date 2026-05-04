# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Thin adapter over the pyiec61850 SWIG binding (libiec61850).

Design notes
------------
``pyiec61850`` is **not** installable via pip.  It is built from the
libiec61850 source tree using cmake + SWIG and installed system-wide (or
into a virtual environment).  See ``docs/internal/IEC61850_MMS_LIBRARY.md``
for the decision record and build instructions.

This module isolates all SWIG API calls behind the :class:`MmsClient`
interface.  The rest of the plugin **never** imports pyiec61850 directly —
it only sees :class:`MmsClient`, :class:`MmsConnectError`, and plain Python
types.  This makes it straightforward to swap the implementation to a
pure-Python MMS codec (Option C) without touching any other file.

Public API
----------
* :class:`MmsConnectError` — raised by :meth:`MmsClient.connect` when the
  MMS Initiate exchange fails.
* :class:`MmsClient` — context-manager-compatible client for one MMS
  connection to a single IED.

Thread safety
-------------
A :class:`MmsClient` instance must be used from a single thread only.
``IedConnection_*`` calls from libiec61850 are not thread-safe.
"""

from __future__ import annotations

import logging
from typing import Any

from protoskipper.core.errors import ConnectionFailure

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IedClientError code → human-readable name
# (values from libiec61850 ied_client_api.h)
# ---------------------------------------------------------------------------
_IED_ERROR_NAMES: dict[int, str] = {
    0: "IED_ERROR_OK",
    1: "IED_ERROR_NOT_CONNECTED",
    2: "IED_ERROR_ALREADY_CONNECTED",
    3: "IED_ERROR_CONNECTION_LOST",
    4: "IED_ERROR_SERVICE_NOT_SUPPORTED",
    5: "IED_ERROR_DEFINITION_CONFLICT",
    6: "IED_ERROR_INVALID_ADDRESS",
    7: "IED_ERROR_HARDWARE_FAULT",
    8: "IED_ERROR_TYPE_INCONSISTENT",
    9: "IED_ERROR_OBJECT_ACCESS_UNSUPPORTED",
    10: "IED_ERROR_TEMPORARILY_UNAVAILABLE",
    11: "IED_ERROR_OBJECT_ACCESS_DENIED",
    12: "IED_ERROR_OBJECT_UNDEFINED",
    13: "IED_ERROR_INVALID_RESPONSE",
    14: "IED_ERROR_TIMEOUT",
    15: "IED_ERROR_ACCESS_VIOLATION",
    16: "IED_ERROR_OBJECT_VALUE_INVALID",
    17: "IED_ERROR_OBJECT_NOT_EXIST",
    18: "IED_ERROR_OBJECT_ALREADY_EXISTS",
    98: "IED_ERROR_OTHER",
    100: "IED_ERROR_USER_PROVIDED_INVALID_ARGUMENT",
    32768: "IED_ERROR_CONNECTION_REJECTED",
}


def _ied_error_name(code: int) -> str:
    return _IED_ERROR_NAMES.get(code, f"IED_ERROR_UNKNOWN({code})")


# ---------------------------------------------------------------------------
# Lazy import guard
# ---------------------------------------------------------------------------


def _require_pyiec61850() -> Any:
    """Return the ``pyiec61850`` module, raising :class:`ImportError` if absent.

    The error message includes step-by-step build instructions so that
    developers get actionable output immediately.
    """
    try:
        import pyiec61850 as _lib  # type: ignore[import]

        return _lib
    except ImportError as exc:
        raise ImportError(
            "pyiec61850 (libiec61850 SWIG binding) is not installed.\n"
            "Build it once per environment:\n"
            "  git clone https://github.com/mz-automation/libiec61850\n"
            "  cd libiec61850 && mkdir build && cd build\n"
            "  cmake -DBUILD_PYTHON_BINDINGS=ON ..\n"
            "  make && sudo make install && sudo ldconfig\n"
            "See docs/internal/IEC61850_MMS_LIBRARY.md for details."
        ) from exc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MmsConnectError(ConnectionFailure):
    """Raised when MMS Initiate fails or the connection is refused.

    Parameters
    ----------
    message:
        Human-readable description including the IED address and error name.
    error_code:
        Raw ``IedClientError`` integer from libiec61850 (0 = OK, never set for
        a successful connect).
    """

    def __init__(self, message: str, error_code: int = 0) -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# MmsClient
# ---------------------------------------------------------------------------


class MmsClient:
    """Thin adapter wrapping ``pyiec61850`` (``IedConnection_*``) for MMS access.

    Usage::

        client = MmsClient("192.168.1.10", 102)
        client.connect()          # raises MmsConnectError on failure
        try:
            pdu = client.negotiated_pdu_size
            ...
        finally:
            client.close()

    Or as a context manager::

        with MmsClient("192.168.1.10", 102) as client:
            ...

    Thread safety
    -------------
    All calls must originate from the same OS thread that called
    :meth:`connect`.  libiec61850 ``IedConnection_*`` functions are not
    thread-safe.
    """

    def __init__(
        self,
        host: str,
        port: int,
        connect_timeout_ms: int = 10_000,
    ) -> None:
        self._host = host
        self._port = port
        self._connect_timeout_ms = connect_timeout_ms
        self._con: Any = None
        self._lib: Any = None
        self._pdu_size: int = 0

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open an MMS Initiate exchange with the remote IED.

        Raises
        ------
        ImportError
            If ``pyiec61850`` is not installed.
        MmsConnectError
            If the MMS Initiate is rejected or times out.
        """
        lib = _require_pyiec61850()
        self._lib = lib

        con = lib.IedConnection_create()
        lib.IedConnection_setConnectTimeout(con, self._connect_timeout_ms)

        error = lib.IedConnection_connect(con, self._host, self._port)
        if error != lib.IED_ERROR_OK:
            lib.IedConnection_destroy(con)
            name = _ied_error_name(error)
            raise MmsConnectError(
                f"MMS connect to {self._host}:{self._port} failed: {name}",
                error_code=error,
            )

        self._con = con

        # Read negotiated max PDU size from the MMS connection layer.
        try:
            mms_con = lib.IedConnection_getMmsConnection(con)
            self._pdu_size = lib.MmsConnection_getMaxPduSize(mms_con)
        except Exception:
            _log.debug("Could not read negotiated PDU size", exc_info=True)
            self._pdu_size = 0

        _log.debug(
            "MMS connect OK host=%s port=%d pdu_size=%d",
            self._host,
            self._port,
            self._pdu_size,
        )

    def close(self) -> None:
        """Send an MMS Close and release all resources.

        Safe to call multiple times or when not connected.
        """
        if self._con is not None and self._lib is not None:
            try:
                self._lib.IedConnection_close(self._con)
            except Exception:
                _log.debug("IedConnection_close raised; ignoring", exc_info=True)
            finally:
                self._lib.IedConnection_destroy(self._con)
                self._con = None
            _log.debug("MMS close OK host=%s port=%d", self._host, self._port)

    def abort(self) -> None:
        """Send an MMS Abort (used when the peer resets unexpectedly).

        Safe to call multiple times or when not connected.
        """
        if self._con is not None and self._lib is not None:
            try:
                self._lib.IedConnection_abort(self._con)
            except Exception:
                _log.debug("IedConnection_abort raised; ignoring", exc_info=True)
            finally:
                self._lib.IedConnection_destroy(self._con)
                self._con = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> MmsClient:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Properties (available after connect())
    # ------------------------------------------------------------------

    @property
    def negotiated_pdu_size(self) -> int:
        """Maximum PDU size negotiated in MMS Initiate (0 if unknown)."""
        return self._pdu_size

    @property
    def peer_implementation(self) -> str:
        """Peer implementation identifier from MMS Initiate response.

        libiec61850 does not expose the ``Implementation`` field from the
        Initiate-Response PDU in the current SWIG API.  Returns ``""``
        until that field is surfaced upstream.
        """
        return ""

    @property
    def is_connected(self) -> bool:
        """``True`` if :meth:`connect` has been called and :meth:`close` has not."""
        return self._con is not None
