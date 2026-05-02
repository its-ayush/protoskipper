# ProtoSkipper Sample Modbus Register Maps

This directory contains example Modbus CSV register maps for common
energy-metering devices.  Load them into a live session via
**Device Tree → right-click session → Import register map…**

## Files

| File | Device | Source document |
|------|--------|-----------------|
| `schneider_pm5560.csv` | Schneider Electric PowerLogic PM5560 | *7EN02-0391-05 EasyLogic PM5500/PM5560 Modbus Communication Guide* (se.com) |
| `siemens_pac2200.csv` | Siemens SENTRON PAC2200 | *A5E02330428-05 SENTRON PAC2200 Modbus Interface* (siemens.com/support) |
| `abb_b23.csv` | ABB B23 112-100 | *B23/B24 Energy Meters Modbus Communication Protocol* (new.abb.com) |

All three devices expose standard power-quality measurements (voltages,
currents, active/reactive power, energy accumulators, THD) via Modbus
holding registers using big-endian float32 encoding.

## Format

Each file conforms to the
[ProtoSkipper Modbus CSV register-map format v1](../../docs/register-maps/MODBUS_CSV_FORMAT.md).

## Notes

- Register addresses are **0-based wire addresses**, not Modicon 40001-style
  notation.
- All float32 values use big-endian byte order and big-endian word order
  unless otherwise noted.
- Only a representative subset of each device's full register map is
  included here.  Refer to the linked vendor document for the complete map.
