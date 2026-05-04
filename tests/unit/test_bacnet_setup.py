# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for P7.I.3: BACnet setup save/load (setup.py)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from protoskipper.builtin_drivers.bacnet.setup import (
    _SCHEMA_VERSION,
    BacnetSetup,
    BenchTile,
    CovSubscription,
    DeviceSetup,
    TransportConfig,
    TrendLogSetup,
    WatchlistEntry,
)

# ---------------------------------------------------------------------------
# TransportConfig
# ---------------------------------------------------------------------------


class TestTransportConfig:
    def test_defaults(self) -> None:
        t = TransportConfig()
        assert t.kind == "ipv4"
        assert t.local_port == 47808
        assert t.bbmd is None
        assert t.foreign_device is None

    def test_round_trip(self) -> None:
        t = TransportConfig(
            kind="ipv4",
            iface="eth0",
            local_port=47809,
            bbmd="192.168.1.1:47808",
            foreign_device={"bbmd": "10.0.0.1:47808", "ttl": 600},
        )
        t2 = TransportConfig.from_dict(t.to_dict())
        assert t2.kind == t.kind
        assert t2.iface == t.iface
        assert t2.local_port == t.local_port
        assert t2.bbmd == t.bbmd
        assert t2.foreign_device == t.foreign_device


# ---------------------------------------------------------------------------
# WatchlistEntry
# ---------------------------------------------------------------------------


class TestWatchlistEntry:
    def test_defaults(self) -> None:
        w = WatchlistEntry(objid="AV:1")
        assert w.prop == "present-value"
        assert w.poll_ms is None
        assert w.cov_ms is None

    def test_round_trip(self) -> None:
        w = WatchlistEntry(objid="AO:1", prop="present-value", poll_ms=1000, cov_ms=0)
        w2 = WatchlistEntry.from_dict(w.to_dict())
        assert w2.objid == w.objid
        assert w2.poll_ms == 1000

    def test_missing_optional_keys(self) -> None:
        w = WatchlistEntry.from_dict({"objid": "BV:1"})
        assert w.prop == "present-value"
        assert w.poll_ms is None


# ---------------------------------------------------------------------------
# CovSubscription
# ---------------------------------------------------------------------------


class TestCovSubscription:
    def test_defaults(self) -> None:
        c = CovSubscription(objid="AV:1")
        assert c.lifetime_s == 300
        assert c.confirmed is True
        assert c.increment is None

    def test_round_trip(self) -> None:
        c = CovSubscription(
            objid="AV:2",
            prop="present-value",
            lifetime_s=600,
            increment=0.5,
            confirmed=False,
        )
        c2 = CovSubscription.from_dict(c.to_dict())
        assert c2.increment == 0.5
        assert c2.confirmed is False


# ---------------------------------------------------------------------------
# TrendLogSetup
# ---------------------------------------------------------------------------


class TestTrendLogSetup:
    def test_round_trip(self) -> None:
        t = TrendLogSetup(objid="TL:3", auto_pull_interval_s=7200)
        t2 = TrendLogSetup.from_dict(t.to_dict())
        assert t2.objid == "TL:3"
        assert t2.auto_pull_interval_s == 7200


# ---------------------------------------------------------------------------
# DeviceSetup
# ---------------------------------------------------------------------------


class TestDeviceSetup:
    def test_minimal(self) -> None:
        d = DeviceSetup(device_id=1234, address="10.0.0.1:47808")
        assert d.vendor_profile == "generic"
        assert d.points_list is None
        assert d.watchlist == []

    def test_round_trip_full(self) -> None:
        d = DeviceSetup(
            device_id=5678,
            address="192.168.1.10:47808",
            vendor_profile="jci_metasys_nae",
            points_list="/tmp/AHU1.csv",
            watchlist=[WatchlistEntry(objid="AV:1", poll_ms=1000)],
            cov_subscriptions=[CovSubscription(objid="AV:1", lifetime_s=600)],
            trend_logs=[TrendLogSetup(objid="TL:1")],
            schedules_pinned=["SCH:1"],
            alarms_pinned=["NC:1"],
        )
        d2 = DeviceSetup.from_dict(d.to_dict())
        assert d2.device_id == 5678
        assert d2.vendor_profile == "jci_metasys_nae"
        assert len(d2.watchlist) == 1
        assert d2.watchlist[0].objid == "AV:1"
        assert len(d2.cov_subscriptions) == 1
        assert len(d2.trend_logs) == 1
        assert d2.schedules_pinned == ["SCH:1"]
        assert d2.alarms_pinned == ["NC:1"]


# ---------------------------------------------------------------------------
# BenchTile
# ---------------------------------------------------------------------------


class TestBenchTile:
    def test_defaults(self) -> None:
        t = BenchTile(device_id=1234)
        assert t.x == 0
        assert t.y == 0

    def test_round_trip(self) -> None:
        t = BenchTile(device_id=1234, x=3, y=7)
        t2 = BenchTile.from_dict(t.to_dict())
        assert t2.x == 3
        assert t2.y == 7


# ---------------------------------------------------------------------------
# BacnetSetup — to_dict / from_dict
# ---------------------------------------------------------------------------


class TestBacnetSetupDict:
    def test_empty_setup(self) -> None:
        s = BacnetSetup()
        d = s.to_dict()
        assert d["version"] == _SCHEMA_VERSION
        assert "$schema" in d
        assert d["devices"] == []

    def test_with_devices(self) -> None:
        s = BacnetSetup(name="Floor-3", operator="ayush")
        s.devices.append(DeviceSetup(device_id=1234, address="10.0.0.1:47808"))
        d = s.to_dict()
        assert len(d["devices"]) == 1
        assert d["devices"][0]["device_id"] == 1234

    def test_from_dict_round_trips_operator(self) -> None:
        s = BacnetSetup(name="Floor-3", operator="ayush@datasailors.io")
        s2 = BacnetSetup.from_dict(s.to_dict())
        assert s2.operator == "ayush@datasailors.io"
        assert s2.name == "Floor-3"

    def test_from_dict_rejects_unknown_version(self) -> None:
        raw = {"version": 99, "name": "x"}
        with pytest.raises(ValueError, match="Unsupported"):
            BacnetSetup.from_dict(raw)

    def test_bench_layout_tiles_serialised(self) -> None:
        s = BacnetSetup()
        s.bench_layout = {"tiles": [BenchTile(device_id=42, x=1, y=2)]}
        d = s.to_dict()
        tiles = d["bench_layout"]["tiles"]
        assert isinstance(tiles[0], dict)
        assert tiles[0]["device_id"] == 42

    def test_bench_layout_tiles_deserialised(self) -> None:
        s = BacnetSetup()
        s.bench_layout = {"tiles": [BenchTile(device_id=42, x=1, y=2)]}
        s2 = BacnetSetup.from_dict(s.to_dict())
        tiles = s2.bench_layout["tiles"]
        assert tiles[0].device_id == 42

    def test_transport_preserved(self) -> None:
        s = BacnetSetup(transport=TransportConfig(iface="en0", local_port=47809))
        s2 = BacnetSetup.from_dict(s.to_dict())
        assert s2.transport.iface == "en0"
        assert s2.transport.local_port == 47809


# ---------------------------------------------------------------------------
# BacnetSetup — save / load (disk I/O)
# ---------------------------------------------------------------------------


class TestBacnetSetupPersistence:
    def test_save_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / "bacnet-setup.json"
            BacnetSetup(name="test").save(p)
            assert p.exists()

    def test_load_round_trips(self) -> None:
        s = BacnetSetup(
            name="Bldg-A",
            operator="ci@datasailors.io",
            transport=TransportConfig(iface="en0"),
        )
        s.devices.append(
            DeviceSetup(
                device_id=1234,
                address="10.0.0.1:47808",
                watchlist=[WatchlistEntry(objid="AV:1", poll_ms=500)],
            )
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "setup.json"
            s.save(p)
            s2 = BacnetSetup.load(p)

        assert s2.name == "Bldg-A"
        assert s2.operator == "ci@datasailors.io"
        assert len(s2.devices) == 1
        assert s2.devices[0].device_id == 1234
        assert s2.devices[0].watchlist[0].objid == "AV:1"

    def test_created_timestamp_auto_set(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "setup.json"
            s = BacnetSetup(name="ts-test")
            assert s.created == ""
            s.save(p)
            assert s.created != ""
            s2 = BacnetSetup.load(p)
            assert s2.created != ""

    def test_created_not_overwritten_on_resave(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "setup.json"
            s = BacnetSetup(name="ts-test")
            s.save(p)
            first_ts = s.created
            s.save(p)  # second save
            assert s.created == first_ts  # should not change

    def test_save_is_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "setup.json"
            BacnetSetup(name="json-check").save(p)
            raw = json.loads(p.read_text())
            assert raw["version"] == _SCHEMA_VERSION

    def test_load_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            BacnetSetup.load(Path("/tmp/does-not-exist-xyz.json"))

    def test_load_bad_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("not json {", encoding="utf-8")
            with pytest.raises(json.JSONDecodeError):
                BacnetSetup.load(p)

    def test_load_non_object_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("[1, 2, 3]", encoding="utf-8")
            with pytest.raises(ValueError, match="Expected JSON object"):
                BacnetSetup.load(p)

    def test_load_unknown_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "future.json"
            p.write_text(json.dumps({"version": 99, "name": "x"}), encoding="utf-8")
            with pytest.raises(ValueError, match="Unsupported"):
                BacnetSetup.load(p)

    def test_full_roundtrip_no_secrets(self) -> None:
        """The saved file must not contain the word 'password'."""
        s = BacnetSetup(name="security-check", operator="test")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "setup.json"
            s.save(p)
            content = p.read_text(encoding="utf-8")
            assert "password" not in content.lower()
