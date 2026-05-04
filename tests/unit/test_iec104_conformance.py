# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the IEC 104 conformance profile loader."""

from __future__ import annotations

from pathlib import Path

import pytest


def _fresh_load(monkeypatch: pytest.MonkeyPatch, profiles_dir: Path) -> dict:  # type: ignore[type-arg]
    """Load conformance profiles from *profiles_dir*, bypassing the lru_cache."""
    from protoskipper.builtin_drivers.iec104 import conformance as mod

    monkeypatch.setattr(mod, "_PROFILES_DIR", profiles_dir)
    mod.load_conformance_profiles.cache_clear()
    result = mod.load_conformance_profiles()
    mod.load_conformance_profiles.cache_clear()
    return result


class TestConformanceProfileLoader:
    def test_loads_bundled_profiles(self) -> None:
        pytest.importorskip("yaml")
        from protoskipper.builtin_drivers.iec104.conformance import load_conformance_profiles

        load_conformance_profiles.cache_clear()
        profiles = load_conformance_profiles()
        load_conformance_profiles.cache_clear()

        assert len(profiles) >= 4, f"Expected ≥ 4 bundled profiles, got {len(profiles)}"
        assert "master_ed2_2016" in profiles
        assert "slave_ed2_2016" in profiles
        assert "gi_conformance" in profiles
        assert "command_conformance" in profiles

    def test_master_profile_has_mandatory_tests(self) -> None:
        pytest.importorskip("yaml")
        from protoskipper.builtin_drivers.iec104.conformance import load_conformance_profiles

        load_conformance_profiles.cache_clear()
        profiles = load_conformance_profiles()
        load_conformance_profiles.cache_clear()

        p = profiles["master_ed2_2016"]
        mandatory = [t for t in p.tests if t.mandatory]
        assert len(mandatory) >= 3, "Master profile must have ≥ 3 mandatory tests"

    def test_test_keys_are_unique_within_profile(self) -> None:
        pytest.importorskip("yaml")
        from protoskipper.builtin_drivers.iec104.conformance import load_conformance_profiles

        load_conformance_profiles.cache_clear()
        profiles = load_conformance_profiles()
        load_conformance_profiles.cache_clear()

        for pid, p in profiles.items():
            keys = [t.key for t in p.tests]
            assert len(keys) == len(set(keys)), f"Duplicate test keys in profile {pid}"

    def test_custom_profile_parsed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("yaml")
        yaml_content = """
id: custom_test
display_name: "Custom Test Profile"
direction: slave
edition: "1.0"
profiles_allowed:
  - lab
tests:
  - key: foo
    label: "Foo test"
    description: "Tests foo"
    timeout_s: 5
    mandatory: true
  - key: bar
    label: "Bar test"
    description: "Tests bar"
    timeout_s: 10
    mandatory: false
    profiles_allowed:
      - lab
"""
        (tmp_path / "custom_test.yaml").write_text(yaml_content, encoding="utf-8")
        profiles = _fresh_load(monkeypatch, tmp_path)

        assert "custom_test" in profiles
        p = profiles["custom_test"]
        assert p.display_name == "Custom Test Profile"
        assert p.direction == "slave"
        assert p.edition == "1.0"
        assert len(p.tests) == 2
        assert p.tests[0].key == "foo"
        assert p.tests[0].mandatory is True
        assert p.tests[1].key == "bar"
        assert p.tests[1].mandatory is False

    def test_bad_yaml_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("yaml")
        (tmp_path / "good.yaml").write_text(
            "id: good\ndisplay_name: Good\ntests: []\n", encoding="utf-8"
        )
        (tmp_path / "bad.yaml").write_text("{{{{: invalid yaml", encoding="utf-8")
        profiles = _fresh_load(monkeypatch, tmp_path)

        assert "good" in profiles
        assert "bad" not in profiles

    def test_missing_id_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pytest.importorskip("yaml")
        (tmp_path / "noid.yaml").write_text("display_name: No ID\ntests: []\n", encoding="utf-8")
        profiles = _fresh_load(monkeypatch, tmp_path)
        assert "noid" not in profiles

    def test_missing_directory_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("yaml")
        missing = tmp_path / "does_not_exist"
        profiles = _fresh_load(monkeypatch, missing)
        assert profiles == {}

    def test_test_profiles_allowed_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("yaml")
        yaml_content = """
id: override_test
display_name: "Override Test"
profiles_allowed:
  - lab
  - commissioning
tests:
  - key: lab_only
    label: "Lab only"
    description: "Only in lab"
    profiles_allowed:
      - lab
  - key: any_profile
    label: "Any"
    description: "Inherits profile-level"
"""
        (tmp_path / "override_test.yaml").write_text(yaml_content, encoding="utf-8")
        profiles = _fresh_load(monkeypatch, tmp_path)
        p = profiles["override_test"]
        lab_only = next(t for t in p.tests if t.key == "lab_only")
        any_profile = next(t for t in p.tests if t.key == "any_profile")
        assert lab_only.profiles_allowed == ("lab",)
        assert "commissioning" in any_profile.profiles_allowed

    def test_no_pyyaml_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        from protoskipper.builtin_drivers.iec104 import conformance as mod

        original = sys.modules.get("yaml")
        sys.modules["yaml"] = None  # type: ignore[assignment]
        mod.load_conformance_profiles.cache_clear()
        try:
            profiles = mod.load_conformance_profiles()
        finally:
            if original is None:
                sys.modules.pop("yaml", None)
            else:
                sys.modules["yaml"] = original
            mod.load_conformance_profiles.cache_clear()

        assert profiles == {}

    def test_profile_is_frozen(self) -> None:
        pytest.importorskip("yaml")
        from protoskipper.builtin_drivers.iec104.conformance import ConformanceProfile

        p = ConformanceProfile(id="x", display_name="X")
        with pytest.raises((AttributeError, TypeError)):
            p.id = "y"  # type: ignore[misc]
