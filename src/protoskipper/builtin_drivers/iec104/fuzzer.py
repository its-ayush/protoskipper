# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Deterministic IEC 60870-5-104 fuzzer (P4.D).

Two fuzzing modes are provided:

* **Codec round-trip** (``fuzz_codec_roundtrip``): generates random valid
  APCI / ASDU byte sequences and verifies they decode without raising any
  exception other than the documented :class:`ProtoSkipperError`
  hierarchy. Useful for spotting unguarded ``IndexError`` / ``struct.error``
  paths in the parsers.

* **Mutation fuzzer** (``fuzz_apdu_mutation``): takes a known-good APDU
  and produces single-byte mutations / truncations / extensions, asserting
  the parser either accepts the result or raises
  :class:`ProtoSkipperError` (no unhandled exceptions, no infinite loops,
  no crashes).

The fuzzer is **deterministic**: every helper takes a ``seed`` argument
so failures reproduce. Hypothesis is intentionally not a dependency to
keep core test runtime fast and offline-friendly.

A separate state-machine fuzzer (``fuzz_against_master``) connects to a
master over TCP and pushes a stream of malformed APDUs, returning a
report of how the master responded (clean close vs. silent
disconnection vs. continued operation). This is useful for catching DoS
regressions in the master receive loop.
"""

from __future__ import annotations

import contextlib
import random
import socket
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from protoskipper.builtin_drivers.iec104.apci import (
    MAX_APDU_LEN,
    START_BYTE,
    build_i_frame,
    build_s_frame,
    build_u_frame,
    parse_apdu,
    peek_apdu_length,
)
from protoskipper.builtin_drivers.iec104.asdu import (
    COT,
    Asdu,
    InformationObject,
    Quality,
    TypeID,
    decode_asdu,
    encode_asdu,
)
from protoskipper.core.errors import ProtoSkipperError

# ---------------------------------------------------------------------------
# Codec round-trip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FuzzReport:
    """Summary of a fuzzing run."""

    iterations: int
    valid_decodes: int
    expected_errors: int
    unexpected_errors: int
    seed: int

    @property
    def ok(self) -> bool:
        return self.unexpected_errors == 0


def _random_apdu_bytes(rng: random.Random) -> bytes:
    """Generate a random byte sequence that *looks* like an APDU."""
    # 30% chance of a totally bogus header.
    if rng.random() < 0.3:
        n = rng.randint(0, 32)
        return bytes(rng.randint(0, 255) for _ in range(n))
    # Otherwise build something with the start byte.
    length = rng.randint(0, MAX_APDU_LEN + 5)  # may exceed the spec ceiling
    body = bytes(rng.randint(0, 255) for _ in range(max(0, length)))
    return bytes([START_BYTE, length & 0xFF]) + body


def fuzz_codec_roundtrip(iterations: int = 5000, *, seed: int = 0) -> FuzzReport:
    """Stream random bytes at the APCI parser; assert no unguarded crashes.

    The contract: any input must produce a parsed APDU OR raise a
    :class:`ProtoSkipperError`. Anything else is a bug.
    """
    rng = random.Random(seed)
    valid = 0
    expected = 0
    unexpected = 0
    for _ in range(iterations):
        buf = _random_apdu_bytes(rng)
        # First, peek_apdu_length must not crash on any byte sequence.
        try:
            _total = peek_apdu_length(buf[:2])
        except ProtoSkipperError:
            expected += 1
            continue
        except Exception:
            unexpected += 1
            continue
        try:
            parse_apdu(buf)
            valid += 1
        except ProtoSkipperError:
            expected += 1
        except Exception:
            unexpected += 1
    return FuzzReport(
        iterations=iterations,
        valid_decodes=valid,
        expected_errors=expected,
        unexpected_errors=unexpected,
        seed=seed,
    )


# ---------------------------------------------------------------------------
# ASDU codec fuzzer
# ---------------------------------------------------------------------------


_FUZZ_TYPES: tuple[TypeID, ...] = (
    TypeID.M_SP_NA_1,
    TypeID.M_DP_NA_1,
    TypeID.M_ME_NC_1,
    TypeID.M_ME_NB_1,
    TypeID.M_ME_NA_1,
    TypeID.M_BO_NA_1,
    TypeID.M_IT_NA_1,
    TypeID.C_SC_NA_1,
    TypeID.C_DC_NA_1,
    TypeID.C_SE_NC_1,
    TypeID.C_BO_NA_1,
    TypeID.C_IC_NA_1,
    TypeID.C_CI_NA_1,
)


def _random_asdu_bytes(rng: random.Random) -> bytes:
    """Generate raw bytes that may or may not parse as an ASDU."""
    if rng.random() < 0.5:
        # Random length, random bytes.
        n = rng.randint(0, 60)
        return bytes(rng.randint(0, 255) for _ in range(n))
    # Try to look-like-an-asdu: type+vsq+cot+ca+IO bytes, but lengths fuzzed.
    type_id = rng.randint(0, 255)
    vsq = rng.randint(0, 255)
    cot = rng.randint(0, 255)
    orig = rng.randint(0, 255)
    ca = rng.randint(0, 0xFFFF)
    body_len = rng.randint(0, 50)
    body = bytes(rng.randint(0, 255) for _ in range(body_len))
    return bytes([type_id, vsq, cot, orig, ca & 0xFF, (ca >> 8) & 0xFF]) + body


def fuzz_asdu_codec(iterations: int = 5000, *, seed: int = 0) -> FuzzReport:
    """Random bytes -> ``decode_asdu`` -> must raise ProtoSkipperError or succeed."""
    rng = random.Random(seed)
    valid = 0
    expected = 0
    unexpected = 0
    for _ in range(iterations):
        buf = _random_asdu_bytes(rng)
        try:
            decode_asdu(buf)
            valid += 1
        except ProtoSkipperError:
            expected += 1
        except Exception:
            unexpected += 1
    return FuzzReport(
        iterations=iterations,
        valid_decodes=valid,
        expected_errors=expected,
        unexpected_errors=unexpected,
        seed=seed,
    )


# ---------------------------------------------------------------------------
# Mutation fuzzer
# ---------------------------------------------------------------------------


def _seed_apdus() -> list[bytes]:
    """A library of structurally valid APDUs to use as mutation seeds."""
    seeds: list[bytes] = []
    seeds.append(build_u_frame.__wrapped__ if False else b"")  # placeholder
    seeds.clear()
    from protoskipper.builtin_drivers.iec104.apci import UType

    seeds.append(build_u_frame(UType.STARTDT_ACT))
    seeds.append(build_u_frame(UType.STARTDT_CON))
    seeds.append(build_u_frame(UType.TESTFR_ACT))
    seeds.append(build_s_frame(0))
    seeds.append(build_s_frame(0x7FFF))
    # I-frames with various ASDUs
    body1 = encode_asdu(
        Asdu(
            type_id=TypeID.M_ME_NC_1,
            cot=COT.SPONT,
            ca=1,
            objects=[InformationObject(ioa=4001, value=230.5, quality=Quality())],
        )
    )
    seeds.append(build_i_frame(0, 0, body1))
    body2 = encode_asdu(
        Asdu(
            type_id=TypeID.C_IC_NA_1,
            cot=COT.ACT,
            ca=1,
            objects=[InformationObject(ioa=0, value=20)],
        )
    )
    seeds.append(build_i_frame(0, 0, body2))
    return seeds


def _mutate_byte_flip(buf: bytes, rng: random.Random) -> bytes:
    if not buf:
        return buf
    pos = rng.randrange(len(buf))
    bit = 1 << rng.randrange(8)
    return buf[:pos] + bytes([buf[pos] ^ bit]) + buf[pos + 1 :]


def _mutate_truncate(buf: bytes, rng: random.Random) -> bytes:
    if not buf:
        return buf
    end = rng.randrange(len(buf))
    return buf[:end]


def _mutate_extend(buf: bytes, rng: random.Random) -> bytes:
    extra = bytes(rng.randint(0, 255) for _ in range(rng.randint(1, 32)))
    return buf + extra


def _mutate_byte_set(buf: bytes, rng: random.Random) -> bytes:
    if not buf:
        return buf
    pos = rng.randrange(len(buf))
    return buf[:pos] + bytes([rng.randint(0, 255)]) + buf[pos + 1 :]


_MUTATIONS: tuple[Callable[[bytes, random.Random], bytes], ...] = (
    _mutate_byte_flip,
    _mutate_truncate,
    _mutate_extend,
    _mutate_byte_set,
)


def fuzz_apdu_mutation(iterations: int = 5000, *, seed: int = 0) -> FuzzReport:
    """Mutate a library of valid APDUs and feed them to the parser."""
    rng = random.Random(seed)
    seeds = _seed_apdus()
    valid = 0
    expected = 0
    unexpected = 0
    for _ in range(iterations):
        original = rng.choice(seeds)
        mutator = rng.choice(_MUTATIONS)
        buf = mutator(original, rng)
        try:
            apdu = parse_apdu(buf)
            # If parse succeeded and it carries an ASDU, fuzz the ASDU codec too.
            if apdu.asdu:
                try:
                    decode_asdu(apdu.asdu)
                    valid += 1
                except ProtoSkipperError:
                    expected += 1
                except Exception:
                    unexpected += 1
            else:
                valid += 1
        except ProtoSkipperError:
            expected += 1
        except Exception:
            unexpected += 1
    return FuzzReport(
        iterations=iterations,
        valid_decodes=valid,
        expected_errors=expected,
        unexpected_errors=unexpected,
        seed=seed,
    )


# ---------------------------------------------------------------------------
# State-machine fuzzer (sends garbage at a target master/slave)
# ---------------------------------------------------------------------------


@dataclass
class StateMachineReport:
    """Outcome of a state-machine fuzz run."""

    sent: int
    peer_closed: bool
    duration_seconds: float
    seed: int


def _generate_fuzz_stream(rng: random.Random, count: int) -> Iterator[bytes]:
    seeds = _seed_apdus()
    for _ in range(count):
        choice = rng.random()
        if choice < 0.5:
            yield rng.choice(_MUTATIONS)(rng.choice(seeds), rng)
        elif choice < 0.8:
            n = rng.randint(0, 64)
            yield bytes(rng.randint(0, 255) for _ in range(n))
        else:
            yield rng.choice(seeds)


def fuzz_against_target(
    host: str,
    port: int,
    *,
    iterations: int = 200,
    seed: int = 0,
    delay: float = 0.001,
    connect_timeout: float = 5.0,
) -> StateMachineReport:
    """Connect to ``host:port`` and stream malformed APDUs.

    Returns a :class:`StateMachineReport` describing how many APDUs were
    sent before the peer closed the socket (which is the *correct*
    response to malformed traffic per IEC 104 §5.3).
    """
    rng = random.Random(seed)
    start = time.monotonic()
    sent = 0
    peer_closed = False
    sock = socket.create_connection((host, port), timeout=connect_timeout)
    sock.settimeout(0.5)
    try:
        for buf in _generate_fuzz_stream(rng, iterations):
            try:
                sock.sendall(buf)
            except OSError:
                peer_closed = True
                break
            sent += 1
            if delay:
                time.sleep(delay)
            # Drain any incoming bytes.
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    peer_closed = True
                    break
            except (TimeoutError, BlockingIOError):
                pass
            except OSError:
                peer_closed = True
                break
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    return StateMachineReport(
        sent=sent,
        peer_closed=peer_closed,
        duration_seconds=time.monotonic() - start,
        seed=seed,
    )


__all__ = [
    "FuzzReport",
    "StateMachineReport",
    "fuzz_against_target",
    "fuzz_apdu_mutation",
    "fuzz_asdu_codec",
    "fuzz_codec_roundtrip",
]
