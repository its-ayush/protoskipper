"""IEC 104 bus-load test scenario — P5.B.1 of docs/internal/EXECUTION_PLAN.md.

Measures general-interrogation round-trip latency and point throughput against
an IEC 104 slave device and reports p50/p95/p99 latency, peak PPS, and total
elapsed time.

Usage::

    protoskipper run examples/scenarios/bus_load_test.py <host> \\
        [--port PORT] [--ca CA] [--rounds N] [--verbose]

Arguments
---------
host        IP address or hostname of the IEC 104 slave.
--port      TCP port (default: 2404).
--ca        Common Address of the ASDU (default: 1).
--rounds    Number of GI rounds to run (default: 10).
--verbose   Print per-round detail.

Exit codes: 0 success, 1 connection / protocol error, 2 bad arguments.

Example::

    protoskipper run examples/scenarios/bus_load_test.py 192.168.1.100 \\
        --ca 1 --rounds 20 --verbose

Output (example)::

    ProtoSkipper bus-load test — 192.168.1.100:2404 CA=1
    ─────────────────────────────────────────────────────
    Rounds           : 10
    Total points     : 1 500
    Peak PPS         : 312.4
    Elapsed          : 4.803 s
    GI latency p50   : 480.2 ms
    GI latency p95   : 501.8 ms
    GI latency p99   : 508.3 ms
    ─────────────────────────────────────────────────────
    Result: PASS  (all rounds completed, no errors)
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Public data model (importable for unit tests)
# ---------------------------------------------------------------------------


@dataclass
class RoundResult:
    """Outcome of a single GI round."""

    round_num: int
    latency_s: float
    point_count: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class BusLoadStats:
    """Aggregated statistics across all completed rounds."""

    host: str
    port: int
    ca: int
    rounds_requested: int
    results: list[RoundResult] = field(default_factory=list)

    # Computed lazily by compute()
    _computed: bool = field(default=False, repr=False, compare=False)
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    total_points: int = 0
    peak_pps: float = 0.0
    elapsed_s: float = 0.0
    errors: int = 0

    def compute(self) -> None:
        """Populate aggregate fields from *results*. Idempotent."""
        if self._computed:
            return
        ok_results = [r for r in self.results if r.ok]
        self.errors = len(self.results) - len(ok_results)
        if not ok_results:
            self._computed = True
            return
        latencies_ms = [r.latency_s * 1000 for r in ok_results]
        latencies_ms_sorted = sorted(latencies_ms)
        n = len(latencies_ms_sorted)

        def pct(p: float) -> float:
            idx = max(0, min(n - 1, int(p / 100 * n)))
            return latencies_ms_sorted[idx]

        self.latency_p50_ms = pct(50)
        self.latency_p95_ms = pct(95)
        self.latency_p99_ms = pct(99)
        self.total_points = sum(r.point_count for r in ok_results)
        self.elapsed_s = sum(r.latency_s for r in ok_results)
        if self.elapsed_s > 0:
            self.peak_pps = max(r.point_count / r.latency_s for r in ok_results if r.latency_s > 0)
        self._computed = True


def format_report(stats: BusLoadStats) -> str:
    """Return a human-readable report string.

    The stats object must have had :meth:`BusLoadStats.compute` called first.
    """
    bar = "\u2500" * 53
    ok_rounds = [r for r in stats.results if r.ok]
    status = "PASS" if stats.errors == 0 and ok_rounds else "FAIL"
    reason = (
        f"all {len(ok_rounds)} rounds completed, no errors"
        if status == "PASS"
        else f"{stats.errors} error(s), {len(ok_rounds)} OK"
    )
    lines = [
        f"ProtoSkipper bus-load test \u2014 {stats.host}:{stats.port} CA={stats.ca}",
        bar,
        f"{'Rounds':<17}: {len(stats.results)} / {stats.rounds_requested}",
        f"{'Total points':<17}: {stats.total_points:,}",
        f"{'Peak PPS':<17}: {stats.peak_pps:.1f}",
        f"{'Elapsed':<17}: {stats.elapsed_s:.3f} s",
        f"{'GI latency p50':<17}: {stats.latency_p50_ms:.1f} ms",
        f"{'GI latency p95':<17}: {stats.latency_p95_ms:.1f} ms",
        f"{'GI latency p99':<17}: {stats.latency_p99_ms:.1f} ms",
        bar,
        f"Result: {status}  ({reason})",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse *argv* (excluding the script path) into a Namespace."""
    parser = argparse.ArgumentParser(
        prog="bus_load_test",
        description="IEC 104 bus-load test — measure GI latency and point throughput.",
        add_help=True,
    )
    parser.add_argument("host", help="IEC 104 slave IP address or hostname.")
    parser.add_argument("--port", type=int, default=2404, help="TCP port (default: 2404).")
    parser.add_argument(
        "--ca", type=int, default=1, help="Common Address of the ASDU (default: 1)."
    )
    parser.add_argument("--rounds", type=int, default=10, help="Number of GI rounds (default: 10).")
    parser.add_argument("--verbose", action="store_true", help="Print per-round detail.")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main entry point — only executed when run via ``protoskipper run``
# ---------------------------------------------------------------------------


def _run(ns_iec104: Any, args: argparse.Namespace, logger: Any) -> BusLoadStats:
    """Core run loop; separated for testability."""
    stats = BusLoadStats(
        host=args.host,
        port=args.port,
        ca=args.ca,
        rounds_requested=args.rounds,
    )
    with ns_iec104.MasterSession(args.host, port=args.port, ca=args.ca, allow_writes=False) as sess:
        for i in range(1, args.rounds + 1):
            t0 = time.monotonic()
            try:
                asdus = sess.gi()
                latency = time.monotonic() - t0
                point_count = sum(len(a.elements) for a in asdus)
                result = RoundResult(
                    round_num=i,
                    latency_s=latency,
                    point_count=point_count,
                )
                if args.verbose:
                    logger.info(
                        "Round %d/%d: %d points in %.1f ms",
                        i,
                        args.rounds,
                        point_count,
                        latency * 1000,
                    )
            except Exception as exc:
                result = RoundResult(
                    round_num=i,
                    latency_s=0.0,
                    point_count=0,
                    error=str(exc),
                )
                logger.warning("Round %d error: %s", i, exc)
            stats.results.append(result)

    stats.compute()
    return stats


# Only runs when invoked via ``protoskipper run``
if "iec104" in dir():
    try:
        _args = parse_args(argv)  # type: ignore[name-defined]  # noqa: F821  # injected
    except SystemExit as _e:
        raise SystemExit(_e.code) from _e

    _stats = _run(iec104, _args, log)  # type: ignore[name-defined]  # noqa: F821  # injected
    print(format_report(_stats))
    if _stats.errors > 0:
        raise SystemExit(1)
