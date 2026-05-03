# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for examples/scenarios/bus_load_test.py — P5.B.1."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load the scenario module without executing the script entry point.
# We rely on the guard ``if "iec104" in dir():`` so that importing the
# module does NOT attempt a real network connection.
# ---------------------------------------------------------------------------

_SCENARIO_PATH = Path(__file__).parents[2] / "examples" / "scenarios" / "bus_load_test.py"


def _load_scenario() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("bus_load_test", _SCENARIO_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec so that dataclass field-type
    # annotations resolve correctly (required by Python 3.14+).
    sys.modules["bus_load_test"] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        del sys.modules["bus_load_test"]
        raise
    return mod


_sc = _load_scenario()
BusLoadStats = _sc.BusLoadStats
RoundResult = _sc.RoundResult
format_report = _sc.format_report
parse_args = _sc.parse_args


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRoundResult:
    def test_ok_when_no_error(self) -> None:
        r = RoundResult(round_num=1, latency_s=0.5, point_count=10)
        assert r.ok is True

    def test_not_ok_when_error_set(self) -> None:
        r = RoundResult(round_num=1, latency_s=0.0, point_count=0, error="timeout")
        assert r.ok is False


class TestBusLoadStats:
    def _make_stats(self, latencies_s: list[float], points_per_round: int = 100) -> BusLoadStats:
        stats = BusLoadStats(host="127.0.0.1", port=2404, ca=1, rounds_requested=len(latencies_s))
        for i, lat in enumerate(latencies_s, start=1):
            stats.results.append(
                RoundResult(round_num=i, latency_s=lat, point_count=points_per_round)
            )
        stats.compute()
        return stats

    def test_compute_latency_percentiles(self) -> None:
        # 10 rounds of 1-10 ms each.
        # pct(p) uses idx = int(p/100 * n), so for n=10:
        #   p50 → idx 5 → sorted[5] = 6 ms
        #   p95 → idx 9 → sorted[9] = 10 ms
        #   p99 → idx 9 → sorted[9] = 10 ms
        stats = self._make_stats([i * 0.001 for i in range(1, 11)])
        assert stats.latency_p50_ms == pytest.approx(6.0)
        assert stats.latency_p95_ms == pytest.approx(10.0)
        assert stats.latency_p99_ms >= stats.latency_p95_ms >= stats.latency_p50_ms

    def test_total_points(self) -> None:
        stats = self._make_stats([0.1, 0.1, 0.1], points_per_round=200)
        assert stats.total_points == 600

    def test_peak_pps(self) -> None:
        # Round with 500 points in 1 s → PPS = 500
        stats = self._make_stats([1.0, 2.0], points_per_round=500)
        assert stats.peak_pps == pytest.approx(500.0, abs=1.0)

    def test_errors_counted(self) -> None:
        stats = BusLoadStats(host="h", port=2404, ca=1, rounds_requested=3)
        stats.results.append(RoundResult(1, 0.1, 10))
        stats.results.append(RoundResult(2, 0.0, 0, error="conn refused"))
        stats.results.append(RoundResult(3, 0.2, 10))
        stats.compute()
        assert stats.errors == 1

    def test_compute_idempotent(self) -> None:
        stats = self._make_stats([0.1, 0.2])
        p50_first = stats.latency_p50_ms
        stats.compute()  # second call
        assert stats.latency_p50_ms == p50_first

    def test_empty_results_no_crash(self) -> None:
        stats = BusLoadStats(host="h", port=2404, ca=1, rounds_requested=0)
        stats.compute()  # must not raise
        assert stats.total_points == 0
        assert stats.peak_pps == 0.0


class TestFormatReport:
    def _stats_all_ok(self) -> BusLoadStats:
        stats = BusLoadStats(host="10.0.0.5", port=2404, ca=1, rounds_requested=5)
        for i in range(1, 6):
            stats.results.append(RoundResult(i, 0.5, 100))
        stats.compute()
        return stats

    def test_contains_host_and_port(self) -> None:
        report = format_report(self._stats_all_ok())
        assert "10.0.0.5:2404" in report

    def test_pass_when_no_errors(self) -> None:
        report = format_report(self._stats_all_ok())
        assert "PASS" in report

    def test_fail_when_errors(self) -> None:
        stats = BusLoadStats(host="h", port=2404, ca=1, rounds_requested=2)
        stats.results.append(RoundResult(1, 0.0, 0, error="refused"))
        stats.results.append(RoundResult(2, 0.0, 0, error="refused"))
        stats.compute()
        assert "FAIL" in format_report(stats)


class TestParseArgs:
    def test_host_required(self) -> None:
        with pytest.raises(SystemExit):
            parse_args([])

    def test_defaults(self) -> None:
        args = parse_args(["192.168.1.1"])
        assert args.host == "192.168.1.1"
        assert args.port == 2404
        assert args.ca == 1
        assert args.rounds == 10
        assert args.verbose is False

    def test_override_all(self) -> None:
        args = parse_args(
            ["10.0.0.1", "--port", "19998", "--ca", "5", "--rounds", "20", "--verbose"]
        )
        assert args.port == 19998
        assert args.ca == 5
        assert args.rounds == 20
        assert args.verbose is True
