# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Session-setup save/restore — persist DeviceRef + ObjectRef lists as JSON.

A "setup" is a snapshot of the currently open sessions: which devices are
connected, which registers are in each session's object browser, and the
session profile and operator tag.  Setups are saved as plain JSON so they
can be version-controlled and shared between operators.

The root JSON structure is::

    {
        "version": 1,
        "sessions": [
            {
                "protocol": "modbus.tcp",
                "address": "10.10.172.74:4196/unit=101",
                "label": "EM6400 #101",
                "profile": "lab",
                "operator": "ayush",
                "objects": [
                    {
                        "object_id": "holding:3000:2",
                        "data_type": "float32",
                        "access": "ro",
                        "label": "Voltage L1",
                        "unit": "V",
                        "metadata": {"byte_order": "big", "word_order": "big"}
                    },
                    ...
                ]
            },
            ...
        ]
    }

Design notes
------------
* The file intentionally contains no credentials or secrets.
* Unknown keys are ignored on load (forward-compatible schema).
* ``ObjectRef.device`` in the restored objects points back to the same
  ``DeviceRef`` as the enclosing session entry.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from protoskipper.core.driver import Access, DeviceRef, ObjectRef, SessionProfile

_logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


class SetupSession:
    """In-memory representation of one saved session entry."""

    def __init__(
        self,
        device: DeviceRef,
        profile: SessionProfile,
        operator: str,
        objects: list[ObjectRef],
    ) -> None:
        self.device = device
        self.profile = profile
        self.operator = operator
        self.objects = objects


class Setup:
    """An ordered list of session entries loaded from (or to be saved to) a file."""

    def __init__(self, sessions: list[SetupSession]) -> None:
        self.sessions = sessions


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _object_to_dict(ref: ObjectRef) -> dict[str, Any]:
    return {
        "object_id": ref.object_id,
        "data_type": ref.data_type,
        "access": ref.access.value,
        "label": ref.label,
        "unit": ref.unit,
        "metadata": dict(ref.metadata) if ref.metadata else {},
    }


def _session_to_dict(entry: SetupSession) -> dict[str, Any]:
    return {
        "protocol": entry.device.protocol,
        "address": entry.device.address,
        "label": entry.device.label,
        "profile": entry.profile.value,
        "operator": entry.operator,
        "objects": [_object_to_dict(o) for o in entry.objects],
    }


def save_setup(setup: Setup, path: Path) -> None:
    """Serialise *setup* to *path* as JSON (creates parent directories)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _SCHEMA_VERSION,
        "sessions": [_session_to_dict(s) for s in setup.sessions],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _logger.info("Saved setup (%d sessions) to %s", len(setup.sessions), path)


# ---------------------------------------------------------------------------
# Deserialisation
# ---------------------------------------------------------------------------


def _access_from_str(value: str) -> Access:
    mapping = {
        "ro": Access.READ_ONLY,
        "rw": Access.READ_WRITE,
        "wo": Access.WRITE_ONLY,
        "none": Access.NONE,
    }
    return mapping.get(value.lower(), Access.READ_ONLY)


def _profile_from_str(value: str) -> SessionProfile:
    mapping = {
        "lab": SessionProfile.LAB,
        "commissioning": SessionProfile.COMMISSIONING,
        "production": SessionProfile.PRODUCTION,
    }
    return mapping.get(value.lower(), SessionProfile.LAB)


def _object_from_dict(d: dict[str, Any], device: DeviceRef) -> ObjectRef | None:
    try:
        return ObjectRef(
            device=device,
            object_id=d["object_id"],
            data_type=d.get("data_type", "uint16"),
            access=_access_from_str(d.get("access", "ro")),
            label=d.get("label") or None,
            unit=d.get("unit") or None,
            metadata=dict(d.get("metadata") or {}),
        )
    except Exception as exc:
        _logger.warning("Skipping malformed object entry %r: %s", d.get("object_id"), exc)
        return None


def _session_from_dict(d: dict[str, Any]) -> SetupSession | None:
    try:
        device = DeviceRef(
            protocol=d["protocol"],
            address=d["address"],
            label=d.get("label"),
        )
        profile = _profile_from_str(d.get("profile", "lab"))
        operator = d.get("operator", "")
        raw_objects = d.get("objects") or []
        objects = [o for raw in raw_objects if (o := _object_from_dict(raw, device)) is not None]
        return SetupSession(device=device, profile=profile, operator=operator, objects=objects)
    except Exception as exc:
        _logger.warning("Skipping malformed session entry: %s", exc)
        return None


def load_setup(path: Path) -> Setup:
    """Deserialise a setup from *path*.

    Returns an empty :class:`Setup` (with zero sessions) on parse error
    rather than raising, so a corrupt file does not crash the app.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _logger.error("Cannot read setup file %s: %s", path, exc)
        return Setup([])

    if not isinstance(raw, dict):
        _logger.error("Setup file %s: root element is not a JSON object", path)
        return Setup([])

    sessions = []
    for entry in raw.get("sessions") or []:
        s = _session_from_dict(entry)
        if s is not None:
            sessions.append(s)
    return Setup(sessions)


# ---------------------------------------------------------------------------
# Register-map save/load (device-independent, for reuse across similar devices)
# ---------------------------------------------------------------------------

_MAP_SCHEMA_VERSION = 1

_SENTINEL_DEVICE = DeviceRef(protocol="unknown", address="__map__")


class RegisterMap:
    """An ordered list of ObjectRef entries without a specific device binding.

    The device field on each ObjectRef is meaningless (placeholder) — it is
    replaced by the caller when the map is imported into a live session.
    """

    def __init__(self, protocol: str, objects: list[ObjectRef]) -> None:
        self.protocol = protocol  # informational; used for compatibility warnings
        self.objects = objects


def save_register_map(rmap: RegisterMap, path: Path) -> None:
    """Serialise *rmap* to *path* as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _MAP_SCHEMA_VERSION,
        "protocol": rmap.protocol,
        "registers": [_object_to_dict(o) for o in rmap.objects],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _logger.info("Saved register map (%d registers) to %s", len(rmap.objects), path)


def load_register_map(path: Path) -> RegisterMap:
    """Deserialise a register map from *path*.

    Objects have a placeholder device; caller must replace via ``dataclasses.replace``.
    Returns an empty :class:`RegisterMap` on parse error.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _logger.error("Cannot read register map %s: %s", path, exc)
        return RegisterMap("unknown", [])
    if not isinstance(raw, dict):
        _logger.error("Register map %s: root element is not a JSON object", path)
        return RegisterMap("unknown", [])
    protocol = raw.get("protocol", "unknown")
    objects = [
        o
        for entry in (raw.get("registers") or [])
        if (o := _object_from_dict(entry, _SENTINEL_DEVICE)) is not None
    ]
    return RegisterMap(protocol, objects)
