# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Point-list CSV loader for IEC 60870-5-104.

A point list maps IOA (Information Object Address) to a human-readable
tag and a type so the GUI watchlist and the driver's
``enumerate_objects`` can return meaningful labels instead of raw IOAs.

CSV format
----------

Required columns: ``ioa``, ``type``, ``label``.
Optional columns: ``unit``, ``ca`` (defaults to driver-level CA), ``access``
(``ro`` / ``wo`` / ``rw``; default ``ro``), ``description``.

``type`` is one of the IEC 104 mnemonics this driver understands:
``M_SP_NA_1``, ``M_DP_NA_1``, ``M_ME_NA_1``, ``M_ME_NB_1``, ``M_ME_NC_1``,
``M_SP_TB_1``, ``M_DP_TB_1``, ``M_ME_TF_1``, ``C_SC_NA_1``, ``C_DC_NA_1``.
Unknown types raise :class:`EncodingError` so a typo in a substation
point list fails fast instead of producing silently wrong data.

Lines beginning with ``#`` and blank lines are ignored. The first
non-comment row must be the header.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from protoskipper.builtin_drivers.iec104.asdu import TypeID
from protoskipper.core.driver import Access
from protoskipper.core.errors import EncodingError

REQUIRED_COLUMNS = {"ioa", "type", "label"}

_TYPE_ALIASES = {t.name: t for t in TypeID}


@dataclass(frozen=True)
class PointDef:
    """One row of a point list."""

    ioa: int
    type_id: TypeID
    label: str
    ca: int | None = None
    unit: str | None = None
    access: Access = Access.READ_ONLY
    description: str = ""


def parse_access(s: str) -> Access:
    s = s.strip().lower()
    if s in {"ro", "r", "read", "read-only", "read_only"}:
        return Access.READ_ONLY
    if s in {"wo", "w", "write", "write-only", "write_only"}:
        return Access.WRITE_ONLY
    if s in {"rw", "read-write", "read_write"}:
        return Access.READ_WRITE
    raise EncodingError(f"Unknown access value: {s!r}")


def parse_type(s: str) -> TypeID:
    s = s.strip().upper()
    if s in _TYPE_ALIASES:
        return _TYPE_ALIASES[s]
    # Allow numeric type ids too.
    try:
        return TypeID(int(s))
    except (ValueError, KeyError) as exc:
        raise EncodingError(f"Unknown ASDU type {s!r}; supported: {sorted(_TYPE_ALIASES)}") from exc


def load_point_list(source: str | Path | list[str]) -> list[PointDef]:
    """Parse a CSV point list from a path, raw text, or list of lines.

    Behaviour:

    * If ``source`` is a :class:`pathlib.Path` or a string ending in
      ``.csv``, the file is opened.
    * Any other string is treated as raw CSV content.
    * A list of strings is treated as already-split lines.
    """
    rows = _read_rows(source)
    if not rows:
        raise EncodingError("Point list is empty")

    header = [h.strip().lower() for h in rows[0]]
    missing = REQUIRED_COLUMNS - set(header)
    if missing:
        raise EncodingError(f"Point list is missing required columns: {sorted(missing)}")

    points: list[PointDef] = []
    seen_ioa: set[tuple[int | None, int]] = set()
    for line_no, raw in enumerate(rows[1:], start=2):
        if not raw or all(not c.strip() for c in raw):
            continue
        if raw[0].lstrip().startswith("#"):
            continue
        record = dict(zip(header, raw, strict=False))
        try:
            ioa = int(str(record["ioa"]).strip(), 0)
        except (KeyError, ValueError) as exc:
            raise EncodingError(f"Bad IOA on line {line_no}: {record.get('ioa')!r}") from exc
        type_id = parse_type(str(record["type"]))
        label = str(record["label"]).strip()
        if not label:
            raise EncodingError(f"Empty label on line {line_no}")
        ca_raw = record.get("ca", "").strip() if record.get("ca") else ""
        ca = int(ca_raw, 0) if ca_raw else None
        unit = (record.get("unit") or "").strip() or None
        access = parse_access(record.get("access") or "ro")
        description = (record.get("description") or "").strip()
        key = (ca, ioa)
        if key in seen_ioa:
            raise EncodingError(f"Duplicate IOA on line {line_no}: ca={ca} ioa={ioa}")
        seen_ioa.add(key)
        points.append(
            PointDef(
                ioa=ioa,
                type_id=type_id,
                label=label,
                ca=ca,
                unit=unit,
                access=access,
                description=description,
            )
        )
    return points


def _read_rows(source: str | Path | list[str]) -> list[list[str]]:
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
    elif isinstance(source, list):
        text = "\n".join(source)
    elif isinstance(source, str) and (source.endswith(".csv") and "\n" not in source):
        text = Path(source).read_text(encoding="utf-8")
    else:
        text = source
    reader = csv.reader(text.splitlines())
    return [row for row in reader if row]
