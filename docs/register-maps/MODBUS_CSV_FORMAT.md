# ProtoSkipper Modbus CSV Register-Map Format — v1

A **register map** CSV file tells ProtoSkipper which Modbus objects a device
exposes, how to decode them, and what engineering-unit conversions to apply.
Right-clicking a session in the Device Tree and choosing **Import register
map…** loads one of these files and repopulates the object browser.

---

## File structure

```
# protoskipper-modbus-map v1
object_id,data_type,access,label,unit,scale,offset,description
holding:0,uint16,rw,Active Power,W,1.0,0.0,Active power measurement
holding:2,float32,ro,Voltage Phase A,V,1.0,0.0,Phase-to-neutral RMS voltage (big byte, big word)
holding:10,uint16,ro,Status Bits,,1.0,0.0,Device status bitfield
```

### Line 1 — Version comment (required)

The first non-blank line **must** be exactly:

```
# protoskipper-modbus-map v1
```

This sentinel allows the importer to reject non-map files early and
reserve the format version number for future breaking changes.

### Line 2 — Header row (required)

The header row names the columns.  Column order is **not** fixed; the
parser matches columns by name.  All eight required columns must be
present; optional columns may appear in any order after them.

---

## Required columns

| Column | Type | Description |
|--------|------|-------------|
| `object_id` | string | Modbus address in `table:address[:count]` format (see below). |
| `data_type` | string | Wire encoding of the value (see **Data types** below). |
| `access` | string | `ro` (read-only), `wo` (write-only), or `rw` (read/write). |
| `label` | string | Human-readable tag name shown in the GUI.  May be empty. |
| `unit` | string | Engineering unit, e.g. `V`, `kWh`, `degC`.  May be empty. |
| `scale` | float | Multiply raw register value by this to get the engineering value.  Default `1.0`. |
| `offset` | float | Add this to the scaled value.  Applied **after** scale.  Default `0.0`. |
| `description` | string | Free-form documentation string. May be empty. |

### `object_id` format

```
<table>:<address>[:<count>]
```

* **table**: `coils`, `discrete`, `holding`, or `input`.
* **address**: zero-based register number (wire address, not Modicon 40001
  notation).  Decimal or `0x`-prefixed hex.
* **count**: number of registers consumed.  Defaults to 1.  Must be
  consistent with `data_type` (e.g. `float32` requires count ≥ 2).

Examples:

| `object_id` | Meaning |
|-------------|---------|
| `holding:0` | Holding register 0, 1 register wide |
| `holding:0:2` | Holding registers 0–1 (e.g. for a float32) |
| `coils:5` | Coil 5 |
| `input:0x0A` | Input register 10 (hex notation) |

---

## Optional columns

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| `byte_order` | string | `big` | Byte order within each 16-bit register word.  `big` or `little`. |
| `word_order` | string | `big` | Word order across multi-register values.  `big` = high word first; `little` = low word first. |
| `bit` | int | *(absent)* | For bitfield objects: which bit (0-based) of the parent register this object maps to.  When present, `data_type` must be `boolean`. |

---

## Data types

| `data_type` | Registers | Python type | Notes |
|-------------|-----------|-------------|-------|
| `boolean` | 1 (or coil) | `bool` | Non-zero register = `True`.  Use with `bit=` for bit extraction. |
| `uint16` | 1 | `int` | Unsigned 16-bit integer. |
| `int16` | 1 | `int` | Signed 16-bit integer (two's complement). |
| `uint32` | 2 | `int` | 32-bit unsigned integer; register count must be 2. |
| `int32` | 2 | `int` | 32-bit signed integer. |
| `float32` | 2 | `float` | IEEE 754 single precision; register count must be 2. |
| `uint64` | 4 | `int` | 64-bit unsigned integer. |
| `int64` | 4 | `int` | 64-bit signed integer. |
| `float64` | 4 | `float` | IEEE 754 double precision. |
| `ascii` | N | `str` | Two ASCII chars per register, trimmed at first NUL; `count` sets N. |
| `utf16` | N | `str` | One UTF-16 code unit per register; `count` sets N. |

Scale and offset are applied **after** decoding for numeric types; they are
ignored for string types.

Engineering value formula:

```
engineering_value = (raw_value * scale) + offset
```

---

## Error handling for malformed rows

The importer uses a **skip-with-warning** policy for per-row problems so
that a partially-valid map file still loads:

| Problem | Behaviour |
|---------|-----------|
| Unknown `data_type` | Row skipped; warning logged. |
| `object_id` not parseable | Row skipped; warning logged. |
| `access` not one of `ro/wo/rw` | Row skipped; warning logged. |
| `scale` or `offset` not numeric | Row skipped; warning logged. |
| `bit` out of range 0–15 | Row skipped; warning logged. |
| Duplicate `object_id` | Later row overwrites earlier; info logged. |

Fatal errors (file unreadable, missing version comment, missing required
column) raise `EncodingError` immediately and abort the import.

---

## Examples

### Example 1 — Single uint16 holding register

```csv
# protoskipper-modbus-map v1
object_id,data_type,access,label,unit,scale,offset,description
holding:0,uint16,rw,Set Point,degC,0.1,0.0,Temperature set point (raw × 0.1 = °C)
```

A write of `250` (°C × 10) stores `2500` in register 0.
Reading `2500` returns `250.0` as the engineering value.

### Example 2 — float32 with explicit byte and word order

```csv
# protoskipper-modbus-map v1
object_id,data_type,access,label,unit,scale,offset,description,byte_order,word_order
holding:2:2,float32,ro,Active Energy,kWh,1.0,0.0,Accumulated active energy,big,little
```

Registers 2 and 3 are decoded as a 32-bit float with big-endian bytes
within each register and **low word first** (register 3 = high word,
register 2 = low word) — common in older Schneider meters.

### Example 3 — Packed bitfield

```csv
# protoskipper-modbus-map v1
object_id,data_type,access,label,unit,scale,offset,description,bit
holding:10,boolean,ro,Fault: Overvoltage,,1.0,0.0,Set when VAC > 264 V,0
holding:10,boolean,ro,Fault: Undervoltage,,1.0,0.0,Set when VAC < 196 V,1
holding:10,boolean,ro,Fault: Overcurrent,,1.0,0.0,Set when IAC > rated,2
holding:10,boolean,ro,Comms Watchdog,,1.0,0.0,Set when comms lost > 5s,7
```

All four objects share the same parent register (`holding:10`).  The
driver issues a single read request and extracts each bit.  A write to a
bitfield object issues a read-modify-write on the parent register.

---

## Compatibility notes

* Column names are case-insensitive.
* Rows beginning with `#` after the version line are treated as comments
  and skipped.
* Blank rows are skipped.
* The BOM (`\ufeff`) is stripped if present (Excel-exported UTF-8 CSVs).
* Files may use CRLF or LF line endings.
