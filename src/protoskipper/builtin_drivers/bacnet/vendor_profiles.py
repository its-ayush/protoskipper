# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet vendor profile library.

Vendor profiles encode device-specific quirks, proprietary object/property
definitions, and sensible default timeouts for known BACnet manufacturers.

A profile is a YAML file in the ``vendor_profiles/`` sub-directory alongside
this module.  Each file is loaded once (``lru_cache``) and returned as a
:class:`VendorProfile` dataclass.

Usage::

    from protoskipper.builtin_drivers.bacnet.vendor_profiles import (
        get_profile,
        load_vendor_profiles,
    )

    profile = get_profile(vendor_id=5)       # JCI Metasys
    profile = get_profile(profile_id="generic")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_logger = logging.getLogger(__name__)

_PROFILES_DIR = Path(__file__).parent / "vendor_profiles"


@dataclass(frozen=True)
class ProprietaryObjectType:
    type_id: int
    name: str
    description: str = ""


@dataclass(frozen=True)
class ProprietaryProperty:
    property_id: int
    name: str
    object_types: tuple[str, ...] = field(default_factory=tuple)
    data_type: str = "any"
    description: str = ""


@dataclass(frozen=True)
class VendorProfile:
    """Vendor-specific BACnet device profile."""

    id: str
    display_name: str
    vendor_id: int | None = None  #: ASHRAE BACnet vendor ID; None = generic
    quirks: tuple[str, ...] = field(default_factory=tuple)
    proprietary_object_types: tuple[ProprietaryObjectType, ...] = field(default_factory=tuple)
    proprietary_properties: tuple[ProprietaryProperty, ...] = field(default_factory=tuple)
    default_apdu_timeout_ms: int = 6000
    rpm_batch_size: int = 16
    cov_lifetime_s: int = 300
    segmentation_supported: bool = True
    notes: str = ""

    # ------------------------------------------------------------------
    # Quirk helpers
    # ------------------------------------------------------------------

    def has_quirk(self, quirk: str) -> bool:
        return quirk in self.quirks

    # Well-known quirk tags (informational)
    # "no-rpm"          — device does not support ReadPropertyMultiple
    # "slow-rpm"        — RPM is unreliable; prefer individual RP
    # "no-cov"          — COV subscriptions not supported
    # "no-segmentation" — segmentation rejected
    # "reboot-on-cov-cancel" — device reboots if SubscribeCOV is cancelled
    # "priority-16-only" — only priority 16 (manual-life-safety) accepted
    # "proprietary-units" — engineering units use proprietary numeric codes
    # "skip-device-rpm" — avoid RPM on device object on first connect


@lru_cache(maxsize=1)
def load_vendor_profiles() -> dict[str, VendorProfile]:
    """Load all ``*.yaml`` files from the vendor_profiles directory.

    Returns a dict keyed by profile ``id``.  Logs a warning for malformed
    files and skips them — it does not raise.  Always succeeds (falls back to
    the built-in generic profile).

    ``yaml`` is an optional dependency (``pyyaml``).  If not installed, all
    profiles in the directory are silently skipped and only the hard-coded
    generic profile is returned.
    """
    profiles: dict[str, VendorProfile] = {}

    try:
        import yaml
    except ImportError:  # pragma: no cover
        _logger.warning(
            "pyyaml not installed; BACnet vendor profiles unavailable — using generic fallback only"
        )
        yaml = None  # type: ignore[assignment]

    if yaml is not None:
        for path in sorted(_PROFILES_DIR.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("top-level must be a YAML mapping")
                profile = _parse_profile(data, path.stem)
                profiles[profile.id] = profile
            except Exception as exc:
                _logger.warning("BACnet vendor profile %s skipped: %s", path.name, exc)

    # Ensure generic always present
    if "generic" not in profiles:
        profiles["generic"] = _default_generic_profile()

    return profiles


def get_profile(
    vendor_id: int | None = None,
    profile_id: str | None = None,
) -> VendorProfile:
    """Look up a vendor profile by numeric vendor-id or string profile-id.

    Falls back to the ``"generic"`` profile if no match is found.
    """
    profiles = load_vendor_profiles()

    if profile_id and profile_id in profiles:
        return profiles[profile_id]

    if vendor_id is not None:
        for p in profiles.values():
            if p.vendor_id == vendor_id:
                return p

    return profiles["generic"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_profile(data: dict, stem: str) -> VendorProfile:
    profile_id = data.get("id", stem)
    quirks = tuple(data.get("quirks") or [])
    po_types = tuple(
        ProprietaryObjectType(
            type_id=int(d["type_id"]),
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
        )
        for d in (data.get("proprietary_object_types") or [])
    )
    prop_props = tuple(
        ProprietaryProperty(
            property_id=int(d["property_id"]),
            name=str(d.get("name", "")),
            object_types=tuple(d.get("object_types") or []),
            data_type=str(d.get("data_type", "any")),
            description=str(d.get("description", "")),
        )
        for d in (data.get("proprietary_properties") or [])
    )
    vendor_id_raw = data.get("vendor_id")
    return VendorProfile(
        id=profile_id,
        display_name=str(data.get("display_name", profile_id)),
        vendor_id=int(vendor_id_raw) if vendor_id_raw is not None else None,
        quirks=quirks,
        proprietary_object_types=po_types,
        proprietary_properties=prop_props,
        default_apdu_timeout_ms=int(data.get("default_apdu_timeout_ms", 6000)),
        rpm_batch_size=int(data.get("rpm_batch_size", 16)),
        cov_lifetime_s=int(data.get("cov_lifetime_s", 300)),
        segmentation_supported=bool(data.get("segmentation_supported", True)),
        notes=str(data.get("notes", "")),
    )


def _default_generic_profile() -> VendorProfile:
    return VendorProfile(
        id="generic",
        display_name="Generic BACnet Device",
        vendor_id=None,
        quirks=(),
        default_apdu_timeout_ms=6000,
        rpm_batch_size=16,
        cov_lifetime_s=300,
        segmentation_supported=True,
    )
