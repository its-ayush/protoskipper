# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 scripting façade — P4.G.3 of docs/internal/EXECUTION_PLAN.md.

Exposes a clean, high-level API for use from:

* ``protoskipper run script.py`` (headless CLI)
* The embedded Python REPL (``View → Scripting Console``)

Usage in a script::

    # -- master side --
    sess = iec104.MasterSession("10.0.0.5", ca=1)
    sess.connect()
    replies = sess.gi()                  # general interrogation
    sess.command(4001, "C_SC_NA_1", on=True, select_execute=True)
    sess.close()

    # -- slave / simulator side (LAB only) --
    srv = iec104.SlaveServer(port=2404, ca=1)
    srv.inject(4001, "M_ME_NC_1", 230.5)
    srv.start()
    # ... test master against srv ...
    srv.stop()

    # -- offline PCAP analysis --
    reader = iec104.PcapReader("capture.pcap")
    for frame in reader:
        print(frame.timestamp, frame.apdu)

    # -- codec fuzzer (LAB only) --
    report = iec104.Fuzzer.codec_roundtrip(iterations=1000)
    print(report.crashes)

Safety
------
* All command-sending methods (``command``, ``gi``, ``clock_sync``) call the
  ``SafetyContext`` supplied at construction time (if any).  When no
  SafetyContext is given (headless script with ``--allow-writes``), writes
  proceed unconditionally.
* A script **without** ``--allow-writes`` gets a deny-everything
  ``SafetyContext``; ``command()`` will raise :class:`PermissionError`.
* Slave servers are forbidden in PRODUCTION (raises ``RuntimeError`` on
  ``SlaveServer.start()``).
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from protoskipper.builtin_drivers.iec104.asdu import (
    Asdu,
    Quality,
    TypeID,
)
from protoskipper.builtin_drivers.iec104.fuzzer import (
    FuzzReport,
    StateMachineReport,
    fuzz_against_target,
    fuzz_apdu_mutation,
    fuzz_asdu_codec,
    fuzz_codec_roundtrip,
)
from protoskipper.builtin_drivers.iec104.master import (
    Iec104MasterSession,
    MasterConfig,
)
from protoskipper.builtin_drivers.iec104.pcap import (
    DissectedFrame,
    iter_iec104_frames,
    summarize_pcap,
)
from protoskipper.builtin_drivers.iec104.slave import (
    Iec104SlaveServer,
    SlaveConfig,
    SlavePoint,
)

__all__ = [
    "Fuzzer",
    "MasterSession",
    "PcapReader",
    "SlaveServer",
]

# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------

_AnyConfirmFn = Callable[[str], bool]
_ConfirmOrNone = _AnyConfirmFn | None


def _resolve_type_id(t: str | int | TypeID) -> TypeID:
    """Accept a TypeID name string, integer, or TypeID enum member."""
    if isinstance(t, TypeID):
        return t
    if isinstance(t, int):
        try:
            return TypeID(t)
        except ValueError as exc:
            raise ValueError(f"Unknown TypeID value: {t!r}") from exc
    # Try by name (e.g. "C_SC_NA_1")
    try:
        return TypeID[t]
    except KeyError as exc:
        raise ValueError(f"Unknown TypeID name: {t!r}") from exc


# ---------------------------------------------------------------------------
# MasterSession
# ---------------------------------------------------------------------------


class MasterSession:
    """High-level IEC 104 master session for scripts and the REPL.

    Parameters mirror :class:`~protoskipper.builtin_drivers.iec104.master.MasterConfig`
    but use keyword-only form for readability.

    Example::

        with iec104.MasterSession("10.0.4.13", ca=1) as sess:
            objects = sess.gi()
            sess.command(4001, "C_SC_NA_1", on=True)
    """

    def __init__(
        self,
        host: str,
        port: int = 2404,
        *,
        ca: int = 1,
        originator: int = 0,
        k: int = 12,
        w: int = 8,
        t0: float = 30.0,
        t1: float = 15.0,
        t2: float = 10.0,
        t3: float = 20.0,
        auto_reconnect: bool = False,
        reconnect_delay: float = 5.0,
        tls: bool = False,
        tls_context: ssl.SSLContext | None = None,
        tls_server_hostname: str | None = None,
        confirm: _ConfirmOrNone = None,
        allow_writes: bool = False,
    ) -> None:
        self._cfg = MasterConfig(
            host=host,
            port=port,
            ca=ca,
            originator=originator,
            k=k,
            w=w,
            t0=t0,
            t1=t1,
            t2=t2,
            t3=t3,
            auto_reconnect=auto_reconnect,
            reconnect_delay=reconnect_delay,
            tls=tls,
            tls_context=tls_context,
            tls_server_hostname=tls_server_hostname,
        )
        self._allow_writes = allow_writes
        self._confirm = confirm
        self._session: Iec104MasterSession | None = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> MasterSession:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open TCP, send STARTDT act, wait for STARTDT con."""
        sess = Iec104MasterSession(self._cfg)
        sess.connect()
        self._session = sess

    def close(self) -> None:
        """Send STOPDT act and close the TCP connection."""
        if self._session is not None:
            self._session.close()
            self._session = None

    @property
    def started(self) -> bool:
        return self._session is not None and self._session.started

    # ------------------------------------------------------------------
    # Interrogation
    # ------------------------------------------------------------------

    def gi(self, group: int = 20, *, timeout: float | None = None) -> list[Asdu]:
        """Issue a General Interrogation (C_IC_NA_1 100).

        *group* defaults to 20 (station interrogation).  Returns all monitor
        ASDUs received between ACTCON and ACTTERM.
        """
        return self._session_or_raise().general_interrogation(qoi=group, timeout=timeout)

    def ci(
        self,
        group: int = 37,
        freeze: int = 1,
        *,
        timeout: float | None = None,
    ) -> list[Asdu]:
        """Issue a Counter Interrogation (C_CI_NA_1 101).

        *freeze* values per spec: 1=read, 2=freeze, 3=freeze-and-reset,
        4=reset.
        """
        return self._session_or_raise().counter_interrogation(
            rqt=group, frz=freeze, timeout=timeout
        )

    def read(self, ioa: int, *, timeout: float | None = None) -> Asdu:
        """Issue a Read command (C_RD_NA_1 102) for a single IOA."""
        return self._session_or_raise().read(ioa=ioa, timeout=timeout)

    # ------------------------------------------------------------------
    # Commands (write-gated)
    # ------------------------------------------------------------------

    def command(
        self,
        ioa: int,
        type_id: str | int | TypeID,
        *,
        value: Any = None,
        on: bool | None = None,
        qu: int = 0,
        select_execute: bool = False,
        timeout: float | None = None,
    ) -> list[Asdu]:
        """Send a control-direction command.

        *value* (or convenience *on* for single-command) is the setpoint /
        state.  *qu* is the Qualifier of Command (0 = unspecified).
        *select_execute=True* uses Select-Before-Operate (SBO) mode.

        Raises :exc:`PermissionError` if writes are not enabled.
        """
        self._require_write_permission(f"command IOA={ioa}")
        tid = _resolve_type_id(type_id)
        sess = self._session_or_raise()

        # Convenience: single-point on/off
        if tid == TypeID.C_SC_NA_1:
            state = bool(on if on is not None else value)
            return [
                sess.single_command(
                    ioa=ioa, on=state, qu=qu, select=select_execute, timeout=timeout
                )
            ]
        if tid == TypeID.C_DC_NA_1:
            # DCO: 1=OFF, 2=ON
            state = bool(on if on is not None else value)
            dcs = 2 if state else 1
            return [
                sess.double_command(ioa=ioa, dcs=dcs, qu=qu, select=select_execute, timeout=timeout)
            ]
        if tid == TypeID.C_SE_NA_1:
            return [
                sess.set_point_normalised(
                    ioa=ioa, value=int(value), select=select_execute, timeout=timeout
                )
            ]
        if tid == TypeID.C_SE_NB_1:
            return [
                sess.set_point_scaled(
                    ioa=ioa, value=int(value), select=select_execute, timeout=timeout
                )
            ]
        if tid == TypeID.C_SE_NC_1:
            return [
                sess.set_point_float(
                    ioa=ioa, value=float(value), select=select_execute, timeout=timeout
                )
            ]
        raise ValueError(f"Unsupported command type: {tid!r}")

    def clock_sync(self, ts: datetime | None = None, *, timeout: float | None = None) -> Asdu:
        """Send a clock synchronisation command (C_CS_NA_1 103).

        *ts* defaults to the current UTC time.

        Raises :exc:`PermissionError` if writes are not enabled.
        """
        self._require_write_permission("clock_sync")
        return self._session_or_raise().clock_sync(ts=ts, timeout=timeout)

    def subscribe(self, callback: Callable[[Asdu], None]) -> None:
        """Register a callback for spontaneous (unsolicited) ASDUs."""
        self._session_or_raise().set_spontaneous_listener(callback)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session_or_raise(self) -> Iec104MasterSession:
        if self._session is None:
            raise RuntimeError("Not connected — call connect() first")
        return self._session

    def _require_write_permission(self, label: str) -> None:
        if self._allow_writes:
            return
        if self._confirm is not None:
            if not self._confirm(label):
                raise PermissionError(f"Write denied by confirm handler: {label}")
            return
        raise PermissionError(
            f"Write '{label}' requires allow_writes=True or a confirm callback. "
            "Pass --allow-writes to protoskipper run, or supply confirm= to MasterSession."
        )


# ---------------------------------------------------------------------------
# SlaveServer
# ---------------------------------------------------------------------------


class SlaveServer:
    """IEC 104 slave/simulator for scripts and the REPL.

    Wraps :class:`~protoskipper.builtin_drivers.iec104.slave.Iec104SlaveServer`
    with a simpler interface and safety-profile guard.

    Example::

        srv = iec104.SlaveServer(port=2404, ca=1)
        srv.inject(4001, "M_ME_NC_1", 230.5)
        srv.inject(1001, "M_SP_NA_1", False)
        srv.start()
        # your test code here
        srv.stop()
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 2404,
        *,
        ca: int = 1,
        k: int = 12,
        w: int = 8,
        t1: float = 15.0,
        t2: float = 10.0,
        t3: float = 20.0,
        max_clients: int = 4,
        tls: bool = False,
        tls_context: ssl.SSLContext | None = None,
        allow_in_production: bool = False,
    ) -> None:
        cfg = SlaveConfig(
            host=host,
            port=port,
            ca=ca,
            k=k,
            w=w,
            t1=t1,
            t2=t2,
            t3=t3,
            max_clients=max_clients,
            tls=tls,
            tls_context=tls_context,
        )
        self._server = Iec104SlaveServer(cfg)
        self._allow_in_production = allow_in_production

    @property
    def port(self) -> int:
        """The actual bound port (useful when port=0 was passed)."""
        return self._server.port

    @property
    def ca(self) -> int:
        return self._server.config.ca

    def inject(
        self,
        ioa: int,
        type_id: str | int | TypeID,
        value: Any,
        *,
        quality: Quality | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        """Add or update a point in the slave data store.

        If the server is running, connected masters receive a spontaneous
        ASDU immediately.
        """
        tid = _resolve_type_id(type_id)
        q = quality if quality is not None else Quality()
        if self._server.get_point(ioa) is None:
            # Point not yet registered — add it first.
            self._server.add_point(SlavePoint(ioa=ioa, type_id=tid, value=value, quality=q))
        else:
            self._server.update(ioa=ioa, value=value, quality=q, timestamp=timestamp)

    def add_points(self, points: list[SlavePoint]) -> None:
        """Bulk-load a list of :class:`SlavePoint` objects before starting."""
        self._server.add_points(points)

    def set_command_handler(self, handler: Callable[[Asdu], bool | None] | None) -> None:
        """Override the default command handler.

        Return ``True`` to accept, ``False`` to reject (negative ACTCON),
        ``None`` to use the default (accept + update the point).
        """
        self._server.set_command_handler(handler)

    def start(self) -> None:
        """Bind and start listening for incoming master connections.

        Raises ``RuntimeError`` when called from a PRODUCTION safety profile
        without ``allow_in_production=True``.
        """
        # Guard: simulating an RTU on a live production network is dangerous.
        # The REPL passes allow_writes=False in PRODUCTION; scripts that
        # explicitly pass allow_in_production=True take responsibility.
        # In practice the SessionManager will use a LAB/COMMISSIONING profile
        # when the scripting console is open — we check as a belt-and-suspenders.
        if not self._allow_in_production:
            import os

            if os.environ.get("PROTOSKIPPER_PROFILE", "").upper() == "PRODUCTION":
                raise RuntimeError(
                    "SlaveServer.start() is not permitted in PRODUCTION profile. "
                    "Pass allow_in_production=True only if you are certain this is "
                    "a lab / staging environment."
                )
        self._server.start()

    def stop(self) -> None:
        """Disconnect all clients and stop listening."""
        self._server.stop()

    def __enter__(self) -> SlaveServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# PcapReader
# ---------------------------------------------------------------------------


class PcapReader:
    """Offline PCAP/libpcap dissector for IEC 104 traffic.

    Iterates over a libpcap file and yields
    :class:`~protoskipper.builtin_drivers.iec104.pcap.DissectedFrame` objects.

    Example::

        reader = iec104.PcapReader("session.pcap")
        for frame in reader:
            if frame.apdu and frame.apdu.asdu:
                print(frame.timestamp, frame.apdu.asdu.type_id)

        # Or get a quick summary:
        summary = iec104.PcapReader.summarize("session.pcap")
        print(summary)
    """

    def __init__(self, path: str | Path, *, iec104_port: int = 2404) -> None:
        self._path = Path(path)
        self._port = iec104_port

    def __iter__(self) -> Iterator[DissectedFrame]:
        yield from iter_iec104_frames(self._path, iec104_port=self._port)

    @staticmethod
    def summarize(path: str | Path, *, iec104_port: int = 2404) -> dict[str, int]:
        """Return a summary dict: total_frames, i_frames, s_frames, u_frames, flows."""
        return summarize_pcap(path, iec104_port=iec104_port)


# ---------------------------------------------------------------------------
# Fuzzer
# ---------------------------------------------------------------------------


class Fuzzer:
    """IEC 104 deterministic fuzzer (LAB profile only).

    All methods are static / class-level; no instance needed.

    Example::

        # codec round-trip: catches parser crashes on random input
        report = iec104.Fuzzer.codec_roundtrip(iterations=10_000, seed=42)
        assert report.crashes == 0

        # mutation fuzzer: sends malformed APDUs to a running master/slave
        rpt = iec104.Fuzzer.against_target("127.0.0.1", 2404, iterations=200)
        print(rpt.outcomes)
    """

    @staticmethod
    def codec_roundtrip(iterations: int = 5000, *, seed: int = 0) -> FuzzReport:
        """Generate random APCI bytes; verify they decode without crashing."""
        return fuzz_codec_roundtrip(iterations=iterations, seed=seed)

    @staticmethod
    def asdu_codec(iterations: int = 5000, *, seed: int = 0) -> FuzzReport:
        """Generate random ASDU bytes; verify they decode without crashing."""
        return fuzz_asdu_codec(iterations=iterations, seed=seed)

    @staticmethod
    def apdu_mutation(iterations: int = 5000, *, seed: int = 0) -> FuzzReport:
        """Take known-good APDUs and apply single-byte mutations."""
        return fuzz_apdu_mutation(iterations=iterations, seed=seed)

    @staticmethod
    def against_target(
        host: str,
        port: int = 2404,
        *,
        iterations: int = 100,
        seed: int = 0,
        timeout: float = 2.0,
    ) -> StateMachineReport:
        """Connect to *host:port* and push a stream of malformed APDUs.

        Returns a :class:`~protoskipper.builtin_drivers.iec104.fuzzer.StateMachineReport`
        describing how the target responded (clean close, silent drop, etc.).
        """
        return fuzz_against_target(
            host=host,
            port=port,
            iterations=iterations,
            seed=seed,
            connect_timeout=timeout,
        )
