# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""IEC 61850 setup file — save and load session configurations (P8.I.3).

A *setup file* (``*.iec61850-setup.json``) lets an operator persist a full
IEC 61850 workspace:

* One or more MMS connections (host, port, optional AP-Title override).
* SCL file path for the project.
* GOOSE subscriptions — one entry per logical interface.
* GOOSE publisher configuration (dataset ref, GOOSE ID, APPID, VLAN …).

Schema
------
The JSON document is **versioned**. The current schema version is ``1``.
Unknown top-level keys are silently ignored to allow forward-compatibility.
Required keys are validated on load; missing or wrong-typed values raise
:exc:`SetupFileError`.

Usage
-----
::

    from protoskipper_iec61850.setup_file import load_setup, save_setup

    data = load_setup(Path("my_project.iec61850-setup.json"))
    save_setup(Path("my_project.iec61850-setup.json"), data)

The *data* dict matches :data:`EMPTY_SETUP` — callers should start from a
copy of that template and modify it rather than building the dict from
scratch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "EMPTY_SETUP",
    "SCHEMA_VERSION",
    "SetupFileError",
    "load_setup",
    "save_setup",
]

SCHEMA_VERSION = 1

#: A blank, version-stamped setup dict.  Copy and mutate to build a new setup.
EMPTY_SETUP: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "connections": [],
    "scl_path": "",
    "goose_subscriptions": [],
    "goose_publisher": None,
}


class SetupFileError(ValueError):
    """Raised when a setup file cannot be loaded due to schema violations."""


# ---------------------------------------------------------------------------
# Internal validators
# ---------------------------------------------------------------------------


def _require(data: dict[str, Any], key: str, expected_type: type) -> Any:
    """Return ``data[key]`` coerced to *expected_type*, or raise :exc:`SetupFileError`."""
    if key not in data:
        raise SetupFileError(f"Missing required key: {key!r}")
    value = data[key]
    if not isinstance(value, expected_type):
        raise SetupFileError(
            f"Key {key!r}: expected {expected_type.__name__}, got {type(value).__name__}"
        )
    return value


def _validate_connection(conn: Any, idx: int) -> dict[str, Any]:
    if not isinstance(conn, dict):
        raise SetupFileError(f"connections[{idx}] must be an object, got {type(conn).__name__}")
    host = _require(conn, "host", str)
    port = _require(conn, "port", int)
    if not (1 <= port <= 65535):
        raise SetupFileError(f"connections[{idx}].port must be 1-65535, got {port}")
    ap_title: str = conn.get("ap_title", "")
    if not isinstance(ap_title, str):
        raise SetupFileError(f"connections[{idx}].ap_title must be a string")
    return {"host": host, "port": port, "ap_title": ap_title}


def _validate_goose_subscription(sub: Any, idx: int) -> dict[str, Any]:
    if not isinstance(sub, dict):
        raise SetupFileError(
            f"goose_subscriptions[{idx}] must be an object, got {type(sub).__name__}"
        )
    iface = _require(sub, "iface", str)
    go_cb_refs = _require(sub, "go_cb_refs", list)
    for j, ref in enumerate(go_cb_refs):
        if not isinstance(ref, str):
            raise SetupFileError(f"goose_subscriptions[{idx}].go_cb_refs[{j}] must be a string")
    return {"iface": iface, "go_cb_refs": list(go_cb_refs)}


def _validate_goose_publisher(pub: Any) -> dict[str, Any] | None:
    if pub is None:
        return None
    if not isinstance(pub, dict):
        raise SetupFileError(f"goose_publisher must be an object or null, got {type(pub).__name__}")
    iface = _require(pub, "iface", str)
    go_cb_ref = _require(pub, "go_cb_ref", str)
    dataset_ref = _require(pub, "dataset_ref", str)
    goose_id = _require(pub, "goose_id", str)
    app_id = _require(pub, "app_id", int)
    if not (0x0000 <= app_id <= 0x3FFF):
        raise SetupFileError(f"goose_publisher.app_id must be 0x0000-0x3FFF, got {app_id:#06x}")
    vlan_id: int = pub.get("vlan_id", 0)
    if not isinstance(vlan_id, int) or not (0 <= vlan_id <= 4095):
        raise SetupFileError(f"goose_publisher.vlan_id must be 0-4095, got {vlan_id!r}")
    vlan_priority: int = pub.get("vlan_priority", 4)
    if not isinstance(vlan_priority, int) or not (0 <= vlan_priority <= 7):
        raise SetupFileError(f"goose_publisher.vlan_priority must be 0-7, got {vlan_priority!r}")
    conf_rev: int = pub.get("conf_rev", 1)
    if not isinstance(conf_rev, int) or conf_rev < 0:
        raise SetupFileError("goose_publisher.conf_rev must be a non-negative integer")
    return {
        "iface": iface,
        "go_cb_ref": go_cb_ref,
        "dataset_ref": dataset_ref,
        "goose_id": goose_id,
        "app_id": app_id,
        "vlan_id": vlan_id,
        "vlan_priority": vlan_priority,
        "conf_rev": conf_rev,
    }


def _validate(raw: Any) -> dict[str, Any]:
    """Validate *raw* (parsed JSON) against the setup-file schema.

    Returns a normalised dict with only the known, correctly-typed keys.
    """
    if not isinstance(raw, dict):
        raise SetupFileError(f"Setup file must be a JSON object, got {type(raw).__name__}")

    version = _require(raw, "version", int)
    if version != SCHEMA_VERSION:
        raise SetupFileError(f"Unsupported setup file version {version}; expected {SCHEMA_VERSION}")

    connections_raw = _require(raw, "connections", list)
    connections = [_validate_connection(c, i) for i, c in enumerate(connections_raw)]

    scl_path = _require(raw, "scl_path", str)

    subs_raw = _require(raw, "goose_subscriptions", list)
    goose_subscriptions = [_validate_goose_subscription(s, i) for i, s in enumerate(subs_raw)]

    goose_publisher = _validate_goose_publisher(raw.get("goose_publisher"))

    return {
        "version": SCHEMA_VERSION,
        "connections": connections,
        "scl_path": scl_path,
        "goose_subscriptions": goose_subscriptions,
        "goose_publisher": goose_publisher,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_setup(path: Path) -> dict[str, Any]:
    """Load and validate a setup file from *path*.

    Parameters
    ----------
    path:
        Filesystem path to the ``*.iec61850-setup.json`` file.

    Returns
    -------
    dict
        Validated setup dict matching the :data:`EMPTY_SETUP` schema.

    Raises
    ------
    SetupFileError
        If the file cannot be parsed or fails schema validation.
    OSError
        If the file cannot be opened.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SetupFileError(f"Cannot read setup file {path}: {exc}") from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SetupFileError(f"Setup file {path} is not valid JSON: {exc}") from exc

    return _validate(raw)


def save_setup(path: Path, data: dict[str, Any]) -> None:
    """Validate *data* and write it as a setup file to *path*.

    The parent directory is created if it does not already exist.

    Parameters
    ----------
    path:
        Target filesystem path.  Will be overwritten if it already exists.
    data:
        Setup dict — must pass schema validation (same rules as :func:`load_setup`).

    Raises
    ------
    SetupFileError
        If *data* fails schema validation.
    OSError
        If the file cannot be written.
    """
    validated = _validate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(validated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
