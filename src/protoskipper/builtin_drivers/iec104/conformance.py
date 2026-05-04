# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 conformance profile loader (P4.G.4 of IEC104_PLAN.md).

Each profile is a YAML file in the ``conformance_profiles/`` sub-directory
that declares a list of test cases with their labels, descriptions, mandatory
flag, timeout, and the safety profiles they are allowed to run under.

The loader uses the same soft-import + skip-bad-file pattern as
:mod:`protoskipper.builtin_drivers.iec104.vendor_profiles`.

Usage::

    from protoskipper.builtin_drivers.iec104.conformance import (
        load_conformance_profiles,
        ConformanceProfile,
        ConformanceTest,
    )
    profiles = load_conformance_profiles()
    for p in profiles.values():
        print(p.display_name, [t.key for t in p.tests])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_PROFILES_DIR = Path(__file__).parent / "conformance_profiles"

_ALL_PROFILES_ALLOWED = ("lab", "commissioning", "production")


@dataclass(frozen=True)
class ConformanceTest:
    """One test case within a conformance profile."""

    key: str
    label: str
    description: str
    timeout_s: int = 30
    mandatory: bool = True
    profiles_allowed: tuple[str, ...] = field(default_factory=lambda: _ALL_PROFILES_ALLOWED)  # type: ignore[assignment]


@dataclass(frozen=True)
class ConformanceProfile:
    """A named collection of conformance tests."""

    id: str
    display_name: str
    direction: str = "master"  # "master" | "slave"
    edition: str = "2.0+A1"
    profiles_allowed: tuple[str, ...] = field(default_factory=lambda: _ALL_PROFILES_ALLOWED)  # type: ignore[assignment]
    tests: tuple[ConformanceTest, ...] = field(default_factory=tuple)


def _parse_test(  # function name + sig just exceeds 100
    raw: dict[str, Any], profile_profiles_allowed: tuple[str, ...]
) -> ConformanceTest | None:
    key = raw.get("key")
    label = raw.get("label")
    if not key or not label:
        return None
    # Test can override profiles_allowed relative to the profile-level setting.
    test_pa = raw.get("profiles_allowed")
    if isinstance(test_pa, list) and test_pa:
        pa: tuple[str, ...] = tuple(str(x) for x in test_pa)
    else:
        pa = profile_profiles_allowed
    return ConformanceTest(
        key=str(key),
        label=str(label),
        description=str(raw.get("description", "")).strip(),
        timeout_s=int(raw.get("timeout_s", 30)),
        mandatory=bool(raw.get("mandatory", True)),
        profiles_allowed=pa,
    )


def _parse_profile(raw: dict[str, Any]) -> ConformanceProfile | None:
    pid = raw.get("id")
    display_name = raw.get("display_name")
    if not pid or not display_name:
        return None
    pa_raw = raw.get("profiles_allowed", list(_ALL_PROFILES_ALLOWED))
    pa: tuple[str, ...] = (
        tuple(str(x) for x in pa_raw) if isinstance(pa_raw, list) else _ALL_PROFILES_ALLOWED
    )

    tests: list[ConformanceTest] = []
    for t_raw in raw.get("tests", []):
        if not isinstance(t_raw, dict):
            continue
        t = _parse_test(t_raw, pa)
        if t is not None:
            tests.append(t)

    return ConformanceProfile(
        id=str(pid),
        display_name=str(display_name),
        direction=str(raw.get("direction", "master")),
        edition=str(raw.get("edition", "2.0+A1")),
        profiles_allowed=pa,
        tests=tuple(tests),
    )


@lru_cache(maxsize=1)
def load_conformance_profiles() -> dict[str, ConformanceProfile]:
    """Load all YAML conformance profiles from the ``conformance_profiles/`` directory.

    Returns a ``dict[id, ConformanceProfile]``.  Malformed files are logged
    and skipped.  The result is ``lru_cache``-d; call
    ``load_conformance_profiles.cache_clear()`` in tests that need a fresh load.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        _logger.warning("PyYAML not installed — conformance profiles unavailable.")
        return {}

    if not _PROFILES_DIR.is_dir():
        _logger.warning("Conformance profiles directory not found: %s", _PROFILES_DIR)
        return {}

    profiles: dict[str, ConformanceProfile] = {}
    for yaml_path in sorted(_PROFILES_DIR.glob("*.yaml")):
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Root must be a YAML mapping")
            profile = _parse_profile(raw)
            if profile is None:
                raise ValueError("Missing required fields 'id' and/or 'display_name'")
            profiles[profile.id] = profile
        except Exception as exc:
            _logger.warning("Skipping bad conformance profile %s: %s", yaml_path.name, exc)

    _logger.debug("Loaded %d conformance profiles from %s", len(profiles), _PROFILES_DIR)
    return profiles
