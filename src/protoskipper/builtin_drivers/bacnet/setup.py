# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet setup file persistence (P7.I.3).

Provides save/load for ``bacnet-setup.json``, the per-commissioning-session
file that captures transport config, device list, watchlist, COV
subscriptions, trend-log auto-pull settings, and bench layout.

Design notes
------------
* **No secrets** are written to disk. Passwords and SC private keys are
  handled by the OS keychain; only the *path* to a cert/key file is stored.
* Schema version is ``1``.  Readers must check and reject unknown future
  versions rather than silently misinterpreting fields.
* The file is round-trippable: ``BacnetSetup.load(p).save(p)`` is a no-op.
* ``BacnetSetup`` and all nested dataclasses are plain dataclasses with
  ``to_dict()`` / ``from_dict()`` helpers.  JSON encoding is strict UTF-8,
  indent=2, ``ensure_ascii=False``.

Usage
-----
::

    from protoskipper.builtin_drivers.bacnet.setup import BacnetSetup

    setup = BacnetSetup(
        name="Bldg-A-Floor-3",
        operator="ayush@datasailors.io",
    )
    setup.devices.append(DeviceSetup(device_id=1234, address="10.10.4.13:47808"))
    setup.save(Path("/tmp/bacnet-setup.json"))

    loaded = BacnetSetup.load(Path("/tmp/bacnet-setup.json"))
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Nested config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TransportConfig:
    """BACnet/IP v4 transport settings."""

    kind: str = "ipv4"  # "ipv4" | "ipv6" | "sc" | "mstp"
    iface: str = ""  #: network interface name (e.g. "eth0")
    local_port: int = 47808
    bbmd: str | None = None  #: "host:port" if using BBMD-forwarded broadcast
    foreign_device: dict[str, Any] | None = None  #: {"bbmd": "host:port", "ttl": 600}

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "iface": self.iface,
            "local_port": self.local_port,
            "bbmd": self.bbmd,
            "foreign_device": self.foreign_device,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TransportConfig:
        return cls(
            kind=str(d.get("kind", "ipv4")),
            iface=str(d.get("iface", "")),
            local_port=int(d.get("local_port", 47808)),
            bbmd=d.get("bbmd"),
            foreign_device=d.get("foreign_device"),
        )


@dataclass
class WatchlistEntry:
    """A single watchlist row for a device."""

    objid: str  #: e.g. "AV:1"
    prop: str = "present-value"
    poll_ms: int | None = None  #: polling interval; None = COV only
    cov_ms: int | None = None  #: COV lifetime in ms; None = no COV

    def to_dict(self) -> dict[str, Any]:
        return {
            "objid": self.objid,
            "prop": self.prop,
            "poll_ms": self.poll_ms,
            "cov_ms": self.cov_ms,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WatchlistEntry:
        return cls(
            objid=str(d["objid"]),
            prop=str(d.get("prop", "present-value")),
            poll_ms=d.get("poll_ms"),
            cov_ms=d.get("cov_ms"),
        )


@dataclass
class CovSubscription:
    """A persisted COV subscription to restore on session open."""

    objid: str
    prop: str = "present-value"
    lifetime_s: int = 300
    increment: float | None = None
    confirmed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "objid": self.objid,
            "prop": self.prop,
            "lifetime_s": self.lifetime_s,
            "increment": self.increment,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CovSubscription:
        return cls(
            objid=str(d["objid"]),
            prop=str(d.get("prop", "present-value")),
            lifetime_s=int(d.get("lifetime_s", 300)),
            increment=d.get("increment"),
            confirmed=bool(d.get("confirmed", True)),
        )


@dataclass
class TrendLogSetup:
    """Auto-pull configuration for a single TrendLog object."""

    objid: str  #: e.g. "TL:1"
    auto_pull_interval_s: int = 3600  #: 0 = disabled

    def to_dict(self) -> dict[str, Any]:
        return {"objid": self.objid, "auto_pull_interval_s": self.auto_pull_interval_s}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrendLogSetup:
        return cls(
            objid=str(d["objid"]),
            auto_pull_interval_s=int(d.get("auto_pull_interval_s", 3600)),
        )


@dataclass
class DeviceSetup:
    """Persisted configuration for one BACnet device."""

    device_id: int
    address: str  #: "host:port" e.g. "10.10.4.13:47808"
    vendor_profile: str = "generic"
    points_list: str | None = None  #: path to EDE/AT/CSV/JSON
    watchlist: list[WatchlistEntry] = field(default_factory=list)
    cov_subscriptions: list[CovSubscription] = field(default_factory=list)
    trend_logs: list[TrendLogSetup] = field(default_factory=list)
    schedules_pinned: list[str] = field(default_factory=list)
    alarms_pinned: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "address": self.address,
            "vendor_profile": self.vendor_profile,
            "points_list": self.points_list,
            "watchlist": [w.to_dict() for w in self.watchlist],
            "cov_subscriptions": [c.to_dict() for c in self.cov_subscriptions],
            "trend_logs": [t.to_dict() for t in self.trend_logs],
            "schedules_pinned": list(self.schedules_pinned),
            "alarms_pinned": list(self.alarms_pinned),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeviceSetup:
        return cls(
            device_id=int(d["device_id"]),
            address=str(d["address"]),
            vendor_profile=str(d.get("vendor_profile", "generic")),
            points_list=d.get("points_list"),
            watchlist=[WatchlistEntry.from_dict(w) for w in d.get("watchlist", [])],
            cov_subscriptions=[
                CovSubscription.from_dict(c) for c in d.get("cov_subscriptions", [])
            ],
            trend_logs=[TrendLogSetup.from_dict(t) for t in d.get("trend_logs", [])],
            schedules_pinned=list(d.get("schedules_pinned", [])),
            alarms_pinned=list(d.get("alarms_pinned", [])),
        )


@dataclass
class BenchTile:
    """Position of one device tile in the bench overview."""

    device_id: int
    x: int = 0
    y: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "x": self.x, "y": self.y}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BenchTile:
        return cls(device_id=int(d["device_id"]), x=int(d.get("x", 0)), y=int(d.get("y", 0)))


# ---------------------------------------------------------------------------
# Root setup object
# ---------------------------------------------------------------------------


@dataclass
class BacnetSetup:
    """Root container for a BACnet commissioning session file.

    Attributes
    ----------
    name:
        Human-readable project name, e.g. ``"Bldg-A-Floor-3"``.
    operator:
        Free-form operator identifier recorded in the setup.
    created:
        ISO 8601 UTC timestamp; auto-set on first save if empty.
    transport:
        BACnet transport configuration shared across all devices in this
        setup (devices may override locally — not yet modelled here).
    devices:
        One :class:`DeviceSetup` per target BACnet device.
    device_simulators:
        Unused in v1; reserved for future BACnet simulator configs.
    audit:
        Dict with ``"dir"`` key pointing to the audit-log directory.
    bench_layout:
        Dict with ``"tiles"`` list for the bench overview panel.
    """

    name: str = ""
    operator: str = ""
    created: str = ""
    transport: TransportConfig = field(default_factory=TransportConfig)
    devices: list[DeviceSetup] = field(default_factory=list)
    device_simulators: list[dict[str, Any]] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)
    bench_layout: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Serialise to *path* as UTF-8 JSON with 2-space indent.

        Raises
        ------
        OSError
            If the file cannot be written.
        """
        if not self.created:
            self.created = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> BacnetSetup:
        """Deserialise from *path*.

        Raises
        ------
        FileNotFoundError
            If *path* does not exist.
        ValueError
            If the JSON is malformed or the schema version is unsupported.
        """
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Expected JSON object, got {type(raw).__name__}")
        version = raw.get("version", 1)
        if version != _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported bacnet-setup schema version {version}; "
                f"this build supports version {_SCHEMA_VERSION}"
            )
        return cls.from_dict(raw)

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        tiles = self.bench_layout.get("tiles", [])
        if tiles and isinstance(tiles[0], BenchTile):
            tiles = [t.to_dict() for t in tiles]
        return {
            "$schema": "https://protoskipper.io/schemas/bacnet-setup-v1.json",
            "version": _SCHEMA_VERSION,
            "name": self.name,
            "created": self.created,
            "operator": self.operator,
            "transport": self.transport.to_dict(),
            "devices": [d.to_dict() for d in self.devices],
            "device_simulators": list(self.device_simulators),
            "audit": dict(self.audit),
            "bench_layout": {**self.bench_layout, "tiles": tiles},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BacnetSetup:
        version = d.get("version", _SCHEMA_VERSION)
        if version != _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported BacnetSetup schema version {version!r}; expected {_SCHEMA_VERSION}"
            )
        bench_raw = d.get("bench_layout", {})
        raw_tiles = bench_raw.get("tiles", [])
        tiles = [BenchTile.from_dict(t) for t in raw_tiles]
        bench_layout = {**bench_raw, "tiles": tiles}
        return cls(
            name=str(d.get("name", "")),
            operator=str(d.get("operator", "")),
            created=str(d.get("created", "")),
            transport=TransportConfig.from_dict(d.get("transport", {})),
            devices=[DeviceSetup.from_dict(dev) for dev in d.get("devices", [])],
            device_simulators=list(d.get("device_simulators", [])),
            audit=dict(d.get("audit", {})),
            bench_layout=bench_layout,
        )
