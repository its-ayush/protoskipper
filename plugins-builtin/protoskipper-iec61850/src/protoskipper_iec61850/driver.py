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

import ipaddress
import logging
import re
import socket
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    FC_BL,
    FC_CF,
    FC_CO,
    FC_DC,
    FC_EX,
    FC_MX,
    FC_OR,
    FC_SE,
    FC_SG,
    FC_SP,
    FC_SR,
    FC_ST,
    FC_SV,
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
    SgcbValues,
)

_logger = logging.getLogger(__name__)

__all__ = ["Iec61850MmsDriver", "Iec61850MmsSession"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MMS_PORT: int = 102
_PROBE_WORKERS: int = 64
_PROBE_TIMEOUT_S: float = 1.5


def _expand_mms_target(target: str) -> list[tuple[str, int]]:
    """Parse a probe target into a list of (host, port) pairs.

    Accepts::

        10.0.0.5
        10.0.0.5:102
        192.168.1.0/24
        192.168.1.0/24:4096
        host1,host2:102,10.0.0.0/24
    """
    hosts: list[tuple[str, int]] = []
    for spec in target.split(","):
        spec = spec.strip()
        if not spec:
            continue
        port = DEFAULT_MMS_PORT
        # Detect CIDR (contains / not followed by digits only — must contain a dot too)
        if "/" in spec and not spec.startswith("/"):
            # May be CIDR like 10.0.0.0/24 optionally with :port suffix before the CIDR slash
            # Separate a trailing :port that appears *after* the CIDR notation
            # e.g. "10.0.0.0/24:4096" is ambiguous; treat trailing :port as port only if it
            # comes after the prefix-length digit group.
            cidr_port_m = re.match(r"^(\S+/\d+):(\d+)$", spec)
            if cidr_port_m:
                spec, port_str = cidr_port_m.group(1), cidr_port_m.group(2)
                port = int(port_str)
            try:
                net = ipaddress.ip_network(spec, strict=False)
            except ValueError:
                _logger.debug("skipping unparseable CIDR %r", spec)
                continue
            for ip in net.hosts() if net.num_addresses > 1 else [net.network_address]:
                hosts.append((str(ip), port))
        else:
            # host or host:port
            host_port_m = re.match(r"^([^:]+):(\d+)$", spec)
            if host_port_m:
                hosts.append((host_port_m.group(1), int(host_port_m.group(2))))
            else:
                hosts.append((spec, port))
    return hosts


def _probe_mms_port(host: str, port: int) -> DeviceRef | None:
    """TCP-connect to host:port; return a :class:`DeviceRef` on success."""
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_S):
            pass
        return DeviceRef(
            protocol="iec61850.mms",
            address=f"{host}:{port}",
            label=f"IEC 61850 IED @ {host}:{port}",
            metadata={"port": port},
        )
    except OSError:
        return None


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

# Map FC string names to integer FC codes (all IEC 61850-7-2 FCs).
_FC_NAME_TO_INT: dict[str, int] = {
    "ST": FC_ST,  # Status
    "MX": FC_MX,  # Measured value
    "SP": FC_SP,  # Setting (persistent)
    "SV": FC_SV,  # Substitution value
    "CF": FC_CF,  # Configuration
    "DC": FC_DC,  # Description
    "SG": FC_SG,  # Setting group (active)
    "SE": FC_SE,  # Setting group (editable)
    "SR": FC_SR,  # Service response
    "OR": FC_OR,  # Operate received
    "BL": FC_BL,  # Blocking
    "EX": FC_EX,  # Extended definition
    "CO": FC_CO,  # Control output
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


def _strip_to_do(da_ref: str) -> str:
    """Strip a DA attribute path down to the enclosing DO level.

    Many IEDs do not support individual DA-level reads but do support DO-level
    reads.  This helper converts ``"LD0/MMXU1.A.phsA.cVal.mag.f"`` to
    ``"LD0/MMXU1.A"`` (the DO), which the driver can then fall back to.

    Returns *da_ref* unchanged if it is already at the DO level (i.e. there
    is only one ``.`` after the ``/``).
    """
    try:
        slash = da_ref.index("/")
        after_slash = da_ref[slash + 1 :]  # e.g. "MMXU1.A.phsA"
        first_dot = after_slash.index(".")  # between LN prefix and DO name
        rest = after_slash[first_dot + 1 :]  # e.g. "A.phsA"
        second_dot_in_rest = rest.find(".")  # -1 when already at DO level
        if second_dot_in_rest < 0:
            return da_ref  # already at DO level — e.g. "LD0/MMXU1.A"
        # Keep through the DO name only.
        do_end = slash + 1 + first_dot + 1 + second_dot_in_rest
        return da_ref[:do_end]
    except ValueError:
        return da_ref


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
        """Probe *target* for IEC 61850 IEDs by scanning TCP port 102.

        *target* accepts:

        * ``host`` or ``host:port`` — single address.
        * ``192.168.1.0/24`` or ``192.168.1.0/24:4096`` — CIDR subnet.
        * Comma-separated combination of the above.

        Discovery is a TCP-connect probe only (no MMS Initiate handshake).
        It is fast and non-intrusive but cannot distinguish a real IED from
        any other service that happens to accept connections on port 102.
        """
        hosts = _expand_mms_target(target)
        if not hosts:
            return
        max_w = min(_PROBE_WORKERS, max(1, len(hosts)))
        with ThreadPoolExecutor(max_workers=max_w) as ex:
            futs = {ex.submit(_probe_mms_port, h, p): (h, p) for h, p in hosts}
            for fut in as_completed(futs):
                ref = fut.result()
                if ref is not None:
                    yield ref

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
        """Walk the IED data model and yield one ObjectRef per leaf data attribute.

        **Primary path — SCL-based enumeration (P8.B.10):**
        Downloads the IED's SCL configuration (``conf.xml.gz``, ``*.icd``, …),
        parses the DataTypeTemplates section, and yields fully-typed leaf data
        attributes with rich metadata.  This path is faster (one file transfer
        instead of hundreds of GetDirectory round-trips) and produces better
        labels.

        **Fallback — MMS directory walk:**
        If the SCL file cannot be fetched or parsed, falls back to the
        original GetDirectory walk.  Each data object is yielded as one
        ObjectRef with ``data_type="do"``; actual types are resolved on read.

        Errors on individual directory queries during the fallback walk are
        logged at WARNING and skipped.

        Yields nothing if the session has no active client.
        """
        if self._client is None:
            return

        # --- Try SCL-based enumeration first ---
        scl_refs = self.enumerate_objects_from_scl()
        if scl_refs is not None:
            yield from scl_refs
            return

        # --- Fallback: MMS directory walk ---
        try:
            ld_names = self._client.get_server_directory()
        except MmsDirectoryError as exc:
            _logger.warning("GetServerDirectory failed: %s", exc)
            return

        total = 0
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
                    total += 1
                    yield ObjectRef(
                        device=self.device,
                        object_id=do_ref,
                        data_type="do",
                        access=Access.READ_ONLY,
                        label=do_ref,
                    )

        self.safety.record_event(
            "iec61850_browse",
            subevent="enumerate_objects",
            target=self.device.address,
            object_count=total,
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
            # DA-level read: single call, no q/t extraction.
            try:
                decoded = self._client.read_object(clean_id, explicit_fc)
            except MmsDirectoryError:
                # Individual DA-level read was rejected (DATA_ACCESS_ERROR or
                # other IED-side error).  Many IEDs support only DO-level reads;
                # strip back to LDInst/LNRef.DOName and retry as a DO read.
                do_ref = _strip_to_do(clean_id)
                if do_ref != clean_id:
                    try:
                        do_dec = self._client.read_do_with_meta(do_ref, explicit_fc)
                        return ReadResult(
                            object_ref=ref,
                            value=do_dec.value,
                            quality=_quality_from_decoded(do_dec),
                            timestamp=_ts_from_ms(do_dec.timestamp_ms),
                        )
                    except MmsDirectoryError:
                        pass
                # Both DA and DO reads failed; return empty without error text
                # so the cell shows blank+BAD rather than a noisy ERR message.
                return ReadResult(
                    object_ref=ref,
                    value=None,
                    quality=Quality.BAD,
                    timestamp=datetime.now(tz=timezone.utc),
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

    def read_many(self, refs: list[ObjectRef]) -> list[ReadResult]:
        """Read a list of object references one at a time.

        Overrides the base fall-through so we can abort the loop early when
        the MMS connection drops rather than firing hundreds of calls into
        libiec61850 against a closed socket (which can cause a SIGSEGV in the
        C layer).
        """
        results: list[ReadResult] = []
        for ref in refs:
            if self._client is None or not self._client.is_connected:
                results.append(
                    ReadResult(
                        object_ref=ref,
                        value=None,
                        quality=Quality.BAD,
                        timestamp=datetime.now(tz=timezone.utc),
                        error="Connection lost; aborting bulk read",
                    )
                )
                continue
            results.append(self.read(ref))
        return results

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
        rcb = self._client.enable_report(rcb_ref, is_buffered, trg_ops, intg_pd, on_report)
        self.safety.record_event(
            "iec61850_report_subscribe",
            subevent="subscribe_report",
            target=self.device.address,
            rcb_ref=rcb_ref,
            buffered=is_buffered,
            trg_ops=trg_ops,
            intg_pd_ms=intg_pd,
        )
        return rcb

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
            self.safety.record_event(
                "iec61850_report_unsubscribe",
                subevent="unsubscribe_report",
                target=self.device.address,
                rcb_ref=rcb_ref,
                buffered=is_buffered,
            )

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
        entries, more = self._client.query_log_by_time(log_ref, start_ms, end_ms)
        self.safety.record_event(
            "iec61850_log_query",
            subevent="query_log_by_time",
            target=self.device.address,
            log_ref=log_ref,
            start_ms=start_ms,
            end_ms=end_ms,
            entry_count=len(entries),
            more_follows=more,
        )
        return entries, more

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
        entries, more = self._client.query_log_after(log_ref, entry_id, timestamp_ms)
        self.safety.record_event(
            "iec61850_log_query",
            subevent="query_log_after",
            target=self.device.address,
            log_ref=log_ref,
            after_timestamp_ms=timestamp_ms,
            entry_count=len(entries),
            more_follows=more,
        )
        return entries, more

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
        infos = self._client.list_files(directory)
        self.safety.record_event(
            "iec61850_file_list",
            subevent="list_files",
            target=self.device.address,
            directory=directory or "/",
            file_count=len(infos),
        )
        return infos

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
        data = self._client.get_file(remote_path)
        self.safety.record_event(
            "iec61850_file_get",
            subevent="get_file",
            target=self.device.address,
            remote_path=remote_path,
            size_bytes=len(data),
        )
        return data

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
        self.safety.record_event(
            "iec61850_file_delete",
            subevent="delete_file",
            target=self.device.address,
            remote_path=remote_path,
        )

    # ------------------------------------------------------------------
    # Setting-group services (P8.B.9)
    # ------------------------------------------------------------------

    def get_sgcb_values(self, sgcb_ref: str) -> SgcbValues:
        """Read NumOfSGs and ActSG from a Setting Group Control Block.

        Delegates to :meth:`MmsClient.get_sgcb_values`.

        Raises
        ------
        MmsDirectoryError
            If the session has no active client, or if the IED returns an
            error.
        """
        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)
        values = self._client.get_sgcb_values(sgcb_ref)
        self.safety.record_event(
            "iec61850_sg_read",
            subevent="get_sgcb_values",
            target=self.device.address,
            sgcb_ref=sgcb_ref,
            num_of_sgs=values.num_of_sgs,
            act_sg=values.act_sg,
        )
        return values

    def select_active_sg(self, sgcb_ref: str, sg_num: int) -> None:
        """Activate a specific setting group.

        Delegates to :meth:`MmsClient.select_active_sg`.

        Raises
        ------
        MmsDirectoryError
            If the session has no active client, or if the IED returns an
            error.
        """
        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)
        self._client.select_active_sg(sgcb_ref, sg_num)
        self.safety.record_event(
            "iec61850_sg_select_active",
            subevent="select_active_sg",
            target=self.device.address,
            sgcb_ref=sgcb_ref,
            sg_num=sg_num,
        )

    def select_edit_sg(self, sgcb_ref: str, sg_num: int) -> None:
        """Open a setting group for editing.

        Delegates to :meth:`MmsClient.select_edit_sg`.

        Raises
        ------
        MmsDirectoryError
            If the session has no active client, or if the IED returns an
            error.
        """
        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)
        self._client.select_edit_sg(sgcb_ref, sg_num)
        self.safety.record_event(
            "iec61850_sg_select_edit",
            subevent="select_edit_sg",
            target=self.device.address,
            sgcb_ref=sgcb_ref,
            sg_num=sg_num,
        )

    def confirm_edit_sg(self, sgcb_ref: str) -> None:
        """Confirm edits to the currently open setting group.

        Delegates to :meth:`MmsClient.confirm_edit_sg`.

        Raises
        ------
        MmsDirectoryError
            If the session has no active client, or if the IED returns an
            error.
        """
        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)
        self._client.confirm_edit_sg(sgcb_ref)
        self.safety.record_event(
            "iec61850_sg_confirm_edit",
            subevent="confirm_edit_sg",
            target=self.device.address,
            sgcb_ref=sgcb_ref,
        )

    def close(self) -> None:
        """Send MMS Close and release all transport resources."""
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # SCL tag model (P8.B.10)
    # ------------------------------------------------------------------

    def get_tag_model(self) -> list[ObjectRef]:
        """Fetch the IED's SCL configuration and expand it into a flat tag list.

        Downloads the SCL file (``conf.xml.gz``, ``*.icd``, ``*.cid``, …) from
        the IED, parses the DataTypeTemplates section, and returns one
        :class:`~protoskipper.core.driver.ObjectRef` per leaf data attribute.

        Each ``ObjectRef`` is populated with:

        * ``object_id`` — ``"LD/LN.DO.DA[FC]"`` (suitable for :meth:`read`)
        * ``data_type`` — IEC 61850 basic type (``"FLOAT32"``, ``"BOOLEAN"``, …)
        * ``access`` — ``READ_WRITE`` for settable FCs, ``READ_ONLY`` otherwise
        * ``label`` — human-readable ``"LD/LN.DO.DA"``
        * ``metadata`` — rich tree info (``ld_inst``, ``ln_class``, ``ln_ref``,
          ``ln_inst``, ``do_name``, ``da_path``, ``fc``, ``cdc``)

        Raises
        ------
        MmsDirectoryError
            If the client cannot fetch the SCL file.
        """
        from protoskipper_iec61850.scl.dtt import expand_tags

        if self._client is None:
            raise MmsDirectoryError("Session has no active MMS client", error_code=1)

        xml_bytes = self._client.fetch_scl()
        tags = expand_tags(xml_bytes)

        self.safety.record_event(
            "iec61850_scl_fetch",
            subevent="get_tag_model",
            target=self.device.address,
            tag_count=len(tags),
        )

        refs: list[ObjectRef] = []
        for tag in tags:
            refs.append(
                ObjectRef(
                    device=self.device,
                    object_id=tag.object_id,
                    data_type=tag.basic_type.lower(),
                    access=Access.READ_WRITE if tag.writable else Access.READ_ONLY,
                    label=tag.label,
                    metadata={
                        "ld_inst": tag.ld_inst,
                        "ln_ref": tag.ln_ref,
                        "ln_class": tag.ln_class,
                        "ln_prefix": tag.ln_prefix,
                        "ln_inst": tag.ln_inst,
                        "do_name": tag.do_name,
                        "da_path": tag.da_path,
                        "fc": tag.fc,
                        "cdc": tag.cdc,
                        "desc": tag.desc,
                    },
                )
            )
        return refs

    def enumerate_objects_from_scl(self) -> list[ObjectRef] | None:
        """Try to build the object list from the IED's SCL file.

        Returns ``None`` if SCL is unavailable or cannot be parsed, so the
        caller can fall back to the MMS directory walk.
        """
        try:
            return self.get_tag_model()
        except Exception as exc:
            _logger.debug("SCL tag model unavailable: %s; falling back to MMS walk", exc)
            return None
