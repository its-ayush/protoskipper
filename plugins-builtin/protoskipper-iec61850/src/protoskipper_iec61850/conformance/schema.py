# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Conformance profile schema — P8.F.1.

Defines the dataclasses that model a conformance test profile (loaded from
a YAML file).  Each profile describes a set of test cases for a given IEC
61850 edition and profile category.

YAML profile structure::

    profile_id: mms_ed21
    edition: "2.1"
    category: "MMS Client"
    description: "IEC 61850-8-1 Ed 2.1 MMS Client conformance tests"
    tests:
      - id: TC_MMS_001
        name: "GetNameList on IED root"
        description: "Server must respond to GetNameList with DomainSpecific scope"
        service: GetNameList
        preconditions:
          - "Server reachable on port 102"
        steps:
          - action: connect
          - action: GetNameList
            params:
              objectClass: Domain
              objectScope: DomainSpecific
          - action: assert_response
            params:
              status: success
              has_items: true
        severity: MANDATORY

Severity levels
---------------
* ``MANDATORY`` — failure means the device is non-conformant.
* ``OPTIONAL`` — informational; does not affect the overall result.
* ``CONDITIONAL`` — mandatory only when a particular feature is claimed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Severity(str, Enum):
    """Conformance test severity level."""

    MANDATORY = "MANDATORY"
    OPTIONAL = "OPTIONAL"
    CONDITIONAL = "CONDITIONAL"


@dataclass
class TestStep:
    """A single step within a conformance test case.

    Attributes
    ----------
    action:
        Action name (e.g. ``"connect"``, ``"GetNameList"``, ``"assert_response"``).
    params:
        Keyword parameters for the action.
    """

    action: str
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TestStep:
        return cls(action=str(d.get("action", "")), params=dict(d.get("params", {})))

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "params": self.params}


@dataclass
class ConformanceTestCase:
    """A single conformance test case.

    Attributes
    ----------
    test_id:
        Unique identifier, e.g. ``"TC_MMS_001"``.
    name:
        Short human-readable name.
    description:
        Longer description of what is tested.
    service:
        MMS / GOOSE / SV service under test.
    preconditions:
        List of precondition strings (informational).
    steps:
        Ordered list of :class:`TestStep` objects.
    severity:
        :class:`Severity` level.
    tags:
        Arbitrary tags for filtering / grouping.
    """

    test_id: str
    name: str
    description: str
    service: str
    preconditions: list[str] = field(default_factory=list)
    steps: list[TestStep] = field(default_factory=list)
    severity: Severity = Severity.MANDATORY
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ConformanceTestCase:
        return cls(
            test_id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            service=str(d.get("service", "")),
            preconditions=list(d.get("preconditions", [])),
            steps=[TestStep.from_dict(s) for s in d.get("steps", [])],
            severity=Severity(d.get("severity", Severity.MANDATORY)),
            tags=list(d.get("tags", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.test_id,
            "name": self.name,
            "description": self.description,
            "service": self.service,
            "preconditions": self.preconditions,
            "steps": [s.to_dict() for s in self.steps],
            "severity": self.severity.value,
            "tags": self.tags,
        }


@dataclass
class ConformanceProfile:
    """A conformance test profile (loaded from a YAML file).

    Attributes
    ----------
    profile_id:
        Unique profile identifier, e.g. ``"mms_ed21"``.
    edition:
        IEC 61850 edition string, e.g. ``"2.1"``.
    category:
        Profile category, e.g. ``"MMS Client"``.
    description:
        Human-readable description.
    tests:
        List of :class:`ConformanceTestCase` objects.
    """

    profile_id: str
    edition: str
    category: str
    description: str
    tests: list[ConformanceTestCase] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ConformanceProfile:
        return cls(
            profile_id=str(d.get("profile_id", "")),
            edition=str(d.get("edition", "")),
            category=str(d.get("category", "")),
            description=str(d.get("description", "")),
            tests=[ConformanceTestCase.from_dict(t) for t in d.get("tests", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "edition": self.edition,
            "category": self.category,
            "description": self.description,
            "tests": [t.to_dict() for t in self.tests],
        }

    @classmethod
    def load_yaml(cls, path: Path) -> ConformanceProfile:
        """Load a conformance profile from a YAML file.

        Parameters
        ----------
        path:
            Path to the YAML profile file.

        Raises
        ------
        FileNotFoundError:
            If *path* does not exist.
        ValueError:
            If the YAML is malformed or missing required fields.
        """
        import yaml  # lazy: not a hard dependency for core import

        if not path.exists():
            msg = f"Profile not found: {path}"
            raise FileNotFoundError(msg)
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            msg = f"Profile must be a YAML mapping, got {type(data).__name__}"
            raise ValueError(msg)
        return cls.from_dict(data)

    def mandatory_tests(self) -> list[ConformanceTestCase]:
        """Return only MANDATORY test cases."""
        return [t for t in self.tests if t.severity == Severity.MANDATORY]

    def by_service(self, service: str) -> list[ConformanceTestCase]:
        """Return test cases for the given service name."""
        return [t for t in self.tests if t.service == service]


# ---------------------------------------------------------------------------
# Built-in profile loader
# ---------------------------------------------------------------------------

_PROFILES_DIR = Path(__file__).parent / "profiles"


def list_builtin_profiles() -> list[str]:
    """Return profile IDs for all built-in YAML profiles."""
    if not _PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in _PROFILES_DIR.glob("*.yaml"))


def load_builtin_profile(profile_id: str) -> ConformanceProfile:
    """Load a built-in profile by ID.

    Parameters
    ----------
    profile_id:
        Profile ID string (e.g. ``"mms_ed21"``).

    Raises
    ------
    FileNotFoundError:
        If no built-in profile with that ID exists.
    """
    path = _PROFILES_DIR / f"{profile_id}.yaml"
    return ConformanceProfile.load_yaml(path)
