# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for IEC 104 vendor profile loading (§5.16 of IEC104_PLAN.md).

Tests cover: YAML parsing, dataclass structure, quirk lookup,
and graceful handling of missing PyYAML or bad YAML files.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_load(monkeypatch: pytest.MonkeyPatch, profiles_dir: Path):
    """Import the loader with a patched profiles directory."""
    from protoskipper.builtin_drivers.iec104 import vendor_profiles as vp_mod

    # Clear the lru_cache so directory changes are picked up.
    vp_mod.load_vendor_profiles.cache_clear()
    monkeypatch.setattr(vp_mod, "_PROFILES_DIR", profiles_dir)
    return vp_mod.load_vendor_profiles()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestVendorProfileLoader:
    def test_loads_bundled_profiles(self) -> None:
        """The bundled YAML files should load without errors (if pyyaml is installed)."""
        pytest.importorskip("yaml")
        from protoskipper.builtin_drivers.iec104.vendor_profiles import load_vendor_profiles

        load_vendor_profiles.cache_clear()
        profiles = load_vendor_profiles()
        assert "generic" in profiles
        assert "abb_rtu560" in profiles
        assert "siemens" in profiles
        assert "schneider" in profiles
        assert "ge" in profiles

    def test_generic_profile_defaults(self) -> None:
        pytest.importorskip("yaml")
        from protoskipper.builtin_drivers.iec104.vendor_profiles import load_vendor_profiles

        load_vendor_profiles.cache_clear()
        p = load_vendor_profiles()["generic"]
        assert p.ca_size == 2
        assert p.ioa_size == 3
        assert p.cot_size == 2
        assert p.k == 12
        assert p.w == 8
        assert p.t0 == 30
        assert p.t1 == 15

    def test_abb_profile_has_quirks(self) -> None:
        pytest.importorskip("yaml")
        from protoskipper.builtin_drivers.iec104.vendor_profiles import load_vendor_profiles

        load_vendor_profiles.cache_clear()
        p = load_vendor_profiles()["abb_rtu560"]
        assert p.has_quirk("no_testfr_con")
        assert not p.has_quirk("sbo_mandatory")

    def test_custom_profile_parsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("yaml")
        yaml_content = textwrap.dedent("""
            id: test_vendor
            display_name: "Test Vendor X"
            ca_size: 2
            ioa_size: 3
            cot_size: 2
            default_oa: 1
            k: 8
            w: 6
            t0: 60
            t1: 20
            t2: 8
            t3: 25
            quirks:
              - long_gi_timeout
              - no_ci
            notes: "Custom vendor profile for testing."
        """)
        (tmp_path / "test_vendor.yaml").write_text(yaml_content, encoding="utf-8")
        profiles = _fresh_load(monkeypatch, tmp_path)
        assert "test_vendor" in profiles
        p = profiles["test_vendor"]
        assert p.display_name == "Test Vendor X"
        assert p.k == 8
        assert p.default_oa == 1
        assert p.has_quirk("long_gi_timeout")
        assert p.has_quirk("no_ci")
        assert p.notes.strip() == "Custom vendor profile for testing."

    def test_bad_yaml_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("yaml")
        (tmp_path / "bad.yaml").write_text("{{{{not: valid: yaml", encoding="utf-8")
        (tmp_path / "good.yaml").write_text("id: good\ndisplay_name: Good\n", encoding="utf-8")
        profiles = _fresh_load(monkeypatch, tmp_path)
        # Bad file is silently skipped; good file is loaded.
        assert "good" in profiles
        assert "bad" not in profiles

    def test_missing_directory_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("yaml")
        missing = tmp_path / "does_not_exist"
        profiles = _fresh_load(monkeypatch, missing)
        assert profiles == {}

    def test_vendor_profile_is_frozen(self) -> None:
        pytest.importorskip("yaml")
        from protoskipper.builtin_drivers.iec104.vendor_profiles import VendorProfile

        p = VendorProfile(id="x", display_name="X")
        with pytest.raises((AttributeError, TypeError)):  # FrozenInstanceError
            p.k = 99  # type: ignore[misc]

    def test_has_quirk_returns_false_for_unknown(self) -> None:
        pytest.importorskip("yaml")
        from protoskipper.builtin_drivers.iec104.vendor_profiles import VendorProfile

        p = VendorProfile(id="x", display_name="X", quirks=("foo",))
        assert p.has_quirk("bar") is False
        assert p.has_quirk("foo") is True

    def test_profile_ids_are_strings(self) -> None:
        pytest.importorskip("yaml")
        from protoskipper.builtin_drivers.iec104.vendor_profiles import load_vendor_profiles

        load_vendor_profiles.cache_clear()
        for pid, profile in load_vendor_profiles().items():
            assert isinstance(pid, str)
            assert isinstance(profile.display_name, str)

    def test_no_pyyaml_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When PyYAML is not installed, the loader returns {} gracefully."""
        import sys

        # Temporarily hide pyyaml from the import system.
        original_yaml = sys.modules.get("yaml")
        sys.modules["yaml"] = None  # type: ignore[assignment]
        try:
            from protoskipper.builtin_drivers.iec104 import vendor_profiles as vp_mod

            vp_mod.load_vendor_profiles.cache_clear()
            profiles = vp_mod.load_vendor_profiles()
            assert profiles == {}
        finally:
            if original_yaml is not None:
                sys.modules["yaml"] = original_yaml
            else:
                del sys.modules["yaml"]
            from protoskipper.builtin_drivers.iec104 import vendor_profiles as vp_mod2

            vp_mod2.load_vendor_profiles.cache_clear()
