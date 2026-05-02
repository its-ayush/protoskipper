# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the Modbus register codec (P1.B.1).

Tests cover all fixed-width data types, all four byte_order x word_order
combinations, encode/decode round-trips, and edge values.  No pymodbus or
Qt dependency.
"""

from __future__ import annotations

import math
import struct

import pytest

from protoskipper.builtin_drivers.modbus.codec import (
    decode_bit,
    decode_registers,
    decode_string,
    encode_bit,
    encode_string,
    encode_value,
    required_register_count,
)

# ---------------------------------------------------------------------------
# required_register_count
# ---------------------------------------------------------------------------


class TestRequiredRegisterCount:
    def test_single_register_types(self) -> None:
        for dtype in ("boolean", "uint16", "int16"):
            assert required_register_count(dtype) == 1, dtype

    def test_two_register_types(self) -> None:
        for dtype in ("uint32", "int32", "float32"):
            assert required_register_count(dtype) == 2, dtype

    def test_four_register_types(self) -> None:
        for dtype in ("uint64", "int64", "float64"):
            assert required_register_count(dtype) == 4, dtype

    def test_string_types_return_zero(self) -> None:
        assert required_register_count("ascii") == 0
        assert required_register_count("utf16") == 0

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(KeyError):
            required_register_count("bad_type")


# ---------------------------------------------------------------------------
# decode_registers — uint16 / int16
# ---------------------------------------------------------------------------


class TestDecodeUint16:
    def test_zero(self) -> None:
        assert decode_registers([0x0000], "uint16") == 0

    def test_max(self) -> None:
        assert decode_registers([0xFFFF], "uint16") == 65535

    def test_midrange(self) -> None:
        assert decode_registers([0x1234], "uint16") == 0x1234


class TestDecodeInt16:
    def test_positive(self) -> None:
        assert decode_registers([0x0064], "int16") == 100

    def test_negative(self) -> None:
        # -1 == 0xFFFF in two's complement
        assert decode_registers([0xFFFF], "int16") == -1

    def test_min(self) -> None:
        assert decode_registers([0x8000], "int16") == -32768


# ---------------------------------------------------------------------------
# decode_registers — uint32 / int32
# ---------------------------------------------------------------------------


class TestDecodeUint32:
    def test_big_big_canonical(self) -> None:
        # 0x12345678 → [0x1234, 0x5678] big/big
        result = decode_registers([0x1234, 0x5678], "uint32", "big", "big")
        assert result == 0x12345678

    def test_big_little_word_order(self) -> None:
        # 0x12345678 → [0x5678, 0x1234] big/little (LSW first)
        result = decode_registers([0x5678, 0x1234], "uint32", "big", "little")
        assert result == 0x12345678

    def test_little_big_byte_order(self) -> None:
        # 0x12345678 → each register byte-swapped: [0x3412, 0x7856] little/big
        result = decode_registers([0x3412, 0x7856], "uint32", "little", "big")
        assert result == 0x12345678

    def test_little_little(self) -> None:
        # 0x12345678 → [0x7856, 0x3412] little/little
        result = decode_registers([0x7856, 0x3412], "uint32", "little", "little")
        assert result == 0x12345678

    def test_zero(self) -> None:
        assert decode_registers([0, 0], "uint32") == 0

    def test_max(self) -> None:
        assert decode_registers([0xFFFF, 0xFFFF], "uint32") == 0xFFFFFFFF


class TestDecodeInt32:
    def test_negative_one(self) -> None:
        assert decode_registers([0xFFFF, 0xFFFF], "int32") == -1

    def test_min(self) -> None:
        assert decode_registers([0x8000, 0x0000], "int32") == -(2**31)


# ---------------------------------------------------------------------------
# decode_registers — float32
# ---------------------------------------------------------------------------


class TestDecodeFloat32:
    def test_pi_big_big(self) -> None:
        # IEEE-754 π: 0x40490FDB → [0x4049, 0x0FDB]
        result = decode_registers([0x4049, 0x0FDB], "float32", "big", "big")
        assert result == pytest.approx(math.pi, rel=1e-6)

    def test_pi_big_little(self) -> None:
        # word order little: [0x0FDB, 0x4049]
        result = decode_registers([0x0FDB, 0x4049], "float32", "big", "little")
        assert result == pytest.approx(math.pi, rel=1e-6)

    def test_pi_little_big(self) -> None:
        # byte order little: each register byte-swapped → [0x4940, 0xDB0F]
        result = decode_registers([0x4940, 0xDB0F], "float32", "little", "big")
        assert result == pytest.approx(math.pi, rel=1e-6)

    def test_pi_little_little(self) -> None:
        # both little: [0xDB0F, 0x4940]
        result = decode_registers([0xDB0F, 0x4940], "float32", "little", "little")
        assert result == pytest.approx(math.pi, rel=1e-6)

    def test_zero(self) -> None:
        assert decode_registers([0, 0], "float32") == pytest.approx(0.0)

    def test_negative(self) -> None:
        raw = struct.pack(">f", -3.14)
        regs = [int.from_bytes(raw[i : i + 2], "big") for i in (0, 2)]
        result = decode_registers(regs, "float32")
        assert result == pytest.approx(-3.14, rel=1e-5)

    def test_infinity(self) -> None:
        raw = struct.pack(">f", float("inf"))
        regs = [int.from_bytes(raw[i : i + 2], "big") for i in (0, 2)]
        assert decode_registers(regs, "float32") == float("inf")


# ---------------------------------------------------------------------------
# decode_registers — uint64 / int64 / float64
# ---------------------------------------------------------------------------


class TestDecodeUint64:
    def test_known_value_big_big(self) -> None:
        val = 0x0102030405060708
        raw = struct.pack(">Q", val)
        regs = [int.from_bytes(raw[i : i + 2], "big") for i in range(0, 8, 2)]
        assert decode_registers(regs, "uint64", "big", "big") == val

    def test_known_value_big_little(self) -> None:
        val = 0x0102030405060708
        raw = struct.pack(">Q", val)
        regs = [int.from_bytes(raw[i : i + 2], "big") for i in range(0, 8, 2)]
        regs_little_word = list(reversed(regs))
        assert decode_registers(regs_little_word, "uint64", "big", "little") == val


class TestDecodeFloat64:
    def test_pi_big_big(self) -> None:
        raw = struct.pack(">d", math.pi)
        regs = [int.from_bytes(raw[i : i + 2], "big") for i in range(0, 8, 2)]
        result = decode_registers(regs, "float64", "big", "big")
        assert result == pytest.approx(math.pi, rel=1e-15)


# ---------------------------------------------------------------------------
# encode_value
# ---------------------------------------------------------------------------


class TestEncodeUint16:
    def test_zero(self) -> None:
        assert encode_value(0, "uint16") == [0]

    def test_max(self) -> None:
        assert encode_value(65535, "uint16") == [0xFFFF]

    def test_midrange(self) -> None:
        assert encode_value(0x1234, "uint16") == [0x1234]


class TestEncodeUint32:
    def test_big_big(self) -> None:
        assert encode_value(0x12345678, "uint32", "big", "big") == [0x1234, 0x5678]

    def test_big_little(self) -> None:
        assert encode_value(0x12345678, "uint32", "big", "little") == [0x5678, 0x1234]

    def test_little_big(self) -> None:
        assert encode_value(0x12345678, "uint32", "little", "big") == [0x3412, 0x7856]

    def test_little_little(self) -> None:
        assert encode_value(0x12345678, "uint32", "little", "little") == [0x7856, 0x3412]


class TestEncodeFloat32:
    def test_pi_big_big(self) -> None:
        registers = encode_value(math.pi, "float32", "big", "big")
        assert registers == [0x4049, 0x0FDB]

    def test_pi_big_little(self) -> None:
        # Plan AC: encode_float32(3.14159, big_byte, little_word)
        registers = encode_value(math.pi, "float32", "big", "little")
        assert registers == [0x0FDB, 0x4049]

    def test_pi_little_big(self) -> None:
        registers = encode_value(math.pi, "float32", "little", "big")
        assert registers == [0x4940, 0xDB0F]

    def test_pi_little_little(self) -> None:
        registers = encode_value(math.pi, "float32", "little", "little")
        assert registers == [0xDB0F, 0x4940]


# ---------------------------------------------------------------------------
# Round-trip (encode → decode) for all fixed-width types
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize(
        "dtype,value",
        [
            ("uint16", 0x1234),
            ("int16", -100),
            ("uint32", 0x12345678),
            ("int32", -123456),
            ("float32", 3.14159),
            ("uint64", 0x0102030405060708),
            ("int64", -(2**50)),
            ("float64", math.pi),
        ],
    )
    @pytest.mark.parametrize(
        "bo,wo",
        [
            ("big", "big"),
            ("big", "little"),
            ("little", "big"),
            ("little", "little"),
        ],
    )
    def test_round_trip(self, dtype: str, value: int | float, bo: str, wo: str) -> None:
        registers = encode_value(value, dtype, bo, wo)
        decoded = decode_registers(registers, dtype, bo, wo)
        if isinstance(value, float):
            assert decoded == pytest.approx(value, rel=1e-5)
        else:
            assert decoded == value


# ---------------------------------------------------------------------------
# Bit-field helpers
# ---------------------------------------------------------------------------


class TestDecodeBit:
    def test_lsb(self) -> None:
        assert decode_bit(0b0000_0001, 0) is True
        assert decode_bit(0b0000_0000, 0) is False

    def test_msb(self) -> None:
        assert decode_bit(0x8000, 15) is True
        assert decode_bit(0x7FFF, 15) is False

    def test_mid_bit(self) -> None:
        assert decode_bit(0b0000_0100, 2) is True
        assert decode_bit(0b1111_1011, 2) is False


class TestEncodeBit:
    def test_set_lsb(self) -> None:
        assert encode_bit(0x0000, 0, True) == 0x0001

    def test_clear_lsb(self) -> None:
        assert encode_bit(0xFFFF, 0, False) == 0xFFFE

    def test_set_msb(self) -> None:
        assert encode_bit(0x0000, 15, True) == 0x8000

    def test_clear_msb(self) -> None:
        assert encode_bit(0xFFFF, 15, False) == 0x7FFF

    def test_set_is_idempotent(self) -> None:
        assert encode_bit(0x0002, 1, True) == 0x0002

    def test_clear_is_idempotent(self) -> None:
        assert encode_bit(0xFFFD, 1, False) == 0xFFFD

    def test_result_masked_to_16_bits(self) -> None:
        assert encode_bit(0xFFFF, 15, True) == 0xFFFF

    def test_clear_bit_masks_to_16_bits(self) -> None:
        assert encode_bit(0x0000, 0, False) == 0x0000


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------


class TestDecodeString:
    def test_ascii_big_endian(self) -> None:
        # "AB" → register 0x4142 (A=0x41, B=0x42)
        assert decode_string([0x4142], "ascii", "big") == "AB"

    def test_ascii_little_endian(self) -> None:
        # "AB" little → register 0x4241 (B=0x42 at high byte when packed LE)
        # pack("<H", 0x4241) → b'\x41\x42' → "AB"
        assert decode_string([0x4241], "ascii", "little") == "AB"

    def test_ascii_null_trimmed(self) -> None:
        # "A\x00" → just "A"
        assert decode_string([0x4100], "ascii", "big") == "A"

    def test_ascii_empty(self) -> None:
        assert decode_string([0x0000, 0x0000], "ascii", "big") == ""

    def test_ascii_multi_register(self) -> None:
        # "ABCD" → [0x4142, 0x4344]
        assert decode_string([0x4142, 0x4344], "ascii", "big") == "ABCD"

    def test_utf16_big_endian(self) -> None:
        # "A" in UTF-16-BE = 0x0041 → register 0x0041
        assert decode_string([0x0041], "utf16", "big") == "A"

    def test_utf16_null_trimmed(self) -> None:
        assert decode_string([0x0041, 0x0000], "utf16", "big") == "A"

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            decode_string([0x4142], "uint16", "big")


class TestEncodeString:
    def test_ascii_round_trip(self) -> None:
        regs = encode_string("AB", 1, "ascii", "big")
        assert decode_string(regs, "ascii", "big") == "AB"

    def test_ascii_null_padded(self) -> None:
        regs = encode_string("A", 2, "ascii", "big")
        assert len(regs) == 2
        assert decode_string(regs, "ascii", "big") == "A"

    def test_ascii_truncated(self) -> None:
        # "ABCDE" truncated to 2 registers (4 bytes → 4 chars)
        regs = encode_string("ABCDE", 2, "ascii", "big")
        assert len(regs) == 2
        result = decode_string(regs, "ascii", "big")
        assert result == "ABCD"

    def test_utf16_round_trip(self) -> None:
        regs = encode_string("A", 1, "utf16", "big")
        assert decode_string(regs, "utf16", "big") == "A"

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            encode_string("X", 1, "uint16", "big")
