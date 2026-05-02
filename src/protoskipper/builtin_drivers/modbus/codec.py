# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Pure Modbus register codec — encode/decode without pymodbus or Qt.

Modbus holds all data in 16-bit registers (each transmitted big-endian).
For data types wider than 16 bits, two or four consecutive registers are
combined.  This module provides:

* :func:`decode_registers` — list of raw Modbus registers → Python int/float.
* :func:`encode_value`     — Python int/float → list of Modbus registers.
* :func:`required_register_count` — how many registers a data type occupies.

Byte-order and word-order vocabulary
-------------------------------------
``byte_order`` — byte order *within* each 16-bit register:
  * ``"big"``    — standard Modbus: high byte before low byte (default).
  * ``"little"`` — exotic: low byte before high byte.

``word_order`` — order of 16-bit *words* in multi-register values:
  * ``"big"``    — most-significant word in the lower register address (default).
  * ``"little"`` — least-significant word in the lower register address.

Worked example — π as float32 (standard Modbus, big/big)
-----------------------------------------------------------
IEEE-754 bit pattern: 0x40490FDB.

Registers on the wire  :  [0x4049, 0x0FDB]
``byte_order="big"``   :  each register transmitted as-is → bytes [0x40, 0x49, 0x0F, 0xDB]
``word_order="big"``   :  high word (0x4049) at lower address → correct.

decode_registers([0x4049, 0x0FDB], "float32") → 3.14159265…

Design notes
-------------
* Zero non-stdlib imports.  No pymodbus, no Qt.  The driver imports this
  lazily so the core stays importable without optional deps.
* ``encode_value`` is the inverse of ``decode_registers`` for all types.
* Scale / offset live in the driver layer (P1.B.4), *not* here.
"""

from __future__ import annotations

import struct

# ---------------------------------------------------------------------------
# Register-count table
# ---------------------------------------------------------------------------

#: Number of 16-bit Modbus registers each data type occupies.
REGISTER_COUNTS: dict[str, int] = {
    "boolean": 1,
    "uint16": 1,
    "int16": 1,
    "uint32": 2,
    "int32": 2,
    "float32": 2,
    "uint64": 4,
    "int64": 4,
    "float64": 4,
    # ascii / utf16 are variable-length; count is determined by the register-map.
    "ascii": 0,
    "utf16": 0,
}

# Struct format letters (big-endian prefix ">") for fixed-width types.
_STRUCT_FMT: dict[str, str] = {
    "uint16": ">H",
    "int16": ">h",
    "uint32": ">I",
    "int32": ">i",
    "float32": ">f",
    "uint64": ">Q",
    "int64": ">q",
    "float64": ">d",
}


def required_register_count(data_type: str) -> int:
    """Return the number of Modbus registers occupied by *data_type*.

    Raises :class:`KeyError` for unknown types.  Variable-length types
    (``"ascii"``, ``"utf16"``) return 0 — callers must read the
    register-map's ``count`` field for those.
    """
    return REGISTER_COUNTS[data_type]


# ---------------------------------------------------------------------------
# Core codec
# ---------------------------------------------------------------------------


def decode_registers(
    registers: list[int],
    data_type: str,
    byte_order: str = "big",
    word_order: str = "big",
) -> int | float:
    """Decode a list of raw Modbus register values into a Python scalar.

    Parameters
    ----------
    registers:
        Raw 16-bit unsigned register values as returned by pymodbus
        (always in wire order, i.e. ascending register address).
    data_type:
        One of the fixed-width types in :data:`REGISTER_COUNTS`
        (``"boolean"`` and string types are *not* handled here; use
        the driver layer for those).
    byte_order:
        Byte order within each 16-bit register: ``"big"`` (default,
        standard Modbus) or ``"little"``.
    word_order:
        Word order across registers: ``"big"`` (MSW at lower address,
        default) or ``"little"`` (LSW at lower address).

    Returns
    -------
    int | float
        The decoded Python value.
    """
    raw = _registers_to_bytes(registers, byte_order, word_order)
    fmt = _STRUCT_FMT[data_type]
    return struct.unpack(fmt, raw)[0]


def encode_value(
    value: int | float,
    data_type: str,
    byte_order: str = "big",
    word_order: str = "big",
) -> list[int]:
    """Encode a Python scalar into a list of Modbus register values.

    This is the exact inverse of :func:`decode_registers`.

    Parameters
    ----------
    value:
        The value to encode.
    data_type:
        Target Modbus data type (must be in :data:`_STRUCT_FMT`).
    byte_order:
        Byte order within each 16-bit register.
    word_order:
        Word order across registers.

    Returns
    -------
    list[int]
        List of 16-bit unsigned integers in wire order (ascending
        register address), suitable for passing to pymodbus write calls.
    """
    fmt = _STRUCT_FMT[data_type]
    raw: bytes = struct.pack(fmt, value)

    # Split into 16-bit words (big-endian within each word as packed by struct).
    words = [int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2)]

    # Apply byte order: if "little", swap the two bytes within each word.
    if byte_order == "little":
        words = [((w & 0x00FF) << 8) | ((w >> 8) & 0xFF) for w in words]

    # Apply word order: if "little", reverse the word list so LSW is first.
    if word_order == "little":
        words = list(reversed(words))

    return words


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _registers_to_bytes(
    registers: list[int],
    byte_order: str,
    word_order: str,
) -> bytes:
    """Convert Modbus registers to a canonical byte string.

    Steps:

    1. Optionally reverse the register list so that the most-significant
       word is always first (normalises word order to "big").
    2. Pack each 16-bit word using the requested byte order.

    The resulting byte string is always interpreted big-endian by the
    callers (struct format ``">"``) because all byte/word swapping has
    already been applied.
    """
    words = list(reversed(registers)) if word_order == "little" else list(registers)
    bo_char = "<" if byte_order == "little" else ">"
    return b"".join(struct.pack(f"{bo_char}H", w) for w in words)


# ---------------------------------------------------------------------------
# Bit-field helpers
# ---------------------------------------------------------------------------


def decode_bit(register: int, bit: int) -> bool:
    """Extract a single bit from a 16-bit Modbus register.

    Parameters
    ----------
    register:
        Raw 16-bit unsigned register value.
    bit:
        Bit position, 0 = LSB, 15 = MSB.
    """
    return bool((register >> bit) & 1)


def encode_bit(register: int, bit: int, value: bool) -> int:
    """Set or clear a single bit in a 16-bit Modbus register.

    Returns the modified 16-bit register value.  Used to implement
    read-modify-write for holding-register bitfield objects.

    Parameters
    ----------
    register:
        Current 16-bit unsigned register value.
    bit:
        Bit position to modify, 0 = LSB, 15 = MSB.
    value:
        New bit value (True = set, False = clear).
    """
    if value:
        return register | (1 << bit)
    return register & ~(1 << bit) & 0xFFFF


# ---------------------------------------------------------------------------
# String helpers (ascii / utf16)
# ---------------------------------------------------------------------------


def decode_string(
    registers: list[int],
    data_type: str,
    byte_order: str = "big",
) -> str:
    """Decode a list of Modbus registers as a null-terminated string.

    Each 16-bit register holds two characters (high byte first for
    ``byte_order="big"``).  The decoded string is stripped of trailing
    NUL characters and surrounding whitespace.

    Parameters
    ----------
    registers:
        Raw register values in wire order (ascending address).
    data_type:
        ``"ascii"`` or ``"utf16"``.
    byte_order:
        Byte order within each register: ``"big"`` (high byte = first
        character) or ``"little"`` (low byte = first character).

    Returns
    -------
    str
        The decoded, stripped string.
    """
    bo_char = "<" if byte_order == "little" else ">"
    raw = b"".join(struct.pack(f"{bo_char}H", r) for r in registers)
    if data_type == "ascii":
        return raw.decode("ascii", errors="replace").rstrip("\x00").strip()
    if data_type == "utf16":
        # UTF-16 needs a BOM or an explicit byte-order mark; without one,
        # we use the configured byte_order.
        codec = "utf-16-le" if byte_order == "little" else "utf-16-be"
        return raw.decode(codec, errors="replace").rstrip("\x00").strip()
    raise ValueError(f"decode_string: unsupported data_type {data_type!r}")


def encode_string(
    value: str,
    register_count: int,
    data_type: str,
    byte_order: str = "big",
) -> list[int]:
    """Encode a string into a list of Modbus registers.

    The string is null-padded to ``register_count`` registers (2 bytes each).
    Characters that do not fit are silently truncated.

    Parameters
    ----------
    value:
        The string to encode.
    register_count:
        Number of 16-bit registers to fill.
    data_type:
        ``"ascii"`` or ``"utf16"``.
    byte_order:
        Byte order within each register.

    Returns
    -------
    list[int]
        List of 16-bit register values in wire order.
    """
    max_bytes = register_count * 2
    if data_type == "ascii":
        raw = value.encode("ascii", errors="replace")
    elif data_type == "utf16":
        codec = "utf-16-le" if byte_order == "little" else "utf-16-be"
        raw = value.encode(codec, errors="replace")
    else:
        raise ValueError(f"encode_string: unsupported data_type {data_type!r}")

    raw = raw[:max_bytes].ljust(max_bytes, b"\x00")
    bo_char = "<" if byte_order == "little" else ">"
    return [struct.unpack(f"{bo_char}H", raw[i : i + 2])[0] for i in range(0, max_bytes, 2)]
