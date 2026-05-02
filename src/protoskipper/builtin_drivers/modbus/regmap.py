# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Modbus CSV register-map importer.

Parses files that conform to the format defined in
``docs/register-maps/MODBUS_CSV_FORMAT.md`` and returns a list of
:class:`~protoskipper.core.driver.ObjectRef` instances that can be injected
into a live session via
:meth:`~protoskipper.gui.services.session_manager.SessionManager.import_register_map`.

Design notes
------------
* The parser is **pure** — no pymodbus dependency, no Qt dependency.
* Fatal errors (missing version sentinel, missing required columns, unreadable
  file) raise :class:`~protoskipper.core.errors.EncodingError`.
* Per-row problems are non-fatal: a warning is logged and the row is skipped.
  This matches the skip-with-warning policy in the format spec.
* Duplicate ``object_id`` values: later row wins; an info-level log is emitted.
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Any

from protoskipper.core.driver import Access, DeviceRef, ObjectRef
from protoskipper.core.errors import EncodingError

_logger = logging.getLogger(__name__)

_VERSION_SENTINEL = "# protoskipper-modbus-map v1"

_REQUIRED_COLUMNS = {
    "object_id",
    "data_type",
    "access",
    "label",
    "unit",
    "scale",
    "offset",
    "description",
}

_VALID_DATA_TYPES = {
    "boolean",
    "uint16",
    "int16",
    "uint32",
    "int32",
    "float32",
    "uint64",
    "int64",
    "float64",
    "ascii",
    "utf16",
}

_VALID_ACCESS = {"ro": Access.READ_ONLY, "wo": Access.WRITE_ONLY, "rw": Access.READ_WRITE}

_TABLES = {"coils", "discrete", "holding", "input"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_csv(path: Path, device: DeviceRef | None = None) -> list[ObjectRef]:
    """Parse a Modbus CSV register-map file and return a list of ObjectRefs.

    Parameters
    ----------
    path:
        Filesystem path to the CSV file.
    device:
        The :class:`~protoskipper.core.driver.DeviceRef` to attach to each
        returned :class:`~protoskipper.core.driver.ObjectRef`.  When
        ``None`` a sentinel ``DeviceRef`` with ``protocol="modbus"`` and
        ``address="<pending>"`` is used; the caller must rebind objects to a
        real device before handing them to the driver.

    Returns
    -------
    list[ObjectRef]
        Ordered list (preserves CSV order; later duplicate object_ids replace
        earlier ones while keeping insertion order).

    Raises
    ------
    EncodingError
        If the file cannot be read, does not start with the required version
        sentinel, or is missing required header columns.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EncodingError(f"Cannot read register map {path}: {exc}") from exc

    # Strip UTF-8 BOM if present.
    text = raw.decode("utf-8-sig", errors="replace")
    return _parse_text(text, source=str(path), device=device)


def load_text(
    text: str,
    *,
    source: str = "<string>",
    device: DeviceRef | None = None,
) -> list[ObjectRef]:
    """Parse a CSV register-map from a string.  Primarily used in tests."""
    return _parse_text(text, source=source, device=device)


# ---------------------------------------------------------------------------
# Internal parser
# ---------------------------------------------------------------------------

_SENTINEL_DEVICE = DeviceRef(protocol="modbus", address="<pending>")


def _parse_text(text: str, *, source: str, device: DeviceRef | None) -> list[ObjectRef]:
    dev = device if device is not None else _SENTINEL_DEVICE

    lines = text.splitlines()
    # Strip leading blank lines before the sentinel.
    non_blank = [ln for ln in lines if ln.strip()]
    if not non_blank:
        raise EncodingError(f"{source}: file is empty")

    if non_blank[0].strip() != _VERSION_SENTINEL:
        raise EncodingError(
            f"{source}: first non-blank line must be exactly "
            f"{_VERSION_SENTINEL!r}; got {non_blank[0].strip()!r}"
        )

    # Feed everything after the sentinel line into the CSV reader.
    # We find the index of the sentinel in the original lines so that
    # row numbers in warnings are accurate.
    sentinel_index = next(i for i, ln in enumerate(lines) if ln.strip() == _VERSION_SENTINEL)
    csv_text = "\n".join(lines[sentinel_index + 1 :])

    reader = csv.DictReader(
        io.StringIO(csv_text),
        skipinitialspace=True,
    )

    # Normalise column names to lower-case for case-insensitive matching.
    # We do a first-pass peek at the header.
    try:
        field_names_raw = reader.fieldnames
    except Exception as exc:  # pragma: no cover
        raise EncodingError(f"{source}: cannot read CSV header: {exc}") from exc

    if field_names_raw is None:
        raise EncodingError(f"{source}: CSV has no header row")

    fieldnames_lower = {f.lower().strip(): f for f in field_names_raw}
    missing = _REQUIRED_COLUMNS - set(fieldnames_lower)
    if missing:
        raise EncodingError(f"{source}: CSV is missing required columns: {sorted(missing)}")

    # Build a helper that fetches a column value case-insensitively.
    def _get(row: dict[str, Any], col: str) -> str:
        key = fieldnames_lower.get(col, col)
        return (row.get(key) or "").strip()

    # We use an ordered dict to handle duplicates (later row wins).
    objects: dict[str, ObjectRef] = {}
    csv_row_number = sentinel_index + 2  # +1 for header, +1 for 1-based

    for row in reader:
        csv_row_number += 1

        # Skip comment rows and blank rows.
        first_val = next(iter(row.values()), "")
        if first_val is None or first_val.strip().startswith("#"):
            continue
        if all((v or "").strip() == "" for v in row.values()):
            continue

        object_id = _get(row, "object_id")
        if not object_id:
            _logger.warning("%s line %d: empty object_id — skipping", source, csv_row_number)
            continue

        # Validate object_id.
        try:
            _validate_object_id(object_id)
        except EncodingError as exc:
            _logger.warning("%s line %d: %s — skipping", source, csv_row_number, exc)
            continue

        # Validate data_type.
        data_type = _get(row, "data_type").lower()
        if data_type not in _VALID_DATA_TYPES:
            _logger.warning(
                "%s line %d: unknown data_type %r (expected one of %s) — skipping",
                source,
                csv_row_number,
                data_type,
                sorted(_VALID_DATA_TYPES),
            )
            continue

        # Validate access.
        access_str = _get(row, "access").lower()
        access = _VALID_ACCESS.get(access_str)
        if access is None:
            _logger.warning(
                "%s line %d: invalid access %r (expected ro/wo/rw) — skipping",
                source,
                csv_row_number,
                access_str,
            )
            continue

        # Validate scale and offset.
        try:
            scale = float(_get(row, "scale") or "1.0")
        except ValueError:
            _logger.warning(
                "%s line %d: non-numeric scale %r — skipping",
                source,
                csv_row_number,
                _get(row, "scale"),
            )
            continue

        try:
            offset = float(_get(row, "offset") or "0.0")
        except ValueError:
            _logger.warning(
                "%s line %d: non-numeric offset %r — skipping",
                source,
                csv_row_number,
                _get(row, "offset"),
            )
            continue

        # Optional columns.
        byte_order = _get(row, "byte_order").lower() or "big"
        word_order = _get(row, "word_order").lower() or "big"
        bit_str = _get(row, "bit")

        if byte_order not in ("big", "little"):
            _logger.warning(
                "%s line %d: invalid byte_order %r — defaulting to 'big'",
                source,
                csv_row_number,
                byte_order,
            )
            byte_order = "big"

        if word_order not in ("big", "little"):
            _logger.warning(
                "%s line %d: invalid word_order %r — defaulting to 'big'",
                source,
                csv_row_number,
                word_order,
            )
            word_order = "big"

        bit: int | None = None
        if bit_str:
            try:
                bit = int(bit_str)
            except ValueError:
                _logger.warning(
                    "%s line %d: non-integer bit %r — skipping",
                    source,
                    csv_row_number,
                    bit_str,
                )
                continue
            if not 0 <= bit <= 15:
                _logger.warning(
                    "%s line %d: bit %d out of range 0-15 - skipping",
                    source,
                    csv_row_number,
                    bit,
                )
                continue
            if data_type != "boolean":
                _logger.warning(
                    "%s line %d: 'bit' column requires data_type='boolean', got %r — skipping",
                    source,
                    csv_row_number,
                    data_type,
                )
                continue

        label = _get(row, "label") or None
        unit = _get(row, "unit") or None
        description = _get(row, "description") or None

        metadata: dict[str, Any] = {
            "scale": scale,
            "offset": offset,
            "byte_order": byte_order,
            "word_order": word_order,
        }
        if bit is not None:
            metadata["bit"] = bit
        if description:
            metadata["description"] = description

        ref = ObjectRef(
            device=dev,
            object_id=object_id,
            data_type=data_type,
            access=access,
            label=label,
            unit=unit,
            metadata=metadata,
        )

        if object_id in objects:
            _logger.info(
                "%s line %d: duplicate object_id %r — overwriting earlier definition",
                source,
                csv_row_number,
                object_id,
            )

        objects[object_id] = ref

    return list(objects.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_object_id(object_id: str) -> None:
    """Raise EncodingError if *object_id* is not a valid Modbus object id."""
    parts = object_id.split(":")
    if len(parts) not in (2, 3):
        raise EncodingError(
            f"object_id must be 'table:address' or 'table:address:count', got {object_id!r}"
        )
    table = parts[0].lower()
    if table not in _TABLES:
        raise EncodingError(f"Unknown Modbus table {parts[0]!r}; expected one of {sorted(_TABLES)}")
    try:
        int(parts[1], 0)
        if len(parts) == 3:
            int(parts[2], 0)
    except ValueError as exc:
        raise EncodingError(f"Non-integer address/count in object_id {object_id!r}: {exc}") from exc
