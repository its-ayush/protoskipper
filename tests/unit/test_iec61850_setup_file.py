# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for IEC 61850 setup file save/load (P8.I.3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
from protoskipper_iec61850.setup_file import (
    EMPTY_SETUP,
    SCHEMA_VERSION,
    SetupFileError,
    load_setup,
    save_setup,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL: dict = {
    "version": SCHEMA_VERSION,
    "connections": [],
    "scl_path": "",
    "goose_subscriptions": [],
    "goose_publisher": None,
}

_FULL: dict = {
    "version": SCHEMA_VERSION,
    "connections": [
        {"host": "192.168.1.1", "port": 102, "ap_title": "1,3,9999,33"},
        {"host": "192.168.1.2", "port": 102, "ap_title": ""},
    ],
    "scl_path": "/project/simpleIO.icd",
    "goose_subscriptions": [
        {"iface": "eth0", "go_cb_refs": ["simpleIO/LLN0$GO$gcb01"]},
    ],
    "goose_publisher": {
        "iface": "eth0",
        "go_cb_ref": "simpleIO/LLN0$GO$gcb01",
        "dataset_ref": "simpleIO/LLN0$DataSet1",
        "goose_id": "simpleIO/LLN0$GO$gcb01",
        "app_id": 0x1000,
        "vlan_id": 0,
        "vlan_priority": 4,
        "conf_rev": 1,
    },
}


# ---------------------------------------------------------------------------
# EMPTY_SETUP
# ---------------------------------------------------------------------------


class TestEmptySetup:
    def test_has_correct_version(self) -> None:
        assert EMPTY_SETUP["version"] == SCHEMA_VERSION

    def test_empty_lists(self) -> None:
        assert EMPTY_SETUP["connections"] == []
        assert EMPTY_SETUP["goose_subscriptions"] == []
        assert EMPTY_SETUP["goose_publisher"] is None
        assert EMPTY_SETUP["scl_path"] == ""


# ---------------------------------------------------------------------------
# save_setup
# ---------------------------------------------------------------------------


class TestSaveSetup:
    def test_minimal_roundtrip(self, tmp_path: Path) -> None:
        p = tmp_path / "test.iec61850-setup.json"
        save_setup(p, _MINIMAL)
        assert p.exists()
        raw = json.loads(p.read_text())
        assert raw["version"] == SCHEMA_VERSION

    def test_full_roundtrip(self, tmp_path: Path) -> None:
        p = tmp_path / "full.iec61850-setup.json"
        save_setup(p, _FULL)
        raw = json.loads(p.read_text())
        assert raw["connections"][0]["host"] == "192.168.1.1"
        assert raw["goose_publisher"]["app_id"] == 0x1000

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        p = tmp_path / "deep" / "nested" / "setup.iec61850-setup.json"
        save_setup(p, _MINIMAL)
        assert p.exists()

    def test_file_ends_with_newline(self, tmp_path: Path) -> None:
        p = tmp_path / "nl.iec61850-setup.json"
        save_setup(p, _MINIMAL)
        assert p.read_text().endswith("\n")

    def test_rejects_bad_data(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.iec61850-setup.json"
        with pytest.raises(SetupFileError):
            save_setup(p, {"version": 99})  # wrong version

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "existing.iec61850-setup.json"
        save_setup(p, _MINIMAL)
        modified = dict(_MINIMAL, scl_path="/new.icd")
        save_setup(p, modified)
        raw = json.loads(p.read_text())
        assert raw["scl_path"] == "/new.icd"


# ---------------------------------------------------------------------------
# load_setup
# ---------------------------------------------------------------------------


class TestLoadSetup:
    def _write(self, tmp_path: Path, data: dict, name: str = "setup.json") -> Path:
        p = tmp_path / name
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_minimal_loads(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, _MINIMAL)
        result = load_setup(p)
        assert result["version"] == SCHEMA_VERSION
        assert result["connections"] == []

    def test_full_loads(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, _FULL)
        result = load_setup(p)
        assert result["connections"][0]["host"] == "192.168.1.1"
        assert result["goose_publisher"]["app_id"] == 0x1000

    def test_unknown_keys_ignored(self, tmp_path: Path) -> None:
        data = dict(_MINIMAL, future_key="value")
        p = self._write(tmp_path, data)
        result = load_setup(p)
        assert "future_key" not in result

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SetupFileError, match="Cannot read"):
            load_setup(tmp_path / "nonexistent.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        with pytest.raises(SetupFileError, match="not valid JSON"):
            load_setup(p)

    def test_not_an_object_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "array.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(SetupFileError, match="JSON object"):
            load_setup(p)

    def test_wrong_version_raises(self, tmp_path: Path) -> None:
        data = dict(_MINIMAL, version=99)
        p = self._write(tmp_path, data)
        with pytest.raises(SetupFileError, match="Unsupported setup file version"):
            load_setup(p)

    def test_missing_version_raises(self, tmp_path: Path) -> None:
        data = {k: v for k, v in _MINIMAL.items() if k != "version"}
        p = self._write(tmp_path, data)
        with pytest.raises(SetupFileError, match="version"):
            load_setup(p)

    def test_missing_connections_raises(self, tmp_path: Path) -> None:
        data = {k: v for k, v in _MINIMAL.items() if k != "connections"}
        p = self._write(tmp_path, data)
        with pytest.raises(SetupFileError, match="connections"):
            load_setup(p)


# ---------------------------------------------------------------------------
# Connection validation
# ---------------------------------------------------------------------------


class TestConnectionValidation:
    def _load_with_connections(self, tmp_path: Path, connections: list) -> dict:
        data = dict(_MINIMAL, connections=connections)
        p = tmp_path / "c.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return load_setup(p)

    def test_valid_connection(self, tmp_path: Path) -> None:
        result = self._load_with_connections(
            tmp_path, [{"host": "10.0.0.1", "port": 102, "ap_title": ""}]
        )
        assert result["connections"][0]["port"] == 102

    def test_port_out_of_range_raises(self, tmp_path: Path) -> None:
        data = dict(_MINIMAL, connections=[{"host": "10.0.0.1", "port": 99999, "ap_title": ""}])
        p = tmp_path / "bad_port.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(SetupFileError, match="port"):
            load_setup(p)

    def test_missing_host_raises(self, tmp_path: Path) -> None:
        data = dict(_MINIMAL, connections=[{"port": 102}])
        p = tmp_path / "no_host.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(SetupFileError, match="host"):
            load_setup(p)

    def test_ap_title_defaults_to_empty(self, tmp_path: Path) -> None:
        result = self._load_with_connections(tmp_path, [{"host": "10.0.0.1", "port": 102}])
        assert result["connections"][0]["ap_title"] == ""

    def test_connection_not_object_raises(self, tmp_path: Path) -> None:
        data = dict(_MINIMAL, connections=["not_an_object"])
        p = tmp_path / "bad_conn.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(SetupFileError, match="object"):
            load_setup(p)


# ---------------------------------------------------------------------------
# GOOSE subscription validation
# ---------------------------------------------------------------------------


class TestGooseSubscriptionValidation:
    def _load_with_subs(self, tmp_path: Path, subs: list) -> dict:
        data = dict(_MINIMAL, goose_subscriptions=subs)
        p = tmp_path / "s.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return load_setup(p)

    def test_valid_subscription(self, tmp_path: Path) -> None:
        result = self._load_with_subs(
            tmp_path,
            [{"iface": "eth0", "go_cb_refs": ["LD/LLN0$GO$gcb01"]}],
        )
        assert result["goose_subscriptions"][0]["iface"] == "eth0"

    def test_missing_iface_raises(self, tmp_path: Path) -> None:
        data = dict(_MINIMAL, goose_subscriptions=[{"go_cb_refs": []}])
        p = tmp_path / "no_iface.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(SetupFileError, match="iface"):
            load_setup(p)

    def test_non_string_go_cb_ref_raises(self, tmp_path: Path) -> None:
        data = dict(
            _MINIMAL,
            goose_subscriptions=[{"iface": "eth0", "go_cb_refs": [123]}],
        )
        p = tmp_path / "bad_ref.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(SetupFileError, match="string"):
            load_setup(p)


# ---------------------------------------------------------------------------
# GOOSE publisher validation
# ---------------------------------------------------------------------------


class TestGoosePublisherValidation:
    _VALID_PUB: ClassVar[dict] = {
        "iface": "eth0",
        "go_cb_ref": "simpleIO/LLN0$GO$gcb01",
        "dataset_ref": "simpleIO/LLN0$DataSet1",
        "goose_id": "simpleIO/LLN0$GO$gcb01",
        "app_id": 0x1000,
        "vlan_id": 0,
        "vlan_priority": 4,
        "conf_rev": 1,
    }

    def _load_with_pub(self, tmp_path: Path, pub) -> dict:  # type: ignore[no-untyped-def]
        data = dict(_MINIMAL, goose_publisher=pub)
        p = tmp_path / "pub.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return load_setup(p)

    def test_valid_publisher(self, tmp_path: Path) -> None:
        result = self._load_with_pub(tmp_path, self._VALID_PUB)
        assert result["goose_publisher"]["app_id"] == 0x1000

    def test_null_publisher_allowed(self, tmp_path: Path) -> None:
        result = self._load_with_pub(tmp_path, None)
        assert result["goose_publisher"] is None

    def test_app_id_out_of_range_raises(self, tmp_path: Path) -> None:
        pub = dict(self._VALID_PUB, app_id=0x4000)
        with pytest.raises(SetupFileError, match="app_id"):
            self._load_with_pub(tmp_path, pub)

    def test_vlan_id_out_of_range_raises(self, tmp_path: Path) -> None:
        pub = dict(self._VALID_PUB, vlan_id=4096)
        with pytest.raises(SetupFileError, match="vlan_id"):
            self._load_with_pub(tmp_path, pub)

    def test_vlan_priority_out_of_range_raises(self, tmp_path: Path) -> None:
        pub = dict(self._VALID_PUB, vlan_priority=8)
        with pytest.raises(SetupFileError, match="vlan_priority"):
            self._load_with_pub(tmp_path, pub)

    def test_defaults_applied(self, tmp_path: Path) -> None:
        """vlan_id, vlan_priority, and conf_rev have defaults."""
        pub = {
            k: v
            for k, v in self._VALID_PUB.items()
            if k not in ("vlan_id", "vlan_priority", "conf_rev")
        }
        result = self._load_with_pub(tmp_path, pub)
        p = result["goose_publisher"]
        assert p["vlan_id"] == 0
        assert p["vlan_priority"] == 4
        assert p["conf_rev"] == 1

    def test_publisher_not_object_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SetupFileError, match="object"):
            self._load_with_pub(tmp_path, "not_an_object")
