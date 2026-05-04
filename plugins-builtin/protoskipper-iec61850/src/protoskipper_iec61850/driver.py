# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""IEC 61850 MMS driver — :class:`ProtocolDriver` integration.

Address format
--------------

``host[:port]``  e.g. ``192.168.1.10`` or ``192.168.1.10:102``

Optional query parameters (appended with ``?``):

``ap_title=1,3,9999,33``  — calling AP-Title OID (default ``1,3,9999,33``)
``called_ap_title=1,3,9999,33``  — called AP-Title
``edition=2.1``  — enforce a specific SCL edition (default ``auto``)

Example::

    192.168.1.10:102?ap_title=1,3,9999,33&edition=2.1

Implementation notes
--------------------

This module is a scaffold (P8.A.1).  Every method raises
:class:`NotImplementedError` with a note pointing to the relevant
P8.B.x sub-task that implements it.  The goal is:

* The driver registers cleanly via the entry-point group.
* ``protoskipper list-protocols`` shows ``iec61850.mms``.
* ``protoskipper-gui`` does not crash when the plugin is installed.
* Tests can verify the contract without requiring a live IED.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any, ClassVar

from protoskipper.core.driver import (
    Access,
    DeviceRef,
    DriverSession,
    ObjectRef,
    ProtocolDriver,
    Quality,
    ReadResult,
    SafetyContext,
    WriteIntent,
    WriteResult,
)
from protoskipper.core.errors import ConnectionFailure, EncodingError

from protoskipper_iec61850._mms_client import (
    ACSI_CLASS_DATA_OBJECT,
    FC_MX,
    FC_SP,
    FC_ST,
    TRG_OPS_DATA_CHANGE,
    TRG_OPS_QUALITY_CHANGE,
    FileInfo,
    LogEntry,
    MmsClient,
    MmsConnectError,
    MmsDecodedValue,
    MmsDirectoryError,
    RcbValues,
    ReportEntry,
)

_logger = logging.getLogger(__name__)

__all__ = ["Iec61850MmsDriver", "Iec61850MmsSession"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MMS_PORT: int = 102

_ADDRESS_RE = re.compile(
    r"^(?P<host>[^:?]+)"
    r"(?::(?P<port>\d+))?"
    r"(?:\?(?P<query>.*))?$"
)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _host_port_from_address(address: str) -> tuple[str, int]:
    """Extract ``(host, port)`` from a normalised :class:`DeviceRef` address.

    The address has already been through :meth:`Iec61850MmsDriver.parse_address`
    so it is guaranteed to be ``host:port[?query]``.
    """
    base = address.split("?")[0]
    host, _, port_str = base.rpartition(":")
    return host, int(port_str)


# FC suffix pattern: "[MX]", "[ST]", "[SP]", etc.
_FC_SUFFIX_RE = re.compile(r"\[(?P<fc>[A-Z]{2,3})\]$")

# Map FC string names to integer FC codes
_FC_NAME_TO_INT: dict[str, int] = {
    "ST": FC_ST,
    "MX": FC_MX,
    "SP": FC_SP,
}


def _parse_object_id_fc(object_id: str) -> tuple[str, int | None]:
    """Split a ``[FC]`` suffix from *object_id*.

    Returns ``(clean_ref, fc_int)`` where *fc_int* is ``None`` when no
    recognised FC suffix is present (DO-level ref from enumerate_objects).
    """
    m = _FC_SUFFIX_RE.search(object_id)
    if m:
        clean = object_id[: m.start()]
        fc = _FC_NAME_TO_INT.get(m.group("fc"))
        return clean, fc
    return object_id, None


def _quality_from_decoded(decoded: MmsDecodedValue) -> Quality:
    """Map an :class:`MmsDecodedValue` quality fields to :class:`Quality`."""
    if decoded.is_substituted:
        return Quality.SIMULATED
    validity = decoded.quality_validity
    if validity == 0:
        return Quality.GOOD
    if validity == 3:  # questionable
        return Quality.UNCERTAIN
    return Quality.BAD  # invalid (1) or reserved (2)


def _ts_from_ms(ts_ms: int) -> datetime:
    """Convert milliseconds since Unix epoch to an aware UTC datetime."""
    if ts_ms == 0:
        return datetime.now(tz=timezone.utc)
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class Iec61850MmsDriver(ProtocolDriver):
    """ProtoSkipper driver for IEC 61850 MMS (ISO 9506 over TCP/102)."""

    PROTOCOL_ID: ClassVar[str] = "iec61850.mms"
    DISPLAY_NAME: ClassVar[str] = "IEC 61850 (MMS)"
    DESCRIPTION: ClassVar[str] = (
        "IEC 61850 MMS over TCP/102 — browse data model, read/write "
        "data attributes, subscribe to reports, GOOSE/SV monitoring."
    )

    def parse_address(self, address: str) -> DeviceRef:
        """Parse ``host[:port][?query]`` into a :class:`DeviceRef`.

        Raises
        ------
        EncodingError
            If ``address`` does not match the expected format.
        """
        m = _ADDRESS_RE.match(address.strip())
        if m is None:
            raise EncodingError(f"Invalid IEC 61850 address: {address!r}")
        host = m.group("host")
        port = int(m.group("port")) if m.group("port") else DEFAULT_MMS_PORT
        if not (1 <= port <= 65535):
            raise EncodingError(f"Port {port} out of range 1-65535")
        normalised = f"{host}:{port}"
        if m.group("query"):
            normalised += f"?{m.group('query')}"
        return DeviceRef(
            protocol=self.PROTOCOL_ID,
            address=normalised,
            label=host,
        )

    def discover(self, target: str) -> Iterator[DeviceRef]:
        """Probe ``target`` for IEC 61850 IEDs.

        .. note::
            Not yet implemented (P8.B.2).  Yields nothing silently.
        """
        return
        yield  # make this a generator even before P8.B.2 lands

    def connect(
        self,
        device: DeviceRef,
        safety: SafetyContext,
    ) -> Iec61850MmsSession:
        """Open an MMS Initiate exchange and return a live session.

        Raises
        ------
        ImportError
            If ``pyiec61850`` is not installed (see build instructions in
            ``docs/internal/IEC61850_MMS_LIBRARY.md``).
        ConnectionFailure
            If the MMS Initiate is rejected or times out.
        """
        host, port = _host_port_from_address(device.address)
        client = MmsClient(host, port)
        try:
            client.connect()
        except MmsConnectError as exc:
            raise ConnectionFailure(str(exc)) from exc
        return Iec61850MmsSession(device=device, safety=safety, client=client)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class Iec61850MmsSession(DriverSession):
    """An open IEC 61850 MMS connection to a single IED."""

    device: DeviceRef
    safety: SafetyContext

    def __init__(
        self,
        device: DeviceRef,
        safety: SafetyContext,
        client: MmsClient | None = None,
    ) -> None:
        self.device = device
        self.safety = safety
        self._client = client

    @property
    def negotiated_pdu_size(self) -> int:
        """Maximum PDU size negotiated in MMS Initiate (0 if unavailable)."""
        return self._client.negotiated_pdu_size if self._client else 0

    @property
    def peer_implementation(self) -> str:
        """Peer implementation string from MMS Initiate response."""
        return self._client.peer_implementation if self._client else ""

    def enumerate_objects(self) -> Iterator[ObjectRef]:
        """Walk the IED data model via MMS GetDirectory services.

        Traversal order: server -> logical device -> logical node ->
        data object.  Each data object is yielded as one
        :class:`~protoskipper.core.driver.ObjectRef`.

        Actual data types and writability are resolved during reads
        (P8.B.4).  ``data_type`` is ``"do"`` for every yielded ref.

        Errors on individual directory queries are logged at WARNING and
        skipped; they do not abort the whole enumeration.

        Yields nothing if the session has no active client.
        """
        if self._client is None:
            return

        try:
            ld_names = self._client.get_server_directory()
        except MmsDirectoryError as exc:
            _logger.warning("GetServerDirectory failed: %s", exc)
            return

        for ld_name in ld_names:
            try:
                ln_names = self._client.get_logical_device_directory(ld_name)
            except MmsDirectoryError as exc:
                _logger.warning("GetLogicalDeviceDirectory(%r) failed: %s", ld_name, exc)
                continue

            for ln_name in ln_names:
                ln_ref = f"{ld_name}/{ln_name}"
                try:
                    do_names = self._client.get_logical_node_directory(
                        ln_ref, ACSI_CLASS_DATA_OBJECT
                    )
                except MmsDirectoryError as exc:
                    _logger.warning("GetLogicalNodeDirectory(%r) failed: %s", ln_ref, exc)
                    continue

                for do_name in do_names:
                    do_ref = f"{ln_ref}.{do_name}"
                    yield ObjectRef(
                        device=self.device,
                        object_id=do_ref,
                        data_type="do",
                        access=Access.READ_ONLY,
                        label=do_ref,
                    )

    def read(self, ref: ObjectRef) -> ReadResult:
        """Read a single data object or data attribute.

        The ``object_id`` on *ref* may be:

        * A **data object** reference (from :meth:`enumerate_objects`) such as
          ``"LD0/MMXU1.A"`` -- no ``[FC]`` suffix.  The method tries FC_MX,
          FC_ST, FC_SP in turn until one succeeds, then also reads the ``.q``
          and ``.t`` sub-attributes for quality and timestamp.
        * A **data attribute** reference with an explicit FC suffix such as
          ``"LD0/LLN0.Mod.stVal[ST]"``.  The FC is used directly; quality
          defaults to :attr:`~protoskipper.core.driver.Quality.UNKNOWN` and
          timestamp to the current wall time (DA-level reads without the full
          DO context cannot reliably locate the sibling ``q`` / ``t``).

        Returns a :class:`~protoskipper.core.driver.ReadResult` whose
        ``quality`` is :attr:`~protoskipper.core.driver.Quality.BAD` and
        ``error`` is set when the read fails; it never raises.
        """
        if self._client is None:
            return ReadResult(
                object_ref=ref,
                value=None,
                quality=Quality.BAD,
                timestamp=datetime.now(tz=timezone.utc),
                error="Session has no active MMS client",
            )

        clean_id, explicit_fc = _parse_object_id_fc(ref.object_id)

        if explicit_fc is not None:
            # DA-level read: single call, no q/t extraction
            try:
                decoded = self._client.read_object(clean_id, explicit_fc)
            except MmsDirectoryError as exc:
                return ReadResult(
                    object_ref=ref,
                    value=None,
                    quality=Quality.BAD,
                    timestamp=datetime.now(tz=timezone.utc),
                    error=str(exc),
                )
            return ReadResult(
                object_ref=ref,
                value=decoded.value,
                quality=Quality.UNKNOWN,
                timestamp=datetime.now(tz=timezone.utc),
            )

        # DO-level read: infer FC by trying MX -> ST -> SP
        last_error: str = "all FC attempts failed"
        for try_fc in (FC_MX, FC_ST, FC_SP):
            try:
                decoded = self._client.read_do_with_meta(clean_id, try_fc)
                return ReadResult(
                    object_ref=ref,
                    value=decoded.value,
                    quality=_quality_from_decoded(decoded),
                    timestamp=_ts_from_ms(decoded.timestamp_ms),
                )
            except MmsDirectoryError as exc:
                last_error = str(exc)
                _logger.debug("read(%r) FC=%d failed: %s; trying next FC", clean_id, try_fc, exc)

        return ReadResult(
            object_ref=ref,
            value=None,
            quality=Quality.BAD,
            timestamp=datetime.now(tz=timezone.utc),
            error=last_error,
        )

    def prepare_write(self, ref: ObjectRef, value: Any) -> WriteIntent:
        """Encode a control write intent (no I/O).

        Parameters
        ----------
        ref:
            Reference to a controllable data object, e.g.
            ``LD0/XCBR1.Pos``.
        value:
            Control value (ctlVal).  Must be ``bool``, ``int``, or ``float``.

        Raises
        ------
        EncodingError
            If *value* is not a supported ctlVal type.
        """
        if not isinstance(value, (bool, int, float)):
            raise EncodingError(
                f"IEC 61850 ctlVal must be bool, int, or float; got {type(value).__name__!r}"
            )
        encoded = repr(value).encode()
        return WriteIntent(
            object_ref=ref,
            requested_value=value,
            encoded_bytes=encoded,
            description=f"Control {ref.object_id} ctlVal={value!r}",
        )

    def commit_write(self, intent: WriteIntent) -> WriteResult:
        """Transmit a previously-prepared control write.

        Calls :meth:`~protoskipper.core.driver.SafetyContext.require_write_authorization`
        before transmission.  Denied writes return a failure
        :class:`~protoskipper.core.driver.WriteResult` without calling
        :meth:`~protoskipper.core.driver.SafetyContext.record_write_outcome`.

        Always calls
        :meth:`~protoskipper.core.driver.SafetyContext.record_write_outcome`
        when a transmission was attempted (regardless of success/failure).

        Never raises.
        """
        now = datetime.now(tz=timezone.utc)

        if not self.safety.require_write_authorization(intent):
            return WriteResult(
                intent=intent,
                success=False,
                timestamp=now,
                error="Write denied by operator",
            )

        if self._client is None:
            result = WriteResult(
                intent=intent,
                success=False,
                timestamp=now,
                error="Session has no active MMS client",
            )
            self.safety.record_write_outcome(result)
            return result

        do_ref, _ = _parse_object_id_fc(intent.object_ref.object_id)
        try:
            ctrl_result = self._client.write_control(do_ref, intent.requested_value)
        except EncodingError as exc:
            result = WriteResult(
                intent=intent,
                success=False,
                timestamp=datetime.now(tz=timezone.utc),
                error=str(exc),
            )
            self.safety.record_write_outcome(result)
            return result

        metadata: dict[str, Any] = {
            "control_model": ctrl_result.control_model,
            "add_cause": ctrl_result.add_cause,
            "add_cause_name": ctrl_result.add_cause_name,
        }
        result = WriteResult(
            intent=intent,
            success=ctrl_result.success,
            timestamp=datetime.now(tz=timezone.utc),
            error=ctrl_result.error_str if not ctrl_result.success else None,
            metadata=metadata,
        )
        self.safety.record_write_outcome(result)
        return result

    def subscribe_report(
        self,
        rcb_ref: str,
        is_buffered: bool,
        on_report: Callable[[ReportEntry], None],
        trg_ops: int = TRG_OPS_DATA_CHANGE | TRG_OPS_QUALITY_CHANGE,
        intg_pd: int = 0,
    ) -> RcbValues:
        """Enable reporting on a Report Control Block and install a callback.

        Parameters
        ----------
        rcb_ref:
            Full RCB reference, e.g. ``"LD0/LLN0.BR.rcbMeas01"``.
        is_buffered:
            ``True`` for BRCB, ``False`` for URCB.
        on_report:
            Callable invoked on the session thread for each incoming report.
        trg_ops:
            ``TriggerOptions`` bitmask (combination of ``TRG_OPS_*``
            constants from ``protoskipper_iec61850._mms_client``).
        intg_pd:
            Integrity period in milliseconds.  0 = disabled.

        Returns
        -------
        RcbValues
            Snapshot of the RCB attributes after enabling.

        Raises
        ------
        MmsDirectoryError
            If the session has no active client, or if the IED returns an
            error.
        """
        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)
        return self._client.enable_report(rcb_ref, is_buffered, trg_ops, intg_pd, on_report)

    def unsubscribe_report(self, rcb_ref: str, is_buffered: bool) -> None:
        """Disable reporting on a Report Control Block and remove the handler.

        Safe to call when there is no active client or when reporting was
        never enabled for *rcb_ref*.

        Parameters
        ----------
        rcb_ref:
            Full RCB reference.
        is_buffered:
            ``True`` for BRCB, ``False`` for URCB.
        """
        if self._client is not None:
            self._client.disable_report(rcb_ref, is_buffered)

    def query_log_by_time(
        self,
        log_ref: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[list[LogEntry], bool]:
        """Read journal entries from an LCB within a UTC millisecond time range.

        Parameters
        ----------
        log_ref:
            Log object reference, e.g. ``"LD0/LLN0$GeneralLog"``.
        start_ms:
            Start of the query range in milliseconds since the Unix epoch
            (inclusive).
        end_ms:
            End of the query range in milliseconds since the Unix epoch
            (inclusive).

        Returns
        -------
        tuple[list[LogEntry], bool]
            ``(entries, more_follows)``.

        Raises
        ------
        MmsDirectoryError
            If the session has no active client, or if the IED returns an
            error.
        """
        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)
        return self._client.query_log_by_time(log_ref, start_ms, end_ms)

    def query_log_after(
        self,
        log_ref: str,
        entry_id: bytes,
        timestamp_ms: int,
    ) -> tuple[list[LogEntry], bool]:
        """Read journal entries after a known entry ID (cursor-based paging).

        Parameters
        ----------
        log_ref:
            Log object reference, e.g. ``"LD0/LLN0$GeneralLog"``.
        entry_id:
            The opaque entry ID of the last-received entry (raw bytes).
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
            If the session has no active client, or if the IED returns an
            error.
        """
        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)
        return self._client.query_log_after(log_ref, entry_id, timestamp_ms)

    # ------------------------------------------------------------------
    # File services (P8.B.8)
    # ------------------------------------------------------------------

    def list_files(self, directory: str | None = None) -> list[FileInfo]:
        """Return file directory entries from the IED.

        Delegates to :meth:`MmsClient.list_files`.

        Raises
        ------
        MmsDirectoryError
            If the session has no active client, or if the IED returns an
            error.
        """
        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)
        return self._client.list_files(directory)

    def get_file(self, remote_path: str) -> bytes:
        """Download a file from the IED.

        Delegates to :meth:`MmsClient.get_file`.

        Raises
        ------
        MmsDirectoryError
            If the session has no active client, or if the IED returns an
            error.
        """
        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)
        return self._client.get_file(remote_path)

    def delete_file(self, remote_path: str) -> None:
        """Delete a file on the IED.

        Delegates to :meth:`MmsClient.delete_file`.

        Raises
        ------
        MmsDirectoryError
            If the session has no active client, or if the IED returns an
            error.
        """
        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)
        self._client.delete_file(remote_path)

    def close(self) -> None:
        """Send MMS Close and release all transport resources."""
        if self._client is not None:
            self._client.close()
            self._client = None
