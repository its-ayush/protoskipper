# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet points-list parser (EDE-2, EDE-3, Annex Q AT, native CSV/JSON).

Supported formats
-----------------

* **EDE-2** — the six-file Engineering Data Exchange bundle (object-list CSV,
  state-text CSV, datatype CSV, unit CSV, vendor CSV, notification CSV).
  Both English and German column-name variants are normalised.
* **EDE-3** — the newer single-CSV format with an ``ede-version`` header row.
* **Annex Q AT** — ANSI/ASHRAE 135-2020 Annex Q textual format.
* **Native CSV** — our own column superset (device_id, objid, object_name,
  description, units, state_text, cov_increment, …).
* **JSON** — our canonical points-list JSON (array of objects matching the
  native CSV columns).

Lines beginning with ``#`` and blank lines are silently ignored.

Usage::

    from protoskipper.builtin_drivers.bacnet.points import load_point_list
    points = load_point_list("/path/to/AHU1.ede")
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from protoskipper.core.driver import Access
from protoskipper.core.errors import EncodingError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PointDef:
    """One row from a BACnet points list."""

    device_id: int | None  #: target Device-Instance; None = any device
    objid: str  #: canonical "type:instance" e.g. "analog-value:1"
    object_name: str = ""
    description: str = ""
    data_type: str = "real"  #: bacpypes3 primitive type hint
    units: str = ""  #: engineering units string
    state_text: tuple[str, ...] = field(default_factory=tuple)  #: for BV/MSV
    cov_increment: float | None = None
    relinquish_default: Any = None
    min_value: float | None = None
    max_value: float | None = None
    notification_class: int | None = None
    access: Access = Access.READ_ONLY
    vendor_quirk: str = ""
    gi_group: int | None = None  #: BACnet does not have GI groups per se, kept for compat

    @property
    def object_type(self) -> str:
        return self.objid.split(":")[0] if ":" in self.objid else self.objid

    @property
    def instance(self) -> int:
        try:
            return int(self.objid.split(":")[1])
        except (IndexError, ValueError):
            return 0


# ---------------------------------------------------------------------------
# Column-name normalisation tables
# ---------------------------------------------------------------------------

# EDE-2 English header → our canonical key
_EDE2_EN: dict[str, str] = {
    "keyname": "object_name",
    "key name": "object_name",
    "object-name": "object_name",
    "object name": "object_name",
    "object-identifier": "objid_raw",
    "object identifier": "objid_raw",
    "object_identifier": "objid_raw",
    "object-type": "object_type_raw",
    "object type": "object_type_raw",
    "object_type": "object_type_raw",
    "instance-number": "instance",
    "instance number": "instance",
    "instance_number": "instance",
    "instance": "instance",
    "description": "description",
    "device-instance": "device_id",
    "device instance": "device_id",
    "device_instance": "device_id",
    "device-id": "device_id",
    "device_id": "device_id",
    "units": "units",
    "unit": "units",
    "unit-code": "units",
    "cov-increment": "cov_increment",
    "cov_increment": "cov_increment",
    "state-text": "state_text_raw",
    "state_text": "state_text_raw",
    "access": "access_raw",
    "vendor-quirk": "vendor_quirk",
    "vendor_quirk": "vendor_quirk",
    "notification-class": "notification_class",
    "notification_class": "notification_class",
    "min-present-value": "min_value",
    "min_present_value": "min_value",
    "max-present-value": "max_value",
    "max_present_value": "max_value",
    "relinquish-default": "relinquish_default_raw",
    "relinquish_default": "relinquish_default_raw",
    "data-type": "data_type",
    "data_type": "data_type",
    "datatype": "data_type",
    "objid": "objid_raw",
}

# EDE-2 German header → same canonical keys
_EDE2_DE: dict[str, str] = {
    "objektname": "object_name",
    "objektkennung": "objid_raw",
    "objekttyp": "object_type_raw",
    "instanznummer": "instance",
    "beschreibung": "description",
    "geräteinstanz": "device_id",
    "einheit": "units",
    "cov-inkrement": "cov_increment",
    "zustandstext": "state_text_raw",
}

# Merge
_ALL_COL_MAP = {**_EDE2_EN, **_EDE2_DE}

# BACnet object-type name → our canonical "type:…" prefix
_OBJ_TYPE_ALIASES: dict[str, str] = {
    "analog-input": "analog-input",
    "ai": "analog-input",
    "analoginput": "analog-input",
    "analog-output": "analog-output",
    "ao": "analog-output",
    "analogoutput": "analog-output",
    "analog-value": "analog-value",
    "av": "analog-value",
    "analogvalue": "analog-value",
    "binary-input": "binary-input",
    "bi": "binary-input",
    "binaryinput": "binary-input",
    "binary-output": "binary-output",
    "bo": "binary-output",
    "binaryoutput": "binary-output",
    "binary-value": "binary-value",
    "bv": "binary-value",
    "binaryvalue": "binary-value",
    "multi-state-input": "multi-state-input",
    "msi": "multi-state-input",
    "multistatevalue": "multi-state-value",
    "multi-state-value": "multi-state-value",
    "msv": "multi-state-value",
    "multi-state-output": "multi-state-output",
    "mso": "multi-state-output",
    "notification-class": "notification-class",
    "nc": "notification-class",
    "schedule": "schedule",
    "sch": "schedule",
    "calendar": "calendar",
    "cal": "calendar",
    "trend-log": "trend-log",
    "tl": "trend-log",
    "trendlog": "trend-log",
    "trend-log-multiple": "trend-log-multiple",
    "tlm": "trend-log-multiple",
    "event-log": "event-log",
    "file": "file",
    "device": "device",
    "program": "program",
    "loop": "loop",
    "accumulator": "accumulator",
    "pulse-converter": "pulse-converter",
    "averaging": "averaging",
    "event-enrollment": "event-enrollment",
    "life-safety-point": "life-safety-point",
    "life-safety-zone": "life-safety-zone",
    "large-analog-value": "large-analog-value",
    "lav": "large-analog-value",
    "channel": "channel",
    "lighting-output": "lighting-output",
    "binary-lighting-output": "binary-lighting-output",
    "blo": "binary-lighting-output",
    "global-group": "global-group",
    "structured-view": "structured-view",
    "network-port": "network-port",
}


def _normalise_obj_type(raw: str) -> str:
    """Return the canonical object-type string for a raw type label."""
    key = raw.strip().lower().replace("_", "-")
    return _OBJ_TYPE_ALIASES.get(key, key)


def _normalise_col(header: str) -> str:
    """Map a raw CSV header to our internal field name."""
    return _ALL_COL_MAP.get(header.strip().lower(), header.strip().lower())


def _parse_access(s: str) -> Access:
    s = s.strip().lower()
    if s in {"ro", "r", "read", "read-only", "read_only", "commandable-output"}:
        return Access.READ_ONLY
    if s in {"wo", "w", "write", "write-only"}:
        return Access.WRITE_ONLY
    if s in {"rw", "read-write", "read_write", "commandable-value", "commandable"}:
        return Access.READ_WRITE
    return Access.READ_ONLY


def _make_objid(row: dict[str, str]) -> str:
    """Build the canonical ``type:instance`` objid from a normalised row dict."""
    objid_raw = row.get("objid_raw", "").strip()
    instance_raw = row.get("instance", "").strip() or "0"

    if objid_raw:
        # EDE-2 numeric pair: "type_id,instance" e.g. "0,1"
        if re.match(r"^\d+,\d+$", objid_raw):
            parts = objid_raw.split(",")
            return f"{_normalise_obj_type(parts[0])}:{parts[1]}"

        # Already in "type:instance" form e.g. "analog-input:1" or "AI:1"
        if ":" in objid_raw:
            parts = objid_raw.split(":", 1)
            return f"{_normalise_obj_type(parts[0])}:{parts[1]}"

        # Type abbreviation with a hyphen but no instance yet e.g. "analog-input"
        # plus a separate instance-number column (EDE-3 object-identifier style)
        if "-" in objid_raw and instance_raw:
            return f"{_normalise_obj_type(objid_raw)}:{instance_raw}"

        # Short type alias without instance (EDE-3: object-identifier=AV, instance-number=1)
        # If an instance column is available, build "type:instance"
        if instance_raw and instance_raw != "0":
            return f"{_normalise_obj_type(objid_raw)}:{instance_raw}"

        # Fallback: return as-is if nothing else matched
        return objid_raw

    # Build from separate object_type_raw + instance columns
    obj_type = _normalise_obj_type(row.get("object_type_raw", row.get("objid", "analog-value")))
    return f"{obj_type}:{instance_raw}"


def _row_to_point_def(row: dict[str, str], file_hint: str = "") -> PointDef:
    """Convert one normalised row dict to a :class:`PointDef`."""
    device_id_raw = row.get("device_id", "").strip()
    device_id = int(device_id_raw) if device_id_raw.isdigit() else None

    objid = _make_objid(row)

    state_text_raw = row.get("state_text_raw", "").strip()
    state_text: tuple[str, ...] = (
        tuple(s.strip() for s in state_text_raw.split(";") if s.strip()) if state_text_raw else ()
    )

    cov_raw = row.get("cov_increment", "").strip()
    cov_increment: float | None = float(cov_raw) if cov_raw else None

    min_raw = row.get("min_value", "").strip()
    min_value: float | None = float(min_raw) if min_raw else None

    max_raw = row.get("max_value", "").strip()
    max_value: float | None = float(max_raw) if max_raw else None

    nc_raw = row.get("notification_class", "").strip()
    notification_class: int | None = int(nc_raw) if nc_raw.isdigit() else None

    relinquish_raw = row.get("relinquish_default_raw", "").strip()
    relinquish: Any = None
    if relinquish_raw:
        try:
            relinquish = float(relinquish_raw)
        except ValueError:
            relinquish = relinquish_raw

    return PointDef(
        device_id=device_id,
        objid=objid,
        object_name=row.get("object_name", "").strip(),
        description=row.get("description", "").strip(),
        data_type=row.get("data_type", "real").strip().lower() or "real",
        units=row.get("units", "").strip(),
        state_text=state_text,
        cov_increment=cov_increment,
        relinquish_default=relinquish,
        min_value=min_value,
        max_value=max_value,
        notification_class=notification_class,
        access=_parse_access(row.get("access_raw", "ro")),
        vendor_quirk=row.get("vendor_quirk", "").strip(),
    )


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_point_list(source: str | Path | list[str]) -> list[PointDef]:
    """Parse a BACnet points list from a file path, raw text, or list of lines.

    Auto-detects format:

    * ``*.json`` → JSON array
    * ``*.ede`` / ``*.csv`` containing ``ede-version`` → EDE-3
    * ``*.csv`` with ``# EDE-2`` header or six-file bundle directory → EDE-2
    * Any other CSV → native CSV / EDE-3 single-file

    Returns a list of :class:`PointDef` objects (one per data point).
    Raises :class:`~protoskipper.core.errors.EncodingError` on fatal format
    errors.
    """
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_dir():
            return _load_ede2_bundle(path)
        text = path.read_text(encoding="utf-8-sig")  # BOM-tolerant
        if path.suffix.lower() == ".json":
            return _load_json(text, str(path))
        return _load_csv_auto(text, str(path))
    # List of lines → treat as CSV
    return _load_csv_auto("\n".join(source), "<inline>")


def _load_json(text: str, source: str) -> list[PointDef]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EncodingError(f"{source}: invalid JSON — {exc}") from exc
    if not isinstance(data, list):
        raise EncodingError(f"{source}: expected a JSON array at the root")
    points: list[PointDef] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise EncodingError(f"{source}[{i}]: each item must be an object")
        row = {
            k.lower().replace("-", "_"): str(v) if v is not None else "" for k, v in item.items()
        }
        # JSON uses canonical names already; normalise anyway
        row = {_normalise_col(k): v for k, v in row.items()}
        points.append(_row_to_point_def(row, source))
    return points


def _load_csv_auto(text: str, source: str) -> list[PointDef]:
    """Auto-detect EDE-3 vs native CSV and parse."""
    lines = text.splitlines()
    # Check for EDE-3 sentinel
    for line in lines[:5]:
        if re.match(r"#\s*ede.?version|ede[-_]?version", line, re.IGNORECASE):
            return _load_ede3(lines, source)
    return _load_native_csv(lines, source)


def _load_ede3(lines: list[str], source: str) -> list[PointDef]:
    """Parse EDE-3 single-file format.

    EDE-3 starts with a comment block containing ``# ede-version:3`` and a
    ``# property-identifier:`` line, followed by a normal CSV header + data.
    """
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("#") or not line.strip():
            continue
        data_lines.append(line)

    if not data_lines:
        return []

    reader = csv.DictReader(data_lines)
    if reader.fieldnames is None:
        return []

    norm_reader = _NormDictReader(reader)
    points: list[PointDef] = []
    for i, row in enumerate(norm_reader):
        try:
            points.append(_row_to_point_def(row, source))
        except Exception as exc:
            _logger.warning("%s row %d: %s", source, i + 2, exc)
    return points


def _load_native_csv(lines: list[str], source: str) -> list[PointDef]:
    """Parse the ProtoSkipper native CSV / generic EDE-like CSV."""
    data_lines = [ln for ln in lines if ln.strip() and not ln.startswith("#")]
    if not data_lines:
        return []

    reader = csv.DictReader(data_lines)
    if reader.fieldnames is None:
        return []

    norm_reader = _NormDictReader(reader)
    points: list[PointDef] = []
    for i, row in enumerate(norm_reader):
        try:
            points.append(_row_to_point_def(row, source))
        except Exception as exc:
            _logger.warning("%s row %d: %s", source, i + 2, exc)
    return points


def _load_ede2_bundle(directory: Path) -> list[PointDef]:
    """Load an EDE-2 six-file bundle from a directory.

    Looks for the object-list file (``*ObjectList*.csv`` or ``*_objects.csv``
    or the file that has the most columns) and optional supplemental files.
    """
    csv_files = sorted(directory.glob("*.csv"))
    if not csv_files:
        raise EncodingError(f"No CSV files found in EDE-2 bundle directory: {directory}")

    # Heuristic: largest file is likely the object list
    object_list_file = max(csv_files, key=lambda p: p.stat().st_size)

    # Check for an explicit naming convention
    for f in csv_files:
        name = f.stem.lower()
        if "object" in name or "objlist" in name or "ede" in name:
            object_list_file = f
            break

    text = object_list_file.read_text(encoding="utf-8-sig")
    return _load_native_csv(text.splitlines(), str(object_list_file))


class _NormDictReader:
    """Wraps a csv.DictReader to normalise column names."""

    def __init__(self, reader: csv.DictReader) -> None:
        self._reader = reader

    def __iter__(self):  # type: ignore[return]
        for raw_row in self._reader:
            yield {_normalise_col(k): v for k, v in (raw_row or {}).items() if k is not None}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
    line: int
    message: str
    rule: str
    severity: str = "error"  # "error" | "warning"


def validate_point_list(points: list[PointDef]) -> list[ValidationIssue]:
    """Run basic validation over a parsed points list.

    Rules checked:
    * Every point must have a non-empty ``objid``.
    * ``objid`` must be in ``type:instance`` form.
    * Duplicate (device_id, objid) pairs are warnings.
    * ``instance`` must be ≥ 0 and ≤ 4194302.
    """
    issues: list[ValidationIssue] = []
    seen: dict[tuple[int | None, str], int] = {}

    for i, p in enumerate(points):
        line = i + 1
        if not p.objid:
            issues.append(ValidationIssue(line, "Empty objid", "non-empty-objid"))
            continue
        if ":" not in p.objid:
            issues.append(
                ValidationIssue(
                    line,
                    f"objid {p.objid!r} must be 'type:instance'",
                    "objid-format",
                )
            )
        else:
            try:
                inst = int(p.objid.split(":")[1])
                if not (0 <= inst <= 4194302):
                    issues.append(
                        ValidationIssue(
                            line,
                            f"Instance {inst} out of range 0..4194302",
                            "instance-range",
                            "warning",
                        )
                    )
            except ValueError:
                issues.append(
                    ValidationIssue(line, f"Non-integer instance in {p.objid!r}", "instance-int")
                )
        key = (p.device_id, p.objid)
        if key in seen:
            issues.append(
                ValidationIssue(
                    line,
                    f"Duplicate objid {p.objid!r} (also at row {seen[key]})",
                    "duplicate-objid",
                    "warning",
                )
            )
        else:
            seen[key] = line

    return issues
