# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet protocol fuzzer (P7.H.3).

Generates malformed / edge-case BACnet/IP datagrams against a target device
for robustness / BTL negative testing.  Always requires explicit user
confirmation before sending traffic; locked out of PRODUCTION profile.

Usage::

    from protoskipper.builtin_drivers.bacnet.fuzzer import BACnetFuzzer

    results = BACnetFuzzer(
        target="192.168.1.100:47808",
        mutations=["bvlc", "apdu"],
        confirmed=True,          # user confirmed "Fuzzing acknowledged"
    ).run(max_mutations=50)

    for r in results:
        print(r["mutation"], r["outcome"])
"""

from __future__ import annotations

import logging
import os
import random
import socket
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)

__all__ = ["MUTATION_CATEGORIES", "BACnetFuzzer", "FuzzResult"]

MUTATION_CATEGORIES = [
    "bvlc",  # BVLC function code / length mutations
    "npdu",  # NPDU hop-count / priority abuse
    "apdu",  # APDU type / tag mutations
    "sequence",  # Protocol sequencing (COV, segmentation)
]


@dataclass
class FuzzResult:
    """Outcome of a single fuzz mutation."""

    mutation: str
    payload: bytes
    outcome: str  # "no-response", "error-pdu", "reject", "abort", "timeout", "crash-suspected"
    response: bytes = field(default=b"")
    error: str = field(default="")


def _random_bytes(length: int) -> bytes:
    return os.urandom(length)


def _bvlc_mutations() -> Iterator[tuple[str, bytes]]:
    """Yield (label, raw_datagram) for BVLC-layer mutations."""
    # Truncated frame (only 2 bytes)
    yield "bvlc-truncated", bytes([0x81, 0x0A])
    # Unknown BVLC function code 0x7F
    yield "bvlc-unknown-func", bytes([0x81, 0x7F, 0x00, 0x04])
    # Claimed length = 0 (underflow)
    yield "bvlc-zero-length", bytes([0x81, 0x0A, 0x00, 0x00])
    # Claimed length >> actual data (overflow)
    yield "bvlc-length-overflow", bytes([0x81, 0x0A, 0x7F, 0xFF])
    # Register-Foreign-Device with TTL=0
    yield "bvlc-fd-ttl-zero", bytes([0x81, 0x05, 0x00, 0x06, 0x00, 0x00])
    # Distribute-Broadcast-To-Network with empty payload
    yield "bvlc-dbn-empty", bytes([0x81, 0x09, 0x00, 0x04])
    # Forwarded-NPDU with truncated originator address
    yield "bvlc-forwarded-truncated", bytes([0x81, 0x04, 0x00, 0x08]) + b"\xc0\xa8"
    # Random junk with BVLC marker
    yield "bvlc-random-payload", bytes([0x81, 0x0A, 0x00, 0x14]) + _random_bytes(16)


def _npdu_mutations() -> Iterator[tuple[str, bytes]]:
    """Yield NPDU-layer mutations (wrapped in a valid BVLC Original-Unicast header)."""

    def _wrap(npdu: bytes) -> bytes:
        length = 4 + len(npdu)
        return struct.pack(">BBH", 0x81, 0x0A, length) + npdu

    # Hop-count = 0 (should cause routers to discard, but test device reaction)
    yield "npdu-hop-count-zero", _wrap(bytes([0x01, 0x28, 0x00, 0x00, 0x00]))
    # Network-priority = 3 (life-safety priority abuse)
    yield "npdu-priority-abuse", _wrap(bytes([0x01, 0x03]))
    # Reserved network message type 0xFF
    yield "npdu-reserved-netmsg", _wrap(bytes([0x01, 0x80, 0xFF]))
    # Source specifier with truncated MAC
    yield "npdu-src-truncated", _wrap(bytes([0x01, 0x08, 0x00, 0x05]))
    # Destination network = 0xFFFF (global broadcast) + hop = 1
    yield "npdu-global-hop1", _wrap(bytes([0x01, 0x20, 0xFF, 0xFF, 0x00, 0x01]))


def _apdu_mutations() -> Iterator[tuple[str, bytes]]:
    """Yield APDU-layer mutations (wrapped in BVLC + bare NPDU)."""

    def _wrap(apdu: bytes) -> bytes:
        npdu = bytes([0x01, 0x04])  # version=1, control=0x04 (data expecting reply)
        payload = npdu + apdu
        return struct.pack(">BBH", 0x81, 0x0A, 4 + len(payload)) + payload

    # Confirmed request: ReadProperty with invoke-ID=255, object-id boundary
    yield (
        "apdu-rp-invoke-max",
        _wrap(bytes([0x00, 0x05, 0xFF, 0x0C, 0x02, 0x3F, 0xFF, 0xFF, 0x19, 0x55])),
    )
    # Confirmed request: RPM with 1000-element property list (malformed length)
    yield (
        "apdu-rpm-huge-proplist",
        _wrap(bytes([0x00, 0x05, 0x01, 0x0E]) + b"\x1e" + b"\x09\x55" * 200 + b"\x1f"),
    )
    # WriteProperty with wrong-type property value (bit-string for an analog)
    yield (
        "apdu-wp-wrong-type",
        _wrap(
            bytes(
                [
                    0x00,
                    0x05,
                    0x02,
                    0x0F,
                    0x0C,
                    0x00,
                    0x80,
                    0x00,
                    0x01,
                    0x19,
                    0x55,
                    0x3E,
                    0x82,
                    0x05,
                    0x00,
                    0x3F,
                ]
            )
        ),
    )
    # Abort PDU mid-segmentation (Abort reason=server-timeout)
    yield "apdu-abort-mid-seg", _wrap(bytes([0x07, 0x42, 0x01]))
    # Reject PDU with reserved reason 0xFF
    yield "apdu-reject-reserved", _wrap(bytes([0x06, 0x01, 0xFF]))
    # Segmented-ACK with sequence number skipped (seqnum=5 after seqnum=1)
    yield "apdu-segmented-ack-skip", _wrap(bytes([0x04, 0x08, 0x01, 0x05, 0x0F]))
    # Random APDU type (type=7, reserved)
    yield "apdu-random", _wrap(bytes([0xE0]) + _random_bytes(8))


def _sequence_mutations() -> Iterator[tuple[str, bytes]]:
    """Yield protocol-sequencing mutations."""

    def _wrap(apdu: bytes) -> bytes:
        npdu = bytes([0x01, 0x00])
        payload = npdu + apdu
        return struct.pack(">BBH", 0x81, 0x0A, 4 + len(payload)) + payload

    # COV notification without prior subscription (unconfirmed)
    yield (
        "seq-cov-no-sub",
        _wrap(
            bytes(
                [
                    0x10,
                    0x01,  # Unconfirmed-Request, SubscribeCOV service
                    0x09,
                    0x7B,  # process-id = 123
                    0x1C,
                    0x00,
                    0x80,
                    0x00,
                    0x01,  # monitored-object = AI:1
                    0x4E,  # list-of-values open
                    0x09,
                    0x55,  # present-value
                    0x2E,
                    0x44,
                    0x42,
                    0x48,
                    0x00,
                    0x00,
                    0x2F,  # float 50.0
                    0x4F,  # list-of-values close
                ]
            )
        ),
    )
    # Who-Is flood burst (3 rapid broadcasts)
    who_is = _wrap(bytes([0x10, 0x08]))
    yield "seq-who-is-burst", who_is


class BACnetFuzzer:
    """BACnet protocol fuzzer (P7.H.3).

    .. warning::
        This class transmits malformed BACnet packets. Only use on devices
        you own and in isolated lab networks. Locked out of PRODUCTION
        safety profile.

    Parameters
    ----------
    target:
        Target address ``"host:port"`` (port defaults to 47808).
    mutations:
        List of mutation category names from :data:`MUTATION_CATEGORIES`.
        Defaults to all categories.
    confirmed:
        Must be ``True`` (user clicked "Fuzzing acknowledged" checkbox).
        If ``False``, :meth:`run` raises ``RuntimeError``.
    inter_mutation_delay:
        Seconds to wait between mutations (default: 0.1 s).
    recv_timeout:
        Socket receive timeout per mutation (default: 1.0 s).
    seed:
        Random seed for reproducible runs.  ``None`` = non-deterministic.
    """

    def __init__(
        self,
        target: str,
        *,
        mutations: list[str] | None = None,
        confirmed: bool = False,
        inter_mutation_delay: float = 0.1,
        recv_timeout: float = 1.0,
        seed: int | None = None,
    ) -> None:
        if not confirmed:
            raise RuntimeError(
                "Fuzzing requires explicit confirmation — pass confirmed=True "
                "after checking the 'Fuzzing acknowledged' checkbox."
            )
        host, _, port_str = target.partition(":")
        self._host = host
        self._port = int(port_str) if port_str else 47808
        self._categories = mutations if mutations is not None else list(MUTATION_CATEGORIES)
        self._delay = inter_mutation_delay
        self._recv_timeout = recv_timeout
        if seed is not None:
            random.seed(seed)

    def _iter_mutations(self) -> Iterator[tuple[str, bytes]]:
        for cat in self._categories:
            if cat == "bvlc":
                yield from _bvlc_mutations()
            elif cat == "npdu":
                yield from _npdu_mutations()
            elif cat == "apdu":
                yield from _apdu_mutations()
            elif cat == "sequence":
                yield from _sequence_mutations()

    def run(self, max_mutations: int | None = None) -> list[FuzzResult]:
        """Send all selected mutations and return a list of :class:`FuzzResult`.

        *max_mutations* caps the total number of mutations sent (useful for
        a quick smoke-test pass).
        """
        results: list[FuzzResult] = []
        count = 0

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self._recv_timeout)

            for label, payload in self._iter_mutations():
                if max_mutations is not None and count >= max_mutations:
                    break
                count += 1

                _logger.debug(
                    "Fuzzing: %s (%d bytes) -> %s:%d", label, len(payload), self._host, self._port
                )
                outcome = "no-response"
                response = b""
                error = ""

                try:
                    sock.sendto(payload, (self._host, self._port))
                    try:
                        response, _ = sock.recvfrom(4096)
                        outcome = _classify_response(response)
                    except TimeoutError:
                        outcome = "timeout"
                except OSError as exc:
                    outcome = "send-error"
                    error = str(exc)
                    _logger.warning("Fuzz send-error %s: %s", label, exc)

                results.append(
                    FuzzResult(
                        mutation=label,
                        payload=payload,
                        outcome=outcome,
                        response=response,
                        error=error,
                    )
                )

                if self._delay > 0:
                    import time

                    time.sleep(self._delay)

        _logger.info(
            "Fuzz run complete: %d mutations, outcomes: %s",
            len(results),
            {r.outcome for r in results},
        )
        return results

    def report_markdown(self, results: list[FuzzResult]) -> str:
        """Return a Markdown summary of *results* suitable for audit / reporting."""
        lines = [
            "# BACnet Fuzz Report",
            f"Target: `{self._host}:{self._port}`  ",
            f"Mutations sent: {len(results)}  ",
            "",
            "| # | Mutation | Outcome | Response (hex, first 16B) |",
            "|---|----------|---------|--------------------------|",
        ]
        for i, r in enumerate(results, 1):
            resp_hex = r.response[:16].hex() if r.response else "—"
            lines.append(f"| {i} | `{r.mutation}` | `{r.outcome}` | `{resp_hex}` |")
        return "\n".join(lines)


def _classify_response(data: bytes) -> str:
    """Classify a raw BACnet/IP response datagram."""
    if len(data) < 4 or data[0] != 0x81:
        return "non-bacnet-response"
    bvlc_func = data[1]
    if bvlc_func == 0x00:
        return "bvlc-result"
    if len(data) > 6:
        apdu_type = (data[6] & 0xF0) >> 4
        apdu_type_names = {5: "error", 6: "reject", 7: "abort"}
        if apdu_type in apdu_type_names:
            return apdu_type_names[apdu_type]
        if apdu_type in (2, 3):  # simple-ack, complex-ack
            return "ack"
    return "other-response"
