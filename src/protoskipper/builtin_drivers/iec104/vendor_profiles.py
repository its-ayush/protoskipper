# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 vendor profile loader (§5.16 of IEC104_PLAN.md).

Loads YAML files from the ``vendor_profiles/`` sub-directory next to this
module and returns a mapping of id → :class:`VendorProfile`.

Profiles pre-fill k, w, t0..t3, CA/IOA size and list known quirks so that
the NewConnectionDialog can offer a "Vendor Profile" dropdown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_PROFILES_DIR = Path(__file__).parent / "vendor_profiles"


@dataclass(frozen=True)
class VendorProfile:
    """Parsed IEC 104 vendor connection profile."""

    id: str
    display_name: str
    ca_size: int = 2
    ioa_size: int = 3
    cot_size: int = 2
    default_oa: int = 0
    k: int = 12
    w: int = 8
    t0: int = 30
    t1: int = 15
    t2: int = 10
    t3: int = 20
    quirks: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    def has_quirk(self, quirk: str) -> bool:
        return quirk in self.quirks


@lru_cache(maxsize=1)
def load_vendor_profiles() -> dict[str, VendorProfile]:
    """Load all YAML vendor profiles from the ``vendor_profiles/`` directory.

    Returns a dict keyed by profile id.  Bad YAML files are logged and skipped.
    The result is cached via ``lru_cache``; call
    ``load_vendor_profiles.cache_clear()`` in tests that mutate the directory.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        _logger.warning(
            "PyYAML not installed — vendor profiles unavailable. Install with: pip install pyyaml"
        )
        return {}

    profiles: dict[str, VendorProfile] = {}
    if not _PROFILES_DIR.is_dir():
        _logger.warning("Vendor profiles directory not found: %s", _PROFILES_DIR)
        return profiles

    for yaml_file in sorted(_PROFILES_DIR.glob("*.yaml")):
        try:
            raw: dict[str, Any] = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("Expected a YAML mapping at the root")
            profile_id = str(raw.get("id") or yaml_file.stem)
            quirks_raw = raw.get("quirks", []) or []
            profile = VendorProfile(
                id=profile_id,
                display_name=str(raw.get("display_name", profile_id)),
                ca_size=int(raw.get("ca_size", 2)),
                ioa_size=int(raw.get("ioa_size", 3)),
                cot_size=int(raw.get("cot_size", 2)),
                default_oa=int(raw.get("default_oa", 0)),
                k=int(raw.get("k", 12)),
                w=int(raw.get("w", 8)),
                t0=int(raw.get("t0", 30)),
                t1=int(raw.get("t1", 15)),
                t2=int(raw.get("t2", 10)),
                t3=int(raw.get("t3", 20)),
                quirks=tuple(str(q) for q in quirks_raw),
                notes=str(raw.get("notes", "")),
            )
            profiles[profile_id] = profile
        except Exception as exc:
            _logger.warning("Skipping vendor profile %s: %s", yaml_file.name, exc)

    _logger.debug("Loaded %d vendor profile(s)", len(profiles))
    return profiles
