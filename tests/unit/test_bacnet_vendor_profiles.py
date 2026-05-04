# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the BACnet vendor profile library."""

from __future__ import annotations

from protoskipper.builtin_drivers.bacnet.vendor_profiles import (
    VendorProfile,
    get_profile,
    load_vendor_profiles,
)


class TestVendorProfileLoading:
    def test_all_13_profiles_load(self) -> None:
        profiles = load_vendor_profiles()
        expected_ids = {
            "generic",
            "jci_metasys",
            "honeywell_webbased",
            "trane_tracer",
            "siemens_desigo",
            "abb_eclipse",
            "schneider_ecostruxure",
            "distech_ecb",
            "reliable_controls",
            "kmc_controls",
            "delta_controls",
            "alc_webctrl",
            "tridium_niagara",
        }
        assert expected_ids.issubset(set(profiles.keys()))

    def test_generic_profile_always_present(self) -> None:
        profiles = load_vendor_profiles()
        assert "generic" in profiles

    def test_profile_is_vendor_profile_dataclass(self) -> None:
        profiles = load_vendor_profiles()
        for p in profiles.values():
            assert isinstance(p, VendorProfile)

    def test_all_profiles_have_required_fields(self) -> None:
        profiles = load_vendor_profiles()
        for pid, p in profiles.items():
            assert p.id == pid or p.id  # id is non-empty
            assert p.display_name, f"Profile {pid} has empty display_name"
            assert p.default_apdu_timeout_ms > 0
            assert p.rpm_batch_size > 0
            assert p.cov_lifetime_s > 0


class TestGetProfile:
    def test_get_by_vendor_id_jci(self) -> None:
        p = get_profile(vendor_id=5)
        assert p.id == "jci_metasys"
        assert p.vendor_id == 5

    def test_get_by_vendor_id_siemens(self) -> None:
        p = get_profile(vendor_id=4)
        assert p.id == "siemens_desigo"

    def test_get_by_vendor_id_honeywell(self) -> None:
        p = get_profile(vendor_id=11)
        assert p.id == "honeywell_webbased"

    def test_get_by_vendor_id_trane(self) -> None:
        p = get_profile(vendor_id=16)
        assert p.id == "trane_tracer"

    def test_get_by_profile_id(self) -> None:
        p = get_profile(profile_id="tridium_niagara")
        assert p.id == "tridium_niagara"
        assert p.vendor_id == 24

    def test_unknown_vendor_id_falls_back_to_generic(self) -> None:
        p = get_profile(vendor_id=99999)
        assert p.id == "generic"

    def test_no_args_returns_generic(self) -> None:
        p = get_profile()
        assert p.id == "generic"


class TestProfileQuirks:
    def test_trane_has_no_cov_quirk(self) -> None:
        p = get_profile(vendor_id=16)
        assert p.has_quirk("no-cov")

    def test_honeywell_has_no_segmentation_quirk(self) -> None:
        p = get_profile(vendor_id=11)
        assert p.has_quirk("no-segmentation")

    def test_tridium_has_reboot_on_cov_cancel(self) -> None:
        p = get_profile(profile_id="tridium_niagara")
        assert p.has_quirk("reboot-on-cov-cancel")

    def test_siemens_no_quirks(self) -> None:
        p = get_profile(vendor_id=4)
        assert not p.quirks

    def test_generic_no_quirks(self) -> None:
        p = get_profile()
        assert not p.quirks


class TestProprietaryExtensions:
    def test_jci_has_proprietary_properties(self) -> None:
        p = get_profile(vendor_id=5)
        assert len(p.proprietary_properties) > 0
        prop_ids = [pp.property_id for pp in p.proprietary_properties]
        assert 4001 in prop_ids

    def test_tridium_slot_path_property(self) -> None:
        p = get_profile(profile_id="tridium_niagara")
        slot_path = next(
            (pp for pp in p.proprietary_properties if pp.name == "NIA-Slot-Path"),
            None,
        )
        assert slot_path is not None
        assert "analog-value" in slot_path.object_types

    def test_siemens_has_proprietary_object_types(self) -> None:
        p = get_profile(vendor_id=4)
        assert len(p.proprietary_object_types) > 0


class TestProfileDefaults:
    def test_rpm_batch_sizes_reasonable(self) -> None:
        profiles = load_vendor_profiles()
        for p in profiles.values():
            assert 1 <= p.rpm_batch_size <= 100, f"{p.id} rpm_batch_size out of range"

    def test_apdu_timeouts_reasonable(self) -> None:
        profiles = load_vendor_profiles()
        for p in profiles.values():
            assert 1000 <= p.default_apdu_timeout_ms <= 60000, (
                f"{p.id} apdu_timeout out of range: {p.default_apdu_timeout_ms}"
            )
