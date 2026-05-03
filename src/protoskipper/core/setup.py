# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 session setup file — save/load ``iec104-setup.json`` (§6 of IEC104_PLAN.md).

The setup file captures everything needed to reproduce a commissioning
session: connection parameters, vendor profile, point-list path, watchlist,
plot configuration, and SOE filters.  Secrets (TLS private keys) are never
stored in the file; they live in the OS keychain.

Design notes
------------
* This module is in ``core/`` and has **zero Qt imports**.
* It uses ``json`` from the stdlib only; no external dependencies.
* The schema is versioned via the top-level ``"version"`` field.
  A loader that encounters ``version > CURRENT_VERSION`` raises
  :class:`SetupVersionError` rather than silently mis-interpreting the file.
* All paths in the file are stored as strings.  The caller is responsible
  for resolving relative paths before passing to :func:`load`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protoskipper.core.errors import ProtoSkipperError

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

CURRENT_VERSION: int = 1
"""Bump when the schema becomes incompatible with a previous version."""

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SetupError(ProtoSkipperError):
    """Base class for setup-file errors."""


class SetupVersionError(SetupError):
    """Raised when the file's ``version`` field exceeds :data:`CURRENT_VERSION`."""

    def __init__(self, found: int) -> None:
        super().__init__(
            f"Setup file version {found} is newer than this ProtoSkipper build "
            f"(supports up to version {CURRENT_VERSION}). "
            "Please upgrade ProtoSkipper."
        )
        self.found = found


class SetupValidationError(SetupError):
    """Raised when a required field is missing or has an illegal value."""


# ---------------------------------------------------------------------------
# Sub-dataclasses (connection parameters, watchlist entries, etc.)
# ---------------------------------------------------------------------------


@dataclass
class TlsConfig:
    """TLS transport settings."""

    enabled: bool = False
    version: str = "auto"  # "1.2", "1.3", "auto"
    mutual_auth: bool = False
    client_cert: str = ""  # PEM file path
    trust_roots: list[str] = field(default_factory=list)
    verify_server_cert: bool = True


@dataclass
class ConnectionParams:
    """IEC 104 connection parameters (§5.1 of IEC104_PLAN.md)."""

    host: str = "127.0.0.1"
    port: int = 2404
    tls: TlsConfig = field(default_factory=TlsConfig)
    common_address: int = 1
    ca_size: int = 2
    ioa_size: int = 3
    cot_size: int = 2
    originator_address: int = 0
    k: int = 12
    w: int = 8
    t0: int = 30
    t1: int = 15
    t2: int = 10
    t3: int = 20
    auto_startdt: bool = True
    auto_gi: bool = True
    auto_counter_int: bool = True
    auto_clock_sync: bool = True
    reconnect_enabled: bool = True
    reconnect_backoff_s: int = 5


@dataclass
class WatchlistEntry:
    """A single point pinned to the watchlist."""

    ioa: int
    poll_ms: int = 0  # 0 = event-driven only


@dataclass
class PlotTrace:
    """One trace on the live plot."""

    ioa: int
    color: str = "#88CCEE"


@dataclass
class PlotConfig:
    """Live-plot settings."""

    traces: list[PlotTrace] = field(default_factory=list)
    range: str = "5min"  # "30s", "1min", "5min", "1hour", "session"


@dataclass
class SoeFilters:
    """SOE panel filter state."""

    types: list[int] = field(default_factory=list)  # ASDU type IDs
    cots: list[int] = field(default_factory=list)  # COT values


@dataclass
class CommandTemplate:
    """A saved command template."""

    name: str
    ioa: int
    type: int  # ASDU type ID e.g. 45 = C_SC_NA_1
    qu: int = 0
    select_execute: bool = True
    value: str = "off"


@dataclass
class TimeSyncConfig:
    periodic_s: int = 0  # 0 = disabled


@dataclass
class PointListRef:
    """Reference to a CSV/XLSX point list."""

    path: str
    sheet: str = ""  # XLSX sheet name; empty = first sheet
    checksum_sha256: str = ""  # optional integrity check


# ---------------------------------------------------------------------------
# Top-level setup dataclass
# ---------------------------------------------------------------------------


@dataclass
class Iec104Setup:
    """In-memory representation of one ``iec104-setup.json`` file."""

    name: str = ""
    created: str = ""  # ISO-8601 UTC timestamp (populated on first save)
    operator: str = ""
    vendor_profile: str = "generic"
    point_list: PointListRef | None = None
    connection: ConnectionParams = field(default_factory=ConnectionParams)
    watchlist: list[WatchlistEntry] = field(default_factory=list)
    plot: PlotConfig = field(default_factory=PlotConfig)
    soe: SoeFilters = field(default_factory=SoeFilters)
    command_templates: list[CommandTemplate] = field(default_factory=list)
    time_sync: TimeSyncConfig = field(default_factory=TimeSyncConfig)
    audit_dir: str = ""
    profile: str = "lab"  # "lab" | "commissioning" | "production"


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _tls_to_dict(t: TlsConfig) -> dict[str, Any]:
    return {
        "enabled": t.enabled,
        "version": t.version,
        "mutual_auth": t.mutual_auth,
        "client_cert": t.client_cert,
        "trust_roots": t.trust_roots,
        "verify_server_cert": t.verify_server_cert,
    }


def _conn_to_dict(c: ConnectionParams) -> dict[str, Any]:
    return {
        "host": c.host,
        "port": c.port,
        "tls": _tls_to_dict(c.tls),
        "common_address": c.common_address,
        "ca_size": c.ca_size,
        "ioa_size": c.ioa_size,
        "cot_size": c.cot_size,
        "originator_address": c.originator_address,
        "params": {"k": c.k, "w": c.w, "t0": c.t0, "t1": c.t1, "t2": c.t2, "t3": c.t3},
        "auto": {
            "startdt": c.auto_startdt,
            "gi": c.auto_gi,
            "counter_int": c.auto_counter_int,
            "clock_sync": c.auto_clock_sync,
        },
        "reconnect": {"enabled": c.reconnect_enabled, "backoff_s": c.reconnect_backoff_s},
    }


def _setup_to_dict(s: Iec104Setup) -> dict[str, Any]:
    d: dict[str, Any] = {
        "$schema": "https://protoskipper.io/schemas/iec104-setup-v1.json",
        "version": CURRENT_VERSION,
        "name": s.name,
        "created": s.created,
        "operator": s.operator,
        "vendor_profile": s.vendor_profile,
        "connection": _conn_to_dict(s.connection),
        "watchlist": [{"ioa": e.ioa, "poll_ms": e.poll_ms} for e in s.watchlist],
        "plot": {
            "traces": [{"ioa": t.ioa, "color": t.color} for t in s.plot.traces],
            "range": s.plot.range,
        },
        "soe": {"filters": {"types": s.soe.types, "cots": s.soe.cots}},
        "command_templates": [
            {
                "name": ct.name,
                "ioa": ct.ioa,
                "type": ct.type,
                "qu": ct.qu,
                "select_execute": ct.select_execute,
                "value": ct.value,
            }
            for ct in s.command_templates
        ],
        "time_sync": {"periodic_s": s.time_sync.periodic_s},
        "audit": {"dir": s.audit_dir},
        "profile": s.profile,
    }
    if s.point_list is not None:
        d["point_list"] = {
            "path": s.point_list.path,
            "sheet": s.point_list.sheet,
            "checksum_sha256": s.point_list.checksum_sha256,
        }
    return d


# ---------------------------------------------------------------------------
# Deserialisation helpers
# ---------------------------------------------------------------------------


def _parse_tls(raw: dict[str, Any]) -> TlsConfig:
    return TlsConfig(
        enabled=bool(raw.get("enabled", False)),
        version=str(raw.get("version", "auto")),
        mutual_auth=bool(raw.get("mutual_auth", False)),
        client_cert=str(raw.get("client_cert", "")),
        trust_roots=[str(x) for x in raw.get("trust_roots", [])],
        verify_server_cert=bool(raw.get("verify_server_cert", True)),
    )


def _parse_conn(raw: dict[str, Any]) -> ConnectionParams:
    params = raw.get("params", {})
    auto = raw.get("auto", {})
    reconnect = raw.get("reconnect", {})
    tls_raw = raw.get("tls", {})
    return ConnectionParams(
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 2404)),
        tls=_parse_tls(tls_raw) if isinstance(tls_raw, dict) else TlsConfig(),
        common_address=int(raw.get("common_address", 1)),
        ca_size=int(raw.get("ca_size", 2)),
        ioa_size=int(raw.get("ioa_size", 3)),
        cot_size=int(raw.get("cot_size", 2)),
        originator_address=int(raw.get("originator_address", 0)),
        k=int(params.get("k", 12)),
        w=int(params.get("w", 8)),
        t0=int(params.get("t0", 30)),
        t1=int(params.get("t1", 15)),
        t2=int(params.get("t2", 10)),
        t3=int(params.get("t3", 20)),
        auto_startdt=bool(auto.get("startdt", True)),
        auto_gi=bool(auto.get("gi", True)),
        auto_counter_int=bool(auto.get("counter_int", True)),
        auto_clock_sync=bool(auto.get("clock_sync", True)),
        reconnect_enabled=bool(reconnect.get("enabled", True)),
        reconnect_backoff_s=int(reconnect.get("backoff_s", 5)),
    )


def _parse_point_list(raw: dict[str, Any]) -> PointListRef:
    return PointListRef(
        path=str(raw.get("path", "")),
        sheet=str(raw.get("sheet", "")),
        checksum_sha256=str(raw.get("checksum_sha256", "")),
    )


def _dict_to_setup(d: dict[str, Any]) -> Iec104Setup:
    version = int(d.get("version", 1))
    if version > CURRENT_VERSION:
        raise SetupVersionError(version)

    pl_raw = d.get("point_list")
    point_list = _parse_point_list(pl_raw) if isinstance(pl_raw, dict) else None

    watchlist = [
        WatchlistEntry(ioa=int(e["ioa"]), poll_ms=int(e.get("poll_ms", 0)))
        for e in d.get("watchlist", [])
        if isinstance(e, dict) and "ioa" in e
    ]

    plot_raw = d.get("plot", {})
    traces = [
        PlotTrace(ioa=int(t["ioa"]), color=str(t.get("color", "#88CCEE")))
        for t in plot_raw.get("traces", [])
        if isinstance(t, dict) and "ioa" in t
    ]
    plot = PlotConfig(traces=traces, range=str(plot_raw.get("range", "5min")))

    soe_raw = d.get("soe", {}).get("filters", {})
    soe = SoeFilters(
        types=[int(x) for x in soe_raw.get("types", [])],
        cots=[int(x) for x in soe_raw.get("cots", [])],
    )

    cts = [
        CommandTemplate(
            name=str(ct["name"]),
            ioa=int(ct["ioa"]),
            type=int(ct["type"]),
            qu=int(ct.get("qu", 0)),
            select_execute=bool(ct.get("select_execute", True)),
            value=str(ct.get("value", "off")),
        )
        for ct in d.get("command_templates", [])
        if isinstance(ct, dict) and "name" in ct and "ioa" in ct and "type" in ct
    ]

    ts_raw = d.get("time_sync", {})
    audit_raw = d.get("audit", {})

    conn_raw = d.get("connection", {})

    return Iec104Setup(
        name=str(d.get("name", "")),
        created=str(d.get("created", "")),
        operator=str(d.get("operator", "")),
        vendor_profile=str(d.get("vendor_profile", "generic")),
        point_list=point_list,
        connection=_parse_conn(conn_raw) if isinstance(conn_raw, dict) else ConnectionParams(),
        watchlist=watchlist,
        plot=plot,
        soe=soe,
        command_templates=cts,
        time_sync=TimeSyncConfig(periodic_s=int(ts_raw.get("periodic_s", 0))),
        audit_dir=str(audit_raw.get("dir", "")),
        profile=str(d.get("profile", "lab")),
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_PROFILE_VALUES = {"lab", "commissioning", "production"}
_VERSION_RE = re.compile(r"^1\.\d$|^auto$")


def _validate(s: Iec104Setup) -> None:
    """Raise :class:`SetupValidationError` on the first violation found."""
    if not (1 <= s.connection.common_address <= 65534):
        raise SetupValidationError(
            f"common_address must be 1..65534, got {s.connection.common_address}"
        )
    if not (1 <= s.connection.port <= 65535):
        raise SetupValidationError(f"port must be 1..65535, got {s.connection.port}")
    if s.connection.k < 1 or s.connection.k > 32767:
        raise SetupValidationError(f"k must be 1..32767, got {s.connection.k}")
    if not (1 <= s.connection.w <= s.connection.k):
        raise SetupValidationError(f"w must be 1..k ({s.connection.k}), got {s.connection.w}")
    if s.profile not in _PROFILE_VALUES:
        raise SetupValidationError(f"profile must be one of {_PROFILE_VALUES}, got {s.profile!r}")
    if s.connection.tls.version not in ("1.2", "1.3", "auto"):
        raise SetupValidationError(
            f"tls.version must be '1.2', '1.3', or 'auto', got {s.connection.tls.version!r}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load(path: str | Path) -> Iec104Setup:
    """Load an ``iec104-setup.json`` file from *path* and return an :class:`Iec104Setup`.

    Raises
    ------
    SetupVersionError
        When the file's version is newer than this build supports.
    SetupValidationError
        When a required field is missing or has an illegal value.
    OSError
        When the file cannot be read.
    json.JSONDecodeError
        When the file is not valid JSON.
    """
    text = Path(path).read_text(encoding="utf-8")
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise SetupValidationError("Setup file root must be a JSON object.")
    setup = _dict_to_setup(raw)
    _validate(setup)
    return setup


def save(setup: Iec104Setup, path: str | Path) -> None:
    """Serialise *setup* to *path* as pretty-printed JSON.

    The ``created`` timestamp is populated if it is currently empty.
    Parent directories are created automatically.

    Raises
    ------
    SetupValidationError
        When *setup* contains illegal values (see :func:`_validate`).
    OSError
        When the file cannot be written.
    """
    _validate(setup)
    if not setup.created:
        setup.created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(_setup_to_dict(setup), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
