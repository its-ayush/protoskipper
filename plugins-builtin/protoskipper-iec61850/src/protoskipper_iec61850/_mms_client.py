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
it only sees :class:`MmsClient`, :class:`MmsConnectError`,
:class:`MmsDirectoryError`, and plain Python types.  This makes it
straightforward to swap the implementation to a pure-Python MMS codec
(Option C) without touching any other file.

Public API
----------
* :class:`MmsConnectError` — raised by :meth:`MmsClient.connect` when the
  MMS Initiate exchange fails.
* :class:`MmsDirectoryError` — raised by directory service methods when the
  IED returns a non-OK error code.
* :class:`MmsClient` — context-manager-compatible client for one MMS
  connection to a single IED.

Constants
---------
* ``ACSI_CLASS_DATA_OBJECT`` ... ``ACSI_CLASS_MsCB`` -- ACSI class integers
  passed to :meth:`MmsClient.get_logical_node_directory`.
* ``FC_ST`` ... ``FC_NONE`` -- Functional constraint integers for read/write
  calls (``IEC61850_FC_*`` from libiec61850).
* :class:`MmsDecodedValue` -- decoded result of :meth:`MmsClient.read_object`.

Thread safety
-------------
A :class:`MmsClient` instance must be used from a single thread only.
``IedConnection_*`` calls from libiec61850 are not thread-safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from protoskipper.core.errors import ConnectionFailure, DriverError, EncodingError

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ACSI class constants (IEC 61850-7-2 § mapped from libiec61850
# ied_client_api.h  ACSIClass enum, values 0-9)
# ---------------------------------------------------------------------------
ACSI_CLASS_DATA_OBJECT: int = 0
ACSI_CLASS_DATA_SET: int = 1
ACSI_CLASS_BRCB: int = 2
ACSI_CLASS_URCB: int = 3
ACSI_CLASS_LCB: int = 4
ACSI_CLASS_LOG: int = 5
ACSI_CLASS_SGCB: int = 6
ACSI_CLASS_GoCB: int = 7
ACSI_CLASS_GsCB: int = 8
ACSI_CLASS_MsCB: int = 9

# ---------------------------------------------------------------------------
# Functional constraint (FC) constants
# (IEC61850_FC_* from libiec61850 ied_client_api.h)
# ---------------------------------------------------------------------------
FC_ST: int = 0  # Status
FC_MX: int = 1  # Measured value
FC_SP: int = 2  # Setting (persistent)
FC_SV: int = 3  # Substitution value
FC_CF: int = 4  # Configuration
FC_DC: int = 5  # Description
FC_SG: int = 6  # Setting group (active)
FC_SE: int = 7  # Setting group (editable)
FC_SR: int = 8  # Service response
FC_OR: int = 9  # Operate received
FC_BL: int = 10  # Blocking
FC_EX: int = 11  # Extended
FC_CO: int = 12  # Control output
FC_NONE: int = -1  # No specific FC

# ---------------------------------------------------------------------------
# Control model constants (IEC 61850-7-2 §17.5 / libiec61850 ControlModel)
# ---------------------------------------------------------------------------
CONTROL_MODEL_STATUS_ONLY: int = 0
CONTROL_MODEL_DIRECT_NORMAL: int = 1  # Direct with normal security
CONTROL_MODEL_SBO_NORMAL: int = 2  # Select-before-operate with normal security
CONTROL_MODEL_DIRECT_ENHANCED: int = 3  # Direct with enhanced security
CONTROL_MODEL_SBO_ENHANCED: int = 4  # Select-before-operate with enhanced security

# ---------------------------------------------------------------------------
# AddCause code names (IEC 61850-7-2 §17.5.3 / libiec61850 AddCause enum)
# ---------------------------------------------------------------------------
_ADD_CAUSE_NAMES: dict[int, str] = {
    0: "UNKNOWN",
    1: "NOT_SUPPORTED",
    2: "BLOCKED_BY_SWITCHING_HIERARCHY",
    3: "SELECT_FAILED",
    4: "INVALID_ADDRESS",
    5: "SYNCHROCHECK_FAILED",
    6: "TIME_LIMIT_OVER",
    7: "NO_RESOURCES",
    8: "PARAMETER_CHANGE_IN_EXECUTION",
    9: "STEP_LIMIT",
    10: "BLOCKED_BY_MODE",
    11: "BLOCKED_BY_PROCESS",
    12: "BLOCKED_BY_INTERLOCKING",
    13: "BLOCKED_BY_SYNCHROCHECK",
    14: "COMMAND_ALREADY_IN_EXECUTION",
    15: "BLOCKED_BY_HEALTH",
    16: "ONE_OF_N_CONTROL",
    17: "ABORTION_BY_CANCEL",
    20: "OBJECT_NOT_SELECTED",
    21: "OBJECT_ALREADY_SELECTED",
    28: "NO_ACCESS_AUTHORITY",
    29: "ENDED_WITH_OVERSHOOT",
    30: "ABORTION_BY_TRIP",
    31: "OBJECT_CONNECTED",
    34: "OBJECT_NONE_EXISTING",
}


def _add_cause_name(code: int) -> str:
    return _ADD_CAUSE_NAMES.get(code, f"ADD_CAUSE_{code}")


# ---------------------------------------------------------------------------
# Reporting constants (P8.B.6)
# ---------------------------------------------------------------------------
# TriggerOptions bits (IEC 61850-7-2 §9.1.3 / libiec61850 TriggerOptions enum)
# ---------------------------------------------------------------------------
TRG_OPS_DATA_CHANGE: int = 2  # dchg
TRG_OPS_QUALITY_CHANGE: int = 4  # qchg
TRG_OPS_DATA_UPDATE: int = 8  # dupd
TRG_OPS_INTEGRITY: int = 64  # period
TRG_OPS_GI: int = 128  # general interrogation

# ReasonForInclusion values (libiec61850 ReasonForInclusion enum)
REASON_NOT_INCLUDED: int = 0
REASON_DATA_CHANGE: int = 1
REASON_QUALITY_CHANGE: int = 2
REASON_DATA_UPDATE: int = 4
REASON_INTEGRITY: int = 8
REASON_GI: int = 16

# RCB element mask bits for IedConnection_setRCBValues parametersMask
# (libiec61850 ClientReportControlBlock_ElementsToSet)
_RCB_ELEMENT_RPT_ENA: int = 2
_RCB_ELEMENT_TRG_OPS: int = 256
_RCB_ELEMENT_INTG_PD: int = 512


# ---------------------------------------------------------------------------
# Quality bit positions (for MmsValue_getBitStringBit)
# IEC 61850-7-2 quality bitstring encoding
# ---------------------------------------------------------------------------
_Q_VALIDITY_BIT0: int = 0  # MSB of 2-bit validity: 0=good,1=invalid,2=reserved,3=questionable
_Q_VALIDITY_BIT1: int = 1  # LSB of 2-bit validity
_Q_SOURCE_SUBSTITUTED: int = 12  # source=substituted -> SIMULATED
_Q_TEST: int = 13  # test bit

# ---------------------------------------------------------------------------
# MMS type constants (MmsType enum from libiec61850 mms_value.h)
# ---------------------------------------------------------------------------
_MMS_ARRAY: int = 0
_MMS_STRUCTURE: int = 1
_MMS_BOOLEAN: int = 2
_MMS_BIT_STRING: int = 3
_MMS_INTEGER: int = 4
_MMS_UNSIGNED: int = 5
_MMS_FLOAT: int = 6
_MMS_OCTET_STRING: int = 7
_MMS_VISIBLE_STRING: int = 8
_MMS_STRING: int = 13
_MMS_UTC_TIME: int = 14

# ---------------------------------------------------------------------------
# IedClientError code -> human-readable name
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
# MmsValue decoding helpers (P8.B.4)
# ---------------------------------------------------------------------------


@dataclass
class MmsDecodedValue:
    """Result of :meth:`MmsClient.read_object`.

    Attributes
    ----------
    value:
        Decoded Python primitive: ``float``, ``int``, ``bool``, ``str``, or
        a ``list`` for structures/arrays.  ``None`` if the read failed or the
        type is unrecognised.
    quality_validity:
        IEC 61850 validity field (0 = good, 1 = invalid, 2 = reserved,
        3 = questionable).  Always 0 for non-quality attributes.
    is_substituted:
        ``True`` when the quality ``source`` bit is set (value is substituted
        / simulated).  Always ``False`` for non-quality attributes.
    is_test:
        ``True`` when the quality ``test`` bit is set.
    timestamp_ms:
        Milliseconds since the Unix epoch from the ``t`` attribute.
        0 when not available.
    """

    value: Any
    quality_validity: int = 0
    is_substituted: bool = False
    is_test: bool = False
    timestamp_ms: int = 0


@dataclass
class ControlResult:
    """Result of :meth:`MmsClient.write_control`.

    Attributes
    ----------
    success:
        True if the operate phase completed without error.
    control_model:
        Integer control model as reported by the IED (``CONTROL_MODEL_*``
        constants).
    add_cause:
        IEC 61850 AddCause integer from the last ApplError.  0 when not
        applicable or when the operate succeeded cleanly.
    add_cause_name:
        Human-readable name for *add_cause*.
    error_str:
        Empty string on success; diagnostic message on failure.
    """

    success: bool
    control_model: int = 0
    add_cause: int = 0
    add_cause_name: str = "UNKNOWN"
    error_str: str = ""


@dataclass
class RcbValues:
    """Snapshot of a Report Control Block's key attributes.

    Attributes
    ----------
    rcb_ref:
        Full RCB reference, e.g. ``"LD0/LLN0.BR.rcbMeas01"``.
    is_buffered:
        True for BRCB, False for URCB.
    rpt_id:
        Report ID string (``RptID`` attribute).
    dat_set:
        Dataset reference (``DatSet`` attribute).
    conf_rev:
        Configuration revision counter.
    opt_flds:
        OptFlds bit mask.
    buf_tm:
        Buffer time in milliseconds.
    trg_ops:
        TriggerOptions bit mask (see ``TRG_OPS_*`` constants).
    intg_pd:
        Integrity period in milliseconds (0 = disabled).
    rpt_ena:
        True if reporting is currently enabled.
    resv:
        True if the RCB is reserved (URCB only).
    """

    rcb_ref: str
    is_buffered: bool
    rpt_id: str = ""
    dat_set: str = ""
    conf_rev: int = 0
    opt_flds: int = 0
    buf_tm: int = 0
    trg_ops: int = 0
    intg_pd: int = 0
    rpt_ena: bool = False
    resv: bool = False  # URCB only


@dataclass
class ReportEntry:
    """One decoded incoming report from a BRCB or URCB.

    Attributes
    ----------
    rcb_ref:
        Reference of the subscribed RCB.
    rpt_id:
        Report ID from the PDU.
    dataset_ref:
        Dataset reference from the PDU.
    is_buffered:
        True if report came from a BRCB.
    has_timestamp:
        True if the report PDU contains a timestamp.
    timestamp_ms:
        Report timestamp in milliseconds since the Unix epoch.
        0 when :attr:`has_timestamp` is False.
    seq_num:
        Sequence number from the report PDU (0 if not included).
    entries:
        Per-member ``(reason: int, value: Any)`` pairs.  *reason* is one
        of the ``REASON_*`` constants; *value* is the decoded Python
        primitive (same encoding as :func:`_mms_value_to_python`).
    """

    rcb_ref: str
    rpt_id: str
    dataset_ref: str
    is_buffered: bool
    has_timestamp: bool = False
    timestamp_ms: int = 0
    seq_num: int = 0
    entries: list[tuple[int, Any]] = field(default_factory=list)


@dataclass
class LogEntry:
    """One decoded journal entry returned by :meth:`MmsClient.query_log_by_time`
    or :meth:`MmsClient.query_log_after`.

    Attributes
    ----------
    log_ref:
        The log object reference that was queried,
        e.g. ``"LD0/LLN0$GeneralLog"``.
    entry_id:
        Opaque entry identifier as raw bytes (``MMS_OCTET_STRING``).
    occurrence_time_ms:
        Journal timestamp in milliseconds since the Unix epoch
        (decoded from ``MMS_BINARY_TIME``).  0 when not present.
    variables:
        List of ``(tag, value)`` pairs decoded from the journal
        entry's data, where *tag* is the MMS variable tag string and
        *value* is the decoded Python value (same types as
        :attr:`MmsDecodedValue.value`).
    """

    log_ref: str
    entry_id: bytes
    occurrence_time_ms: int
    variables: list[tuple[str, Any]] = field(default_factory=list)


@dataclass
class FileInfo:
    """Metadata for a single entry in a server-side file directory.

    Attributes
    ----------
    name:
        File path as returned by the server (e.g. ``"COMTRADE/fault01.cfg"``).
    size:
        File size in bytes.  0 when the server does not report a size.
    last_modified_ms:
        UTC timestamp of the last modification in milliseconds since the
        Unix epoch.  0 when not available.
    """

    name: str
    size: int
    last_modified_ms: int


@dataclass
class SgcbValues:
    """Snapshot of a Setting Group Control Block (SGCB) at read time.

    IEC 61850-7-2 §8.7 defines the SGCB attributes.  Only the two most
    useful attributes are surfaced here; all reads use the **SG** functional
    constraint (FC=6).

    Attributes
    ----------
    num_of_sgs:
        Total number of setting groups configured on the IED (1-based count,
        always ≥ 1).
    act_sg:
        Index of the currently active setting group (1-based, 1 ≤ act_sg ≤
        num_of_sgs).
    """

    num_of_sgs: int
    act_sg: int


def _mms_value_to_python(lib: Any, val: Any) -> Any:
    """Recursively convert a pyiec61850 ``MmsValue`` to a Python object.

    Caller is responsible for calling ``MmsValue_delete`` on *val* after use.
    Returns ``None`` for unrecognised types or if *val* is ``None``.
    """
    if val is None:
        return None
    typ = lib.MmsValue_getType(val)
    if typ == _MMS_BOOLEAN:
        return bool(lib.MmsValue_getBoolean(val))
    if typ == _MMS_INTEGER:
        return int(lib.MmsValue_toInt32(val))
    if typ == _MMS_UNSIGNED:
        return int(lib.MmsValue_toUint32(val))
    if typ == _MMS_FLOAT:
        return float(lib.MmsValue_toFloat(val))
    if typ in (_MMS_VISIBLE_STRING, _MMS_STRING):
        return str(lib.MmsValue_toString(val))
    if typ == _MMS_BIT_STRING:
        return int(lib.MmsValue_getBitStringAsInteger(val))
    if typ == _MMS_UTC_TIME:
        return int(lib.MmsValue_getUtcTimeInMs(val))
    if typ in (_MMS_ARRAY, _MMS_STRUCTURE):
        n = lib.MmsValue_getArraySize(val)
        return [_mms_value_to_python(lib, lib.MmsValue_getElement(val, i)) for i in range(n)]
    _log.debug("Unrecognised MmsType %d; returning None", typ)
    return None


def _decode_q_bits(lib: Any, q_val: Any) -> tuple[int, bool, bool]:
    """Extract ``(validity, is_substituted, is_test)`` from a quality MmsValue.

    Uses ``MmsValue_getBitStringBit`` so the result is independent of how
    libiec61850 packs the bit string into an integer.

    Returns ``(0, False, False)`` on any failure.
    """
    if q_val is None:
        return 0, False, False
    try:
        b0 = bool(lib.MmsValue_getBitStringBit(q_val, _Q_VALIDITY_BIT0))
        b1 = bool(lib.MmsValue_getBitStringBit(q_val, _Q_VALIDITY_BIT1))
        validity = (int(b0) << 1) | int(b1)
        substituted = bool(lib.MmsValue_getBitStringBit(q_val, _Q_SOURCE_SUBSTITUTED))
        is_test = bool(lib.MmsValue_getBitStringBit(q_val, _Q_TEST))
        return validity, substituted, is_test
    except Exception:
        _log.debug("Failed to decode quality MmsValue", exc_info=True)
        return 0, False, False


def _python_to_mms_ctlval(lib: Any, value: Any) -> Any:
    """Encode a Python value as an MmsValue suitable for a control ctlVal.

    Parameters
    ----------
    lib:
        The ``pyiec61850`` module (already verified present by the caller).
    value:
        Python value to encode.  Must be ``bool``, ``int``, or ``float``.

    Returns
    -------
    Any
        A newly-allocated ``MmsValue``; the caller must call
        ``lib.MmsValue_delete`` when done.

    Raises
    ------
    EncodingError
        If *value* is not a ``bool``, ``int``, or ``float``.
    """
    if isinstance(value, bool):
        return lib.MmsValue_newBoolean(value)
    if isinstance(value, int):
        return lib.MmsValue_newIntegerFromInt32(value)
    if isinstance(value, float):
        return lib.MmsValue_newFloat(value)
    raise EncodingError(
        f"Cannot encode {type(value).__name__!r} as MMS ctlVal; "
        "only bool, int, and float are supported."
    )


def _on_incoming_report(
    lib: Any,
    report: Any,
    rcb_ref: str,
    is_buffered: bool,
    callback: Any,
) -> None:
    """Decode a libiec61850 ``ClientReport`` and invoke *callback*.

    Errors during decoding are caught and logged at DEBUG level so that a
    malformed report never crashes the connection thread.
    """
    try:
        rpt_id = str(lib.ClientReport_getRptId(report) or "")
        dataset_ref = str(lib.ClientReport_getDataSetName(report) or "")
        has_ts = bool(lib.ClientReport_hasTimestamp(report))
        ts_ms = int(lib.ClientReport_getTimestamp(report)) if has_ts else 0
        seq_num_raw = getattr(lib, "ClientReport_getSeqNum", None)
        seq_num = int(seq_num_raw(report)) if seq_num_raw is not None else 0
        n = int(lib.ClientReport_getDataSetEntryCount(report))
        data_vals = lib.ClientReport_getDataSetValues(report)
        entries: list[tuple[int, Any]] = []
        for i in range(n):
            reason = int(lib.ClientReport_getReasonForInclusion(report, i))
            member = _mms_value_to_python(lib, lib.MmsValue_getElement(data_vals, i))
            entries.append((reason, member))
        callback(
            ReportEntry(
                rcb_ref=rcb_ref,
                rpt_id=rpt_id,
                dataset_ref=dataset_ref,
                is_buffered=is_buffered,
                has_timestamp=has_ts,
                timestamp_ms=ts_ms,
                seq_num=seq_num,
                entries=entries,
            )
        )
    except Exception:
        _log.debug("Error processing incoming report for %r", rcb_ref, exc_info=True)


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


# ---------------------------------------------------------------------------
# LinkedList helper
# ---------------------------------------------------------------------------


def _ll_to_list(lib: Any, ll: Any) -> list[str]:
    """Convert a pyiec61850 ``LinkedList`` of ``char*`` to ``list[str]``.

    ``LinkedList_getData`` returns SWIG ``void*`` objects; ``lib.toCharP``
    casts them to Python ``str`` (they are C ``char*`` in practice).
    Frees the LinkedList after conversion via ``lib.LinkedList_destroy``.
    Returns an empty list if *ll* is ``None``.
    """
    result: list[str] = []
    if ll is None:
        return result
    node = lib.LinkedList_getNext(ll)
    while node is not None:
        data = lib.LinkedList_getData(node)
        if data is not None:
            raw = lib.toCharP(data)
            result.append(
                raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            )
        node = lib.LinkedList_getNext(node)
    lib.LinkedList_destroy(ll)
    return result


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MmsDirectoryError(DriverError):
    """Raised when an MMS GetDirectory service call returns a non-OK error.

    Parameters
    ----------
    message:
        Human-readable description including the service name and error name.
    error_code:
        Raw ``IedClientError`` integer from libiec61850.
    """

    def __init__(self, message: str, error_code: int = 0) -> None:
        super().__init__(message)
        self.error_code = error_code


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
        # Stores report handler closures keyed by rcb_ref to prevent GC
        self._report_handlers: dict[str, Any] = {}

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
        # MmsConnection_getMaxPduSize is absent in older pyiec61850 bindings;
        # fall back to 0 (unlimited) rather than crashing.
        get_pdu_size = getattr(lib, "MmsConnection_getMaxPduSize", None)
        if get_pdu_size is not None:
            try:
                mms_con = lib.IedConnection_getMmsConnection(con)
                self._pdu_size = get_pdu_size(mms_con)
            except Exception:
                self._pdu_size = 0
        else:
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
        self._report_handlers.clear()

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
        """``True`` if the MMS connection is active at the C layer."""
        if self._con is None:
            return False
        try:
            return self._lib.IedConnection_getState(self._con) == self._lib.IED_STATE_CONNECTED
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Read services (P8.B.4)
    # ------------------------------------------------------------------
    #
    # ``IedConnection_readObject`` takes a data reference (in dot notation,
    # e.g. ``"LD0/MMXU1.A"`` or ``"LD0/LLN0.Mod.stVal"``) and an FC
    # integer, and returns ``(MmsValue, IedClientError)`` via the SWIG
    # OUTPUT typemap for ``IedClientError*``.

    def read_object(self, object_ref: str, fc: int) -> MmsDecodedValue:
        """Read a single data object or data attribute.

        Parameters
        ----------
        object_ref:
            IEC 61850 data reference in dot notation, e.g.
            ``"LD0/MMXU1.A"`` or ``"LD0/LLN0.Mod.stVal"``.
            Must **not** include a ``[FC]`` suffix.
        fc:
            Functional constraint (one of the ``FC_*`` module constants).

        Returns
        -------
        MmsDecodedValue
            ``quality_validity``, ``is_substituted``, ``is_test``, and
            ``timestamp_ms`` are all 0 / ``False``; this method only decodes
            the raw value.  Use :meth:`read_do_with_meta` to populate those.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK error code, or if the connection is
            not in the CONNECTED state.
        """
        lib = self._lib
        # Guard: check the C-level connection state *before* entering
        # libiec61850.  If the IED dropped the session (state CLOSED/CLOSING),
        # calling IedConnection_readObject on a non-CONNECTED handle can
        # trigger undefined behaviour inside libiec61850's MMS layer.
        if self._con is None:
            raise MmsDirectoryError("Not connected", error_code=lib.IED_ERROR_NOT_CONNECTED)
        state = lib.IedConnection_getState(self._con)
        if state != lib.IED_STATE_CONNECTED:
            raise MmsDirectoryError(
                f"Connection is not active (state={state})",
                error_code=lib.IED_ERROR_NOT_CONNECTED,
            )
        mms_val, error = lib.IedConnection_readObject(self._con, object_ref, fc)
        if error != lib.IED_ERROR_OK:
            if mms_val is not None:
                lib.MmsValue_delete(mms_val)
            raise MmsDirectoryError(
                f"ReadObject({object_ref!r}, FC={fc}) failed: {_ied_error_name(error)}",
                error_code=error,
            )
        try:
            python_val = _mms_value_to_python(lib, mms_val)
        finally:
            if mms_val is not None:
                lib.MmsValue_delete(mms_val)
        return MmsDecodedValue(value=python_val)

    def _read_q(self, q_ref: str, fc: int) -> tuple[int, bool, bool]:
        """Internal: read a quality attribute and decode its bits.

        Returns ``(0, False, False)`` on any error so callers never raise.
        """
        lib = self._lib
        try:
            mms_q, error = lib.IedConnection_readObject(self._con, q_ref, fc)
            if error != lib.IED_ERROR_OK or mms_q is None:
                if mms_q is not None:
                    lib.MmsValue_delete(mms_q)
                return 0, False, False
            try:
                return _decode_q_bits(lib, mms_q)
            finally:
                lib.MmsValue_delete(mms_q)
        except Exception:
            _log.debug("Failed to read quality %r", q_ref, exc_info=True)
            return 0, False, False

    def _read_ts_ms(self, t_ref: str, fc: int) -> int:
        """Internal: read a timestamp attribute and return ms since epoch.

        Returns 0 on any error so callers never raise.
        """
        lib = self._lib
        try:
            mms_t, error = lib.IedConnection_readObject(self._con, t_ref, fc)
            if error != lib.IED_ERROR_OK or mms_t is None:
                if mms_t is not None:
                    lib.MmsValue_delete(mms_t)
                return 0
            try:
                ts = _mms_value_to_python(lib, mms_t)
            finally:
                lib.MmsValue_delete(mms_t)
            return int(ts) if ts is not None else 0
        except Exception:
            _log.debug("Failed to read timestamp %r", t_ref, exc_info=True)
            return 0

    def read_do_with_meta(self, do_ref: str, fc: int) -> MmsDecodedValue:
        """Read a data object plus its standard ``q`` and ``t`` sub-attributes.

        Issues three separate ``IedConnection_readObject`` calls:

        1. ``do_ref`` with *fc* -- the value.
        2. ``do_ref + ".q"`` with *fc* -- quality bit string.
        3. ``do_ref + ".t"`` with *fc* -- UTC timestamp.

        Quality and timestamp reads are best-effort: errors are logged at
        DEBUG level and result in zero / False defaults.

        Parameters
        ----------
        do_ref:
            Data object reference, e.g. ``"LD0/MMXU1.A"`` or
            ``"LD0/LLN0.Mod"``.  Must not include a ``[FC]`` suffix.
        fc:
            Functional constraint for all three reads.

        Returns
        -------
        MmsDecodedValue
            All fields populated; ``value`` may be a ``list`` for structured
            DOs (elements are anonymous -- their names come from the SCL).

        Raises
        ------
        MmsDirectoryError
            If the primary value read fails.
        """
        decoded = self.read_object(do_ref, fc)
        validity, substituted, is_test = self._read_q(f"{do_ref}.q", fc)
        ts_ms = self._read_ts_ms(f"{do_ref}.t", fc)
        return MmsDecodedValue(
            value=decoded.value,
            quality_validity=validity,
            is_substituted=substituted,
            is_test=is_test,
            timestamp_ms=ts_ms,
        )

    # ------------------------------------------------------------------
    # Write / control services (P8.B.5)
    # ------------------------------------------------------------------
    #
    # libiec61850 control service API (from ied_client_api.h):
    #   ControlObjectClient_create(ref, con)           -> ControlObjectClient
    #   ControlObjectClient_getControlModel(ctrl)      -> int (0-4)
    #   ControlObjectClient_select(ctrl)               -> bool
    #   ControlObjectClient_selectWithValue(ctrl, val) -> bool
    #   ControlObjectClient_operate(ctrl, val, opTm)   -> bool
    #   ControlObjectClient_getLastApplError(ctrl)     -> LastApplError{.error, .addCause}
    #   ControlObjectClient_destroy(ctrl)

    def write_control(
        self,
        do_ref: str,
        value: Any,
        oper_time_ms: int = 0,
    ) -> ControlResult:
        """Issue a control operation to a controllable data object.

        Automatically selects the control sequence based on the control model
        reported by the IED:

        * :data:`CONTROL_MODEL_DIRECT_NORMAL` /
          :data:`CONTROL_MODEL_DIRECT_ENHANCED` — operate immediately.
        * :data:`CONTROL_MODEL_SBO_NORMAL` — Select then Operate.
        * :data:`CONTROL_MODEL_SBO_ENHANCED` — SelectWithValue then Operate.

        Parameters
        ----------
        do_ref:
            Data object reference of the controllable object, e.g.
            ``"LD0/XCBR1.Pos"``.  Must not include a ``[FC]`` suffix.
        value:
            Control value (ctlVal).  Must be ``bool``, ``int``, or ``float``.
        oper_time_ms:
            UTC time in milliseconds for time-activated operate.  0 means
            "operate now".

        Returns
        -------
        ControlResult
            Never raises (other than :class:`~protoskipper.core.errors.EncodingError`
            for unsupported *value* types); errors are captured in
            :attr:`ControlResult.error_str`.
        """
        lib = self._lib
        ctrl = lib.ControlObjectClient_create(do_ref, self._con)
        if ctrl is None:
            return ControlResult(
                success=False,
                error_str=f"ControlObjectClient_create returned None for {do_ref!r}",
            )
        try:
            model = int(lib.ControlObjectClient_getControlModel(ctrl))
            mms_val = _python_to_mms_ctlval(lib, value)  # raises EncodingError if invalid
            try:
                # SBO select phase
                if model == CONTROL_MODEL_SBO_NORMAL:
                    if not bool(lib.ControlObjectClient_select(ctrl)):
                        last_err = lib.ControlObjectClient_getLastApplError(ctrl)
                        ac = int(getattr(last_err, "addCause", 0))
                        return ControlResult(
                            success=False,
                            control_model=model,
                            add_cause=ac,
                            add_cause_name=_add_cause_name(ac),
                            error_str=f"Select failed: addCause={_add_cause_name(ac)}",
                        )
                elif model == CONTROL_MODEL_SBO_ENHANCED and not bool(
                    lib.ControlObjectClient_selectWithValue(ctrl, mms_val)
                ):
                    last_err = lib.ControlObjectClient_getLastApplError(ctrl)
                    ac = int(getattr(last_err, "addCause", 0))
                    return ControlResult(
                        success=False,
                        control_model=model,
                        add_cause=ac,
                        add_cause_name=_add_cause_name(ac),
                        error_str=f"SelectWithValue failed: addCause={_add_cause_name(ac)}",
                    )

                # Operate phase (all models)
                operated = bool(lib.ControlObjectClient_operate(ctrl, mms_val, oper_time_ms))
                last_err = lib.ControlObjectClient_getLastApplError(ctrl)
                ac = int(getattr(last_err, "addCause", 0))
                if operated:
                    return ControlResult(
                        success=True,
                        control_model=model,
                        add_cause=ac,
                        add_cause_name=_add_cause_name(ac),
                    )
                return ControlResult(
                    success=False,
                    control_model=model,
                    add_cause=ac,
                    add_cause_name=_add_cause_name(ac),
                    error_str=f"Operate failed: addCause={_add_cause_name(ac)}",
                )
            except EncodingError:
                raise
            except Exception as exc:
                _log.debug(
                    "write_control(%r) error during select/operate: %s",
                    do_ref,
                    exc,
                    exc_info=True,
                )
                return ControlResult(success=False, error_str=str(exc))
            finally:
                lib.MmsValue_delete(mms_val)
        except EncodingError:
            raise
        finally:
            lib.ControlObjectClient_destroy(ctrl)

    # ------------------------------------------------------------------
    # Reporting services (P8.B.6)
    # ------------------------------------------------------------------
    #
    # GetRCBValues returns a ``ClientReportControlBlock*`` whose attributes
    # are read via ``ClientReportControlBlock_*`` getters and then freed via
    # ``ClientReportControlBlock_destroy``.  SetRCBValues transmits only the
    # attributes indicated by the ``parametersMask`` bitmask
    # (``_RCB_ELEMENT_*`` constants).
    #
    # The report handler installed via ``IedConnection_installReportHandler``
    # is stored in ``self._report_handlers[rcb_ref]`` to prevent the closure
    # from being garbage-collected while reporting is active.

    def get_rcb_values(self, rcb_ref: str, is_buffered: bool) -> RcbValues:
        """Read current attribute values of a Report Control Block.

        Parameters
        ----------
        rcb_ref:
            Full RCB reference, e.g. ``"LD0/LLN0.BR.rcbMeas01"``.
        is_buffered:
            True for BRCB, False for URCB.

        Returns
        -------
        RcbValues
            Snapshot of the RCB state.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK error code.
        """
        lib = self._lib
        rcb, error = lib.IedConnection_getRCBValues(self._con, rcb_ref, is_buffered)
        if error != lib.IED_ERROR_OK:
            if rcb is not None:
                lib.ClientReportControlBlock_destroy(rcb)
            raise MmsDirectoryError(
                f"GetRCBValues({rcb_ref!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )
        try:
            rpt_id = str(lib.ClientReportControlBlock_getRptId(rcb) or "")
            dat_set = str(lib.ClientReportControlBlock_getDatSet(rcb) or "")
            conf_rev = int(lib.ClientReportControlBlock_getConfRev(rcb))
            opt_flds = int(lib.ClientReportControlBlock_getOptFlds(rcb))
            buf_tm = int(lib.ClientReportControlBlock_getBufTm(rcb))
            trg_ops = int(lib.ClientReportControlBlock_getTrgOps(rcb))
            intg_pd = int(lib.ClientReportControlBlock_getIntgPd(rcb))
            rpt_ena = bool(lib.ClientReportControlBlock_getRptEna(rcb))
            resv = bool(lib.ClientReportControlBlock_getResv(rcb)) if not is_buffered else False
        finally:
            lib.ClientReportControlBlock_destroy(rcb)
        return RcbValues(
            rcb_ref=rcb_ref,
            is_buffered=is_buffered,
            rpt_id=rpt_id,
            dat_set=dat_set,
            conf_rev=conf_rev,
            opt_flds=opt_flds,
            buf_tm=buf_tm,
            trg_ops=trg_ops,
            intg_pd=intg_pd,
            rpt_ena=rpt_ena,
            resv=resv,
        )

    def enable_report(
        self,
        rcb_ref: str,
        is_buffered: bool,
        trg_ops: int = TRG_OPS_DATA_CHANGE | TRG_OPS_QUALITY_CHANGE,
        intg_pd: int = 0,
        on_report: Any = None,
    ) -> RcbValues:
        """Enable reporting for a Report Control Block.

        Installs a Python callback (if provided), sets ``TrgOps`` and
        ``IntgPd``, and enables reporting by setting ``RptEna=True``.

        Parameters
        ----------
        rcb_ref:
            Full RCB reference, e.g. ``"LD0/LLN0.BR.rcbMeas01"``.
        is_buffered:
            True for BRCB, False for URCB.
        trg_ops:
            ``TriggerOptions`` bitmask (combination of ``TRG_OPS_*``).
        intg_pd:
            Integrity period in milliseconds.  0 = disabled.
        on_report:
            Callable invoked with a :class:`ReportEntry` for each incoming
            report.  ``None`` = install no handler (reports are discarded).

        Returns
        -------
        RcbValues
            Snapshot of the RCB attributes *after* enabling.

        Raises
        ------
        MmsDirectoryError
            If ``GetRCBValues`` or ``SetRCBValues`` fails.
        """
        lib = self._lib
        rcb, error = lib.IedConnection_getRCBValues(self._con, rcb_ref, is_buffered)
        if error != lib.IED_ERROR_OK:
            if rcb is not None:
                lib.ClientReportControlBlock_destroy(rcb)
            raise MmsDirectoryError(
                f"GetRCBValues({rcb_ref!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )
        try:
            rpt_id = str(lib.ClientReportControlBlock_getRptId(rcb) or "")
            if on_report is not None:

                def _handler(con: Any, report: Any, param: Any) -> None:
                    _on_incoming_report(lib, report, rcb_ref, is_buffered, on_report)

                self._report_handlers[rcb_ref] = _handler
                lib.IedConnection_installReportHandler(self._con, rcb_ref, rpt_id, _handler, None)
            lib.ClientReportControlBlock_setTrgOps(rcb, trg_ops)
            lib.ClientReportControlBlock_setIntgPd(rcb, intg_pd)
            lib.ClientReportControlBlock_setRptEna(rcb, True)
            mask = _RCB_ELEMENT_RPT_ENA | _RCB_ELEMENT_TRG_OPS | _RCB_ELEMENT_INTG_PD
            error2 = lib.IedConnection_setRCBValues(self._con, rcb, mask, True)
            if error2 != lib.IED_ERROR_OK:
                self._report_handlers.pop(rcb_ref, None)
                raise MmsDirectoryError(
                    f"SetRCBValues(RptEna=True) for {rcb_ref!r} failed: {_ied_error_name(error2)}",
                    error_code=error2,
                )
        finally:
            lib.ClientReportControlBlock_destroy(rcb)

        # Return a fresh snapshot so the caller knows the live RCB state
        return self.get_rcb_values(rcb_ref, is_buffered)

    def disable_report(self, rcb_ref: str, is_buffered: bool) -> None:
        """Disable reporting for a Report Control Block.

        Sets ``RptEna=False`` and removes the installed report handler.
        Errors during the ``GetRCBValues`` or ``SetRCBValues`` calls are
        logged at WARNING level and do not raise so that ``close()`` can
        always clean up.

        Parameters
        ----------
        rcb_ref:
            Full RCB reference.
        is_buffered:
            True for BRCB, False for URCB.
        """
        lib = self._lib
        rcb, error = lib.IedConnection_getRCBValues(self._con, rcb_ref, is_buffered)
        if error != lib.IED_ERROR_OK:
            _log.warning("GetRCBValues(%r) for disable failed: %s", rcb_ref, _ied_error_name(error))
            self._report_handlers.pop(rcb_ref, None)
            return
        try:
            lib.ClientReportControlBlock_setRptEna(rcb, False)
            error2 = lib.IedConnection_setRCBValues(self._con, rcb, _RCB_ELEMENT_RPT_ENA, True)
            if error2 != lib.IED_ERROR_OK:
                _log.warning(
                    "SetRCBValues(RptEna=False) for %r failed: %s",
                    rcb_ref,
                    _ied_error_name(error2),
                )
        finally:
            lib.ClientReportControlBlock_destroy(rcb)
        self._report_handlers.pop(rcb_ref, None)

    # ------------------------------------------------------------------
    # Directory services (P8.B.3)
    # ------------------------------------------------------------------
    #
    # SWIG OUTPUT-typemap pattern: functions whose C signature takes an
    # ``IedClientError* error`` output parameter return a Python tuple
    # ``(LinkedList_result, int_error_code)`` because libiec61850's SWIG
    # file applies ``%apply IedClientError *OUTPUT { IedClientError* error }``.
    # The LinkedList is then converted to a plain ``list[str]`` by
    # ``_ll_to_list`` which also frees the list.

    # ---------------------------------------------------------------------------
    # Log services (P8.B.7)
    # ---------------------------------------------------------------------------

    def _decode_journal_entries(
        self,
        lib: Any,
        entries_ll: Any,
        log_ref: str,
    ) -> list[LogEntry]:
        """Decode a pyiec61850 ``LinkedList<MmsJournalEntry>`` to :class:`LogEntry`.

        Ownership of *entries_ll* is transferred: this method frees each
        ``MmsJournalEntry`` node after decoding and destroys the list.
        """
        result: list[LogEntry] = []
        if entries_ll is None:
            return result
        try:
            node = lib.LinkedList_getNext(entries_ll)
            while node is not None:
                entry = lib.LinkedList_getData(node)
                # --- occurrence time (MMS_BINARY_TIME) ---
                occ_val = lib.MmsJournalEntry_getOccurenceTime(entry)
                try:
                    occ_ms: int = (
                        lib.MmsValue_getBinaryTimeAsUtcMs(occ_val) if occ_val is not None else 0
                    )
                except Exception:
                    occ_ms = 0
                # --- entry ID (MMS_OCTET_STRING) ---
                eid_val = lib.MmsJournalEntry_getEntryID(entry)
                try:
                    if eid_val is not None:
                        buf = lib.MmsValue_getOctetStringBuffer(eid_val)
                        size = lib.MmsValue_getOctetStringSize(eid_val)
                        entry_id: bytes = bytes(buf[:size]) if buf is not None else b""
                    else:
                        entry_id = b""
                except Exception:
                    entry_id = b""
                # --- journal variables ---
                variables: list[tuple[str, Any]] = []
                jvars_ll = lib.MmsJournalEntry_getJournalVariables(entry)
                if jvars_ll is not None:
                    jv_node = lib.LinkedList_getNext(jvars_ll)
                    while jv_node is not None:
                        jv = lib.LinkedList_getData(jv_node)
                        tag = lib.MmsJournalVariable_getTag(jv) or ""
                        raw_val = lib.MmsJournalVariable_getValue(jv)
                        variables.append((tag, _mms_value_to_python(lib, raw_val)))
                        jv_node = lib.LinkedList_getNext(jv_node)
                result.append(
                    LogEntry(
                        log_ref=log_ref,
                        entry_id=entry_id,
                        occurrence_time_ms=occ_ms,
                        variables=variables,
                    )
                )
                node = lib.LinkedList_getNext(node)
        finally:
            # Destroy the list and all contained MmsJournalEntry objects.
            lib.LinkedList_destroyDeep(entries_ll, lib.MmsJournalEntry_destroy)
        return result

    def query_log_by_time(
        self,
        log_ref: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[list[LogEntry], bool]:
        """Read journal entries in a UTC millisecond time range.

        Implements the IEC 61850-7-2 *QueryLogByTime* ACSI service via
        ``IedConnection_queryLogByTime``.

        Parameters
        ----------
        log_ref:
            Log object reference in the form ``"<LD>/<LN>$<LogName>"``,
            e.g. ``"LD0/LLN0$GeneralLog"``.
        start_ms:
            Start of the query range in milliseconds since the Unix epoch
            (inclusive).
        end_ms:
            End of the query range in milliseconds since the Unix epoch
            (inclusive).

        Returns
        -------
        tuple[list[LogEntry], bool]
            ``(entries, more_follows)`` where *more_follows* is ``True``
            when the IED has additional entries beyond the range that fit
            in a single MMS PDU.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK ``IedClientError``.
        """
        lib = self._lib
        # SWIG OUTPUT typemap pattern:
        # IedConnection_queryLogByTime(con, logRef, start, end)
        # → (LinkedList<MmsJournalEntry>, IedClientError, bool moreFollows)
        entries_ll, error, more_follows = lib.IedConnection_queryLogByTime(
            self._con,
            log_ref,
            start_ms,
            end_ms,
        )
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"QueryLogByTime({log_ref!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )
        return self._decode_journal_entries(lib, entries_ll, log_ref), bool(more_follows)

    def query_log_after(
        self,
        log_ref: str,
        entry_id: bytes,
        timestamp_ms: int,
    ) -> tuple[list[LogEntry], bool]:
        """Read journal entries after a known entry ID.

        Implements the IEC 61850-7-2 *QueryLogAfterEntry* ACSI service via
        ``IedConnection_queryLogAfter``.

        Parameters
        ----------
        log_ref:
            Log object reference, e.g. ``"LD0/LLN0$GeneralLog"``.
        entry_id:
            The opaque entry ID of the last-received entry, as raw bytes
            (``MMS_OCTET_STRING``).
        timestamp_ms:
            The occurrence-time of the last-received entry in milliseconds
            since the Unix epoch.

        Returns
        -------
        tuple[list[LogEntry], bool]
            ``(entries, more_follows)``.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK ``IedClientError``.
        """
        lib = self._lib
        # Build a MMS_OCTET_STRING MmsValue for the entry ID.
        eid_mv = lib.MmsValue_newOctetString(len(entry_id), len(entry_id))
        try:
            buf = lib.MmsValue_getOctetStringBuffer(eid_mv)
            for i, b in enumerate(entry_id):
                buf[i] = b
            entries_ll, error, more_follows = lib.IedConnection_queryLogAfter(
                self._con,
                log_ref,
                eid_mv,
                timestamp_ms,
            )
        finally:
            lib.MmsValue_delete(eid_mv)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"QueryLogAfter({log_ref!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )
        return self._decode_journal_entries(lib, entries_ll, log_ref), bool(more_follows)

    # ---------------------------------------------------------------------------
    # Setting-group services (P8.B.9)
    # ---------------------------------------------------------------------------
    #
    # pyiec61850 does not expose the high-level SGCB ACSI functions
    # (IedConnection_getSGCBValues / selectSettingGroup / etc.) in its SWIG
    # bindings.  We implement SGCB access via the generic
    # ``IedConnection_readObject`` / ``IedConnection_writeObject`` MMS calls
    # with the SG (FC=6) and SE (FC=7) functional constraints respectively.
    #
    # Standard SGCB attribute paths (IEC 61850-7-2 §8.7.3):
    #
    #   {sgcb_ref}.NumOfSGs  FC=SG  -- total number of setting groups
    #   {sgcb_ref}.ActSG     FC=SG  -- index of currently active SG (1-based)
    #   {sgcb_ref}.EditSG    FC=SE  -- index of setting group open for editing
    #   {sgcb_ref}.CnfEdit   FC=SE  -- boolean; write True to confirm edits
    #
    # ``IedConnection_writeObject`` is a void C function with an OUTPUT error
    # parameter; SWIG maps it so that the Python call returns the error code
    # directly: ``error = lib.IedConnection_writeObject(con, ref, fc, mv)``.

    def get_sgcb_values(self, sgcb_ref: str) -> SgcbValues:
        """Read ``NumOfSGs`` and ``ActSG`` from a Setting Group Control Block.

        Parameters
        ----------
        sgcb_ref:
            SGCB reference in dot notation, e.g. ``"simpleIO/LLN0.SGCB"``.

        Returns
        -------
        SgcbValues
            Snapshot of the two most-used SGCB attributes.

        Raises
        ------
        MmsDirectoryError
            If either attribute read returns a non-OK error code.
        """
        lib = self._lib

        num_mv, error = lib.IedConnection_readObject(self._con, f"{sgcb_ref}.NumOfSGs", FC_SG)
        if error != lib.IED_ERROR_OK:
            if num_mv is not None:
                lib.MmsValue_delete(num_mv)
            raise MmsDirectoryError(
                f"GetSGCBValues({sgcb_ref!r}).NumOfSGs failed: {_ied_error_name(error)}",
                error_code=error,
            )
        try:
            num_of_sgs = int(lib.MmsValue_toInt32(num_mv))
        finally:
            lib.MmsValue_delete(num_mv)

        act_mv, error = lib.IedConnection_readObject(self._con, f"{sgcb_ref}.ActSG", FC_SG)
        if error != lib.IED_ERROR_OK:
            if act_mv is not None:
                lib.MmsValue_delete(act_mv)
            raise MmsDirectoryError(
                f"GetSGCBValues({sgcb_ref!r}).ActSG failed: {_ied_error_name(error)}",
                error_code=error,
            )
        try:
            act_sg = int(lib.MmsValue_toInt32(act_mv))
        finally:
            lib.MmsValue_delete(act_mv)

        return SgcbValues(num_of_sgs=num_of_sgs, act_sg=act_sg)

    def select_active_sg(self, sgcb_ref: str, sg_num: int) -> None:
        """Activate a specific setting group (ACSI SelectActiveSG service).

        Writes ``ActSG`` with FC=SG.

        Parameters
        ----------
        sgcb_ref:
            SGCB reference in dot notation, e.g. ``"simpleIO/LLN0.SGCB"``.
        sg_num:
            1-based setting group index to activate.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK error code.
        """
        lib = self._lib
        mv = lib.MmsValue_newIntegerFromInt32(sg_num)
        try:
            error = lib.IedConnection_writeObject(self._con, f"{sgcb_ref}.ActSG", FC_SG, mv)
        finally:
            lib.MmsValue_delete(mv)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"SelectActiveSG({sgcb_ref!r}, {sg_num}) failed: {_ied_error_name(error)}",
                error_code=error,
            )

    def select_edit_sg(self, sgcb_ref: str, sg_num: int) -> None:
        """Open a setting group for editing (ACSI SelectEditSG service).

        Writes ``EditSG`` with FC=SE.

        Parameters
        ----------
        sgcb_ref:
            SGCB reference in dot notation, e.g. ``"simpleIO/LLN0.SGCB"``.
        sg_num:
            1-based setting group index to open for editing.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK error code.
        """
        lib = self._lib
        mv = lib.MmsValue_newIntegerFromInt32(sg_num)
        try:
            error = lib.IedConnection_writeObject(self._con, f"{sgcb_ref}.EditSG", FC_SE, mv)
        finally:
            lib.MmsValue_delete(mv)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"SelectEditSG({sgcb_ref!r}, {sg_num}) failed: {_ied_error_name(error)}",
                error_code=error,
            )

    def confirm_edit_sg(self, sgcb_ref: str) -> None:
        """Confirm editing of the currently open setting group (ACSI ConfirmEditSGValues).

        Writes ``CnfEdit = True`` with FC=SE.

        Parameters
        ----------
        sgcb_ref:
            SGCB reference in dot notation, e.g. ``"simpleIO/LLN0.SGCB"``.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK error code.
        """
        lib = self._lib
        mv = lib.MmsValue_newBoolean(True)
        try:
            error = lib.IedConnection_writeObject(self._con, f"{sgcb_ref}.CnfEdit", FC_SE, mv)
        finally:
            lib.MmsValue_delete(mv)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"ConfirmEditSG({sgcb_ref!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )

    # ---------------------------------------------------------------------------
    # File services (P8.B.8)
    # ---------------------------------------------------------------------------

    def list_files(self, directory: str | None = None) -> list[FileInfo]:
        """Return file-directory entries from the IED's virtual file store.

        Implements the IEC 61850-7-2 *GetFileAttributeValues* ACSI service via
        ``IedConnection_getFileDirectory``.

        Parameters
        ----------
        directory:
            Remote directory path (e.g. ``"COMTRADE"``), or ``None`` /
            empty string for the root directory.

        Returns
        -------
        list[FileInfo]
            One :class:`FileInfo` per file or sub-directory reported.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK ``IedClientError``.
        """
        lib = self._lib
        dir_arg: str = directory or ""
        # IedGetFileDirStr does all C work (IedConnection_getFileDirectory +
        # FileDirectoryEntry processing + cleanup) inside a single C function.
        # Python only sees a TAB-delimited string result and an int error code.
        raw, error = lib.IedGetFileDirStr(self._con, dir_arg)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"GetFileDirectory({dir_arg!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )
        text: str = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
        result: list[FileInfo] = []
        for line in text.splitlines():
            if not line:
                continue
            parts = line.split("\t", 2)
            if len(parts) == 3:
                result.append(
                    FileInfo(
                        name=parts[0],
                        size=int(parts[1]) if parts[1].isdigit() else 0,
                        last_modified_ms=int(parts[2]) if parts[2].isdigit() else 0,
                    )
                )
        return result

    def get_file(self, remote_path: str) -> bytes:
        """Download a file from the IED's virtual file store.

        Implements the IEC 61850-7-2 *GetFile* ACSI service via
        ``IedConnection_getFile``.  The file is received in chunks via an
        internal handler and assembled into a single :class:`bytes` object.

        Parameters
        ----------
        remote_path:
            Path of the file on the server (e.g. ``"COMTRADE/fault01.cfg"``).

        Returns
        -------
        bytes
            Complete file content.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK ``IedClientError``.
        """
        lib = self._lib
        # IedGetFileBytes is a C shim that wraps IedConnection_getFile with a
        # C callback — SWIG cannot pass a Python callable as IedClientGetFileHandler.
        data, error = lib.IedGetFileBytes(self._con, remote_path)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"GetFile({remote_path!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )
        return data

    def delete_file(self, remote_path: str) -> None:
        """Delete a file from the IED's virtual file store.

        Implements the IEC 61850-7-2 *DeleteFile* ACSI service via
        ``IedConnection_deleteFile``.

        Parameters
        ----------
        remote_path:
            Path of the file to delete on the server.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK ``IedClientError``.
        """
        lib = self._lib
        error = lib.IedConnection_deleteFile(self._con, remote_path)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"DeleteFile({remote_path!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )

    # Candidate SCL filenames in priority order (common IED vendor conventions).
    _SCL_CANDIDATES: tuple[str, ...] = (
        "conf.xml.gz",  # ABB REF/RET/REC relays, Siemens SIPROTEC
        "conf.xml",
        "ied.icd",
        "ied.cid",
        "ied.scd",
        "config.icd",
    )
    # SCL file extensions to look for when scanning the root directory.
    _SCL_EXTENSIONS: frozenset[str] = frozenset({"icd", "cid", "scd", "iid"})

    def fetch_scl(self) -> bytes:
        """Download the IED's SCL configuration and return raw XML bytes.

        Tries a fixed list of candidate filenames first, then scans the root
        file directory for files with ``.icd``, ``.cid``, ``.scd``, or
        ``.iid`` extensions.  GZip-compressed files (``*.gz``) are
        decompressed transparently.

        Returns
        -------
        bytes
            Decompressed raw SCL XML bytes (UTF-8 or UTF-16 encoded).

        Raises
        ------
        MmsDirectoryError
            If no SCL file is found on the IED or every download attempt fails.
        """
        import gzip as _gzip

        # --- Try fixed candidate filenames first ---
        for candidate in self._SCL_CANDIDATES:
            try:
                data = self.get_file(candidate)
            except MmsDirectoryError:
                continue
            if candidate.endswith(".gz"):
                try:
                    data = _gzip.decompress(data)
                except OSError:
                    continue  # not actually gzip; skip
            return data

        # --- Scan root directory for SCL-extension files ---
        try:
            entries = self.list_files("")
        except MmsDirectoryError as exc:
            raise MmsDirectoryError("No SCL file found on IED", error_code=0) from exc

        for entry in entries:
            name = entry.name
            suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if suffix not in self._SCL_EXTENSIONS:
                continue
            try:
                data = self.get_file(name)
                return data
            except MmsDirectoryError:
                continue

        raise MmsDirectoryError("No SCL file found on IED", error_code=0)

    # ---------------------------------------------------------------------------
    # Directory services (P8.B.3)
    # ---------------------------------------------------------------------------

    def get_server_directory(self) -> list[str]:
        """Return logical-device names reported by GetServerDirectory.

        Calls ``IedConnection_getServerDirectory`` with ``getFileNames=False``.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK ``IedClientError``.
        """
        lib = self._lib
        raw, error = lib.IedGetServerDirStr(self._con)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"GetServerDirectory failed: {_ied_error_name(error)}",
                error_code=error,
            )
        text: str = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
        return [name for name in text.splitlines() if name]

    def get_logical_device_directory(self, ld_name: str) -> list[str]:
        """Return logical-node names within *ld_name*.

        Calls ``IedConnection_getLogicalDeviceDirectory``.
        The returned names are bare LN class+instance (e.g. ``"MMXU1"``),
        not full functional references.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK ``IedClientError``.
        """
        lib = self._lib
        ll, error = lib.IedConnection_getLogicalDeviceDirectory(self._con, ld_name)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"GetLogicalDeviceDirectory({ld_name!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )
        return _ll_to_list(lib, ll)

    def get_logical_node_directory(self, ln_ref: str, acsi_class: int) -> list[str]:
        """Return names of ACSI objects of *acsi_class* within *ln_ref*.

        *ln_ref* is a full functional reference such as ``"LD0/MMXU1"``.
        *acsi_class* is one of the ``ACSI_CLASS_*`` constants in this module
        (e.g. :data:`ACSI_CLASS_DATA_OBJECT`).

        Calls ``IedConnection_getLogicalNodeDirectory``.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK ``IedClientError``.
        """
        lib = self._lib
        ll, error = lib.IedConnection_getLogicalNodeDirectory(self._con, ln_ref, acsi_class)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"GetLogicalNodeDirectory({ln_ref!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )
        return _ll_to_list(lib, ll)

    def get_data_directory_fc(self, da_ref: str) -> list[str]:
        """Return sub-attribute names (with ``[FC]`` suffix) of a DO or DA.

        *da_ref* is a functional reference such as ``"LD0/MMXU1.A"``.
        Returned strings look like ``"phsA[MX]"`` or ``"stVal[ST]"``.
        A non-empty result means the item is a structured DA; an empty
        result means it is a leaf attribute.

        Calls ``IedConnection_getDataDirectoryFC``.

        Raises
        ------
        MmsDirectoryError
            If the IED returns a non-OK ``IedClientError``.
        """
        lib = self._lib
        ll, error = lib.IedConnection_getDataDirectoryFC(self._con, da_ref)
        if error != lib.IED_ERROR_OK:
            raise MmsDirectoryError(
                f"GetDataDirectoryFC({da_ref!r}) failed: {_ied_error_name(error)}",
                error_code=error,
            )
        return _ll_to_list(lib, ll)
