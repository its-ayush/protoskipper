# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for protoskipper.core.setup (iec104-setup.json save/load)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from protoskipper.core.setup import (
    CURRENT_VERSION,
    CommandTemplate,
    ConnectionParams,
    Iec104Setup,
    PlotConfig,
    PlotTrace,
    PointListRef,
    SetupValidationError,
    SetupVersionError,
    SoeFilters,
    TimeSyncConfig,
    TlsConfig,
    WatchlistEntry,
    load,
    save,
)


class TestSaveRoundTrip:
    def test_default_setup_roundtrips(self, tmp_path: Path) -> None:
        s = Iec104Setup(name="test", operator="alice")
        dest = tmp_path / "setup.json"
        save(s, dest)
        loaded = load(dest)
        assert loaded.name == "test"
        assert loaded.operator == "alice"
        assert loaded.connection.host == "127.0.0.1"
        assert loaded.connection.port == 2404

    def test_created_timestamp_auto_filled(self, tmp_path: Path) -> None:
        s = Iec104Setup()
        assert s.created == ""
        save(s, tmp_path / "s.json")
        assert s.created != ""  # mutated in-place

    def test_existing_created_preserved(self, tmp_path: Path) -> None:
        ts = "2026-01-15T10:00:00Z"
        s = Iec104Setup(created=ts)
        save(s, tmp_path / "s.json")
        assert s.created == ts

    def test_version_field_is_current(self, tmp_path: Path) -> None:
        dest = tmp_path / "s.json"
        save(Iec104Setup(), dest)
        raw = json.loads(dest.read_text())
        assert raw["version"] == CURRENT_VERSION

    def test_schema_field_present(self, tmp_path: Path) -> None:
        dest = tmp_path / "s.json"
        save(Iec104Setup(), dest)
        raw = json.loads(dest.read_text())
        assert "$schema" in raw

    def test_parent_dir_created(self, tmp_path: Path) -> None:
        dest = tmp_path / "deep" / "nested" / "setup.json"
        save(Iec104Setup(), dest)
        assert dest.exists()


class TestConnectionParams:
    def test_full_connection_roundtrips(self, tmp_path: Path) -> None:
        conn = ConnectionParams(
            host="10.0.0.1",
            port=2404,
            common_address=7,
            ca_size=1,
            ioa_size=2,
            cot_size=1,
            originator_address=3,
            k=20,
            w=15,
            t0=60,
            t1=30,
            t2=8,
            t3=25,
            auto_startdt=False,
            auto_gi=False,
            reconnect_enabled=False,
            reconnect_backoff_s=10,
        )
        s = Iec104Setup(connection=conn)
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        c = loaded.connection
        assert c.host == "10.0.0.1"
        assert c.ca_size == 1
        assert c.ioa_size == 2
        assert c.cot_size == 1
        assert c.k == 20
        assert c.w == 15
        assert c.t0 == 60
        assert not c.auto_startdt
        assert not c.reconnect_enabled

    def test_tls_config_roundtrips(self, tmp_path: Path) -> None:
        tls = TlsConfig(
            enabled=True,
            version="1.3",
            mutual_auth=True,
            client_cert="/path/to/cert.pem",
            trust_roots=["/path/to/ca1.pem", "/path/to/ca2.pem"],
            verify_server_cert=False,
        )
        s = Iec104Setup(connection=ConnectionParams(tls=tls))
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        t = loaded.connection.tls
        assert t.enabled is True
        assert t.version == "1.3"
        assert t.mutual_auth is True
        assert t.client_cert == "/path/to/cert.pem"
        assert len(t.trust_roots) == 2
        assert t.verify_server_cert is False


class TestWatchlistAndPlot:
    def test_watchlist_roundtrips(self, tmp_path: Path) -> None:
        s = Iec104Setup(
            watchlist=[
                WatchlistEntry(ioa=2001, poll_ms=1000),
                WatchlistEntry(ioa=4001, poll_ms=0),
            ]
        )
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        assert len(loaded.watchlist) == 2
        assert loaded.watchlist[0].ioa == 2001
        assert loaded.watchlist[0].poll_ms == 1000
        assert loaded.watchlist[1].ioa == 4001

    def test_plot_traces_roundtrip(self, tmp_path: Path) -> None:
        s = Iec104Setup(
            plot=PlotConfig(
                traces=[PlotTrace(ioa=2001, color="#FF0000")],
                range="1min",
            )
        )
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        assert loaded.plot.range == "1min"
        assert loaded.plot.traces[0].ioa == 2001
        assert loaded.plot.traces[0].color == "#FF0000"

    def test_soe_filters_roundtrip(self, tmp_path: Path) -> None:
        s = Iec104Setup(soe=SoeFilters(types=[30, 31, 36], cots=[3, 7]))
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        assert loaded.soe.types == [30, 31, 36]
        assert loaded.soe.cots == [3, 7]


class TestPointListAndTemplates:
    def test_point_list_roundtrips(self, tmp_path: Path) -> None:
        s = Iec104Setup(
            point_list=PointListRef(
                path="/data/points.xlsx",
                sheet="Points",
                checksum_sha256="abc123",
            )
        )
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        assert loaded.point_list is not None
        assert loaded.point_list.path == "/data/points.xlsx"
        assert loaded.point_list.sheet == "Points"
        assert loaded.point_list.checksum_sha256 == "abc123"

    def test_no_point_list_roundtrips(self, tmp_path: Path) -> None:
        s = Iec104Setup()
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        assert loaded.point_list is None

    def test_command_templates_roundtrip(self, tmp_path: Path) -> None:
        s = Iec104Setup(
            command_templates=[
                CommandTemplate(
                    name="Open Q1", ioa=4001, type=45, qu=1, select_execute=True, value="off"
                )
            ]
        )
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        assert len(loaded.command_templates) == 1
        ct = loaded.command_templates[0]
        assert ct.name == "Open Q1"
        assert ct.ioa == 4001
        assert ct.type == 45
        assert ct.select_execute is True

    def test_time_sync_and_audit_dir(self, tmp_path: Path) -> None:
        s = Iec104Setup(
            time_sync=TimeSyncConfig(periodic_s=300),
            audit_dir="/var/log/ps/",
        )
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        assert loaded.time_sync.periodic_s == 300
        assert loaded.audit_dir == "/var/log/ps/"


class TestProfileField:
    def test_default_profile_is_lab(self, tmp_path: Path) -> None:
        dest = tmp_path / "s.json"
        save(Iec104Setup(), dest)
        loaded = load(dest)
        assert loaded.profile == "lab"

    def test_production_profile_roundtrips(self, tmp_path: Path) -> None:
        s = Iec104Setup(profile="production")
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        assert loaded.profile == "production"


class TestValidation:
    def test_bad_port_raises(self) -> None:
        s = Iec104Setup(connection=ConnectionParams(port=0))
        with pytest.raises(SetupValidationError, match="port"):
            save(s, "/dev/null")

    def test_bad_ca_raises(self) -> None:
        s = Iec104Setup(connection=ConnectionParams(common_address=0))
        with pytest.raises(SetupValidationError, match="common_address"):
            save(s, "/dev/null")

    def test_w_exceeds_k_raises(self) -> None:
        s = Iec104Setup(connection=ConnectionParams(k=5, w=10))
        with pytest.raises(SetupValidationError, match="w must be"):
            save(s, "/dev/null")

    def test_bad_profile_raises(self) -> None:
        s = Iec104Setup(profile="superuser")
        with pytest.raises(SetupValidationError, match="profile"):
            save(s, "/dev/null")

    def test_bad_tls_version_raises(self) -> None:
        s = Iec104Setup(connection=ConnectionParams(tls=TlsConfig(enabled=True, version="1.0")))
        with pytest.raises(SetupValidationError, match=r"tls\.version"):
            save(s, "/dev/null")


class TestVersionHandling:
    def test_future_version_raises(self, tmp_path: Path) -> None:
        dest = tmp_path / "s.json"
        dest.write_text(json.dumps({"version": CURRENT_VERSION + 1, "name": "x"}), encoding="utf-8")
        with pytest.raises(SetupVersionError) as exc_info:
            load(dest)
        assert exc_info.value.found == CURRENT_VERSION + 1

    def test_missing_version_defaults_to_1(self, tmp_path: Path) -> None:
        dest = tmp_path / "s.json"
        dest.write_text(json.dumps({"name": "no-version"}), encoding="utf-8")
        s = load(dest)
        assert s.name == "no-version"

    def test_non_dict_root_raises(self, tmp_path: Path) -> None:
        dest = tmp_path / "s.json"
        dest.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(SetupValidationError):
            load(dest)

    def test_bad_json_raises(self, tmp_path: Path) -> None:
        dest = tmp_path / "s.json"
        dest.write_text("not json{{{", encoding="utf-8")
        import json as json_mod

        with pytest.raises(json_mod.JSONDecodeError):
            load(dest)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(OSError):
            load(tmp_path / "nonexistent.json")


class TestVendorProfile:
    def test_vendor_profile_roundtrips(self, tmp_path: Path) -> None:
        s = Iec104Setup(vendor_profile="abb_rtu560")
        dest = tmp_path / "s.json"
        save(s, dest)
        loaded = load(dest)
        assert loaded.vendor_profile == "abb_rtu560"

    def test_default_vendor_profile_is_generic(self, tmp_path: Path) -> None:
        dest = tmp_path / "s.json"
        save(Iec104Setup(), dest)
        loaded = load(dest)
        assert loaded.vendor_profile == "generic"
