# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for newly implemented BACnet features.

Covers:
  - evaluate_weekly_schedule (P7.F.2)
  - BacnetSimulator trigger_event, alarm_log, clear_alarm_log (P7.F.3)
  - BacnetSimulator run_script, inject_sequence (P7.F.4)
  - BACnetFuzzer construction and mutation generators (P7.H.3)
  - ConformanceRunner list_profiles, ConformanceReport to_markdown/to_dict (P7.H.2)
  - BACnetSCSession constructor validation (P7.C)
  - MSTPSession constructor validation (P7.D)
  - BACnetIPv6Session constructor validation (P7.B.2)
  - PcapReader import error (P7.G)
  - PointsDiff constructor (P7.H.6)
  - DiffResult output methods (P7.H.6)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# P7.F.2 — evaluate_weekly_schedule
# ---------------------------------------------------------------------------


def _make_day_entries(*args: tuple[str, Any]) -> list[tuple[str, Any]]:
    """Build a day-schedule list: [(time_str, value), ...]"""
    return list(args)


def _full_week(day_entries: list[dict[str, Any]]) -> list[Any]:
    """7-day schedule where every day has the same entries."""
    return [day_entries] * 7


class TestEvaluateWeeklySchedule:
    def test_monday_returns_correct_value(self) -> None:
        from protoskipper.builtin_drivers.bacnet.simulator import evaluate_weekly_schedule

        entries = _make_day_entries(("08:00:00", "on"), ("18:00:00", "off"))
        schedule = _full_week(entries)
        # Monday 09:00 → should be "on"
        dt = datetime(2024, 1, 15, 9, 0, 0)  # 2024-01-15 is a Monday
        assert evaluate_weekly_schedule(schedule, dt) == "on"

    def test_monday_after_second_entry(self) -> None:
        from protoskipper.builtin_drivers.bacnet.simulator import evaluate_weekly_schedule

        entries = _make_day_entries(("08:00:00", "on"), ("18:00:00", "off"))
        schedule = _full_week(entries)
        dt = datetime(2024, 1, 15, 19, 0, 0)
        assert evaluate_weekly_schedule(schedule, dt) == "off"

    def test_before_first_entry_returns_none(self) -> None:
        from protoskipper.builtin_drivers.bacnet.simulator import evaluate_weekly_schedule

        entries = _make_day_entries(("08:00:00", "on"))
        schedule = _full_week(entries)
        dt = datetime(2024, 1, 15, 6, 0, 0)  # before 08:00
        result = evaluate_weekly_schedule(schedule, dt)
        assert result is None

    def test_sunday_uses_day_index_6(self) -> None:
        from protoskipper.builtin_drivers.bacnet.simulator import evaluate_weekly_schedule

        # Sunday = index 6 in ASHRAE convention (Monday=0)
        # Build a schedule where only Sunday has entries
        schedule: list[Any] = [[] for _ in range(6)]
        schedule.append(_make_day_entries(("00:00:00", "weekend")))
        dt = datetime(2024, 1, 21, 12, 0, 0)  # 2024-01-21 is a Sunday
        assert evaluate_weekly_schedule(schedule, dt) == "weekend"

    def test_empty_schedule_returns_none(self) -> None:
        from protoskipper.builtin_drivers.bacnet.simulator import evaluate_weekly_schedule

        schedule: list[list[Any]] = [[] for _ in range(7)]
        dt = datetime(2024, 1, 15, 12, 0, 0)
        assert evaluate_weekly_schedule(schedule, dt) is None

    def test_boundary_at_exact_time(self) -> None:
        from protoskipper.builtin_drivers.bacnet.simulator import evaluate_weekly_schedule

        entries = _make_day_entries(("08:00:00", "on"), ("18:00:00", "off"))
        schedule = _full_week(entries)
        dt = datetime(2024, 1, 15, 18, 0, 0)  # exactly at 18:00
        assert evaluate_weekly_schedule(schedule, dt) == "off"

    def test_wednesday(self) -> None:
        from protoskipper.builtin_drivers.bacnet.simulator import evaluate_weekly_schedule

        entries = _make_day_entries(("07:00:00", "wakeup"))
        schedule = _full_week(entries)
        dt = datetime(2024, 1, 17, 10, 0, 0)  # Wednesday
        assert evaluate_weekly_schedule(schedule, dt) == "wakeup"


# ---------------------------------------------------------------------------
# P7.F.3 — trigger_event / alarm_log
# ---------------------------------------------------------------------------


class TestSimulatorAlarmLog:
    def _make_sim(self) -> Any:
        from protoskipper.builtin_drivers.bacnet.simulator import BacnetSimulator

        return BacnetSimulator()

    def test_alarm_log_starts_empty(self) -> None:
        sim = self._make_sim()
        assert sim.alarm_log() == []

    def test_trigger_event_adds_to_log(self) -> None:
        sim = self._make_sim()
        sim.trigger_event("analog-input:0", "high-limit", "alarm", 10, "High pressure")
        log = sim.alarm_log()
        assert len(log) == 1
        assert log[0]["object_id"] == "analog-input:0"
        assert log[0]["event_state"] == "high-limit"
        assert log[0]["notify_type"] == "alarm"
        assert log[0]["priority"] == 10
        assert log[0]["message_text"] == "High pressure"

    def test_clear_alarm_log(self) -> None:
        sim = self._make_sim()
        sim.trigger_event("binary-input:1", "offnormal", "event", 5, "")
        assert len(sim.alarm_log()) == 1
        sim.clear_alarm_log()
        assert sim.alarm_log() == []

    def test_multiple_events(self) -> None:
        sim = self._make_sim()
        for i in range(3):
            sim.trigger_event(f"analog-input:{i}", "high-limit", "alarm", 10, "")
        assert len(sim.alarm_log()) == 3


# ---------------------------------------------------------------------------
# P7.F.4 — run_script / inject_sequence
# ---------------------------------------------------------------------------


class TestSimulatorScript:
    def _make_sim(self) -> Any:
        from protoskipper.builtin_drivers.bacnet.simulator import BacnetSimulator

        sim = BacnetSimulator()
        return sim

    def test_run_script_executes_python(self) -> None:
        sim = self._make_sim()
        sim.run_script("sim.trigger_event('analog-input:0', 'high-limit', 'alarm', 10, 'scripted')")
        assert len(sim.alarm_log()) == 1
        assert sim.alarm_log()[0]["message_text"] == "scripted"

    def test_run_script_raises_on_bad_code(self) -> None:
        sim = self._make_sim()
        with pytest.raises((ValueError, RuntimeError, SyntaxError, Exception)):
            sim.run_script("raise ValueError('test error')")


# ---------------------------------------------------------------------------
# P7.H.3 — BACnetFuzzer
# ---------------------------------------------------------------------------


class TestBACnetFuzzer:
    def test_confirmed_false_raises(self) -> None:
        from protoskipper.builtin_drivers.bacnet.fuzzer import BACnetFuzzer

        with pytest.raises(RuntimeError, match="confirmed=True"):
            BACnetFuzzer("127.0.0.1", confirmed=False)

    def test_bvlc_mutations_count(self) -> None:
        from protoskipper.builtin_drivers.bacnet.fuzzer import _bvlc_mutations

        mutations = list(_bvlc_mutations())
        assert len(mutations) >= 6

    def test_npdu_mutations_count(self) -> None:
        from protoskipper.builtin_drivers.bacnet.fuzzer import _npdu_mutations

        mutations = list(_npdu_mutations())
        assert len(mutations) >= 4

    def test_apdu_mutations_count(self) -> None:
        from protoskipper.builtin_drivers.bacnet.fuzzer import _apdu_mutations

        mutations = list(_apdu_mutations())
        assert len(mutations) >= 5

    def test_sequence_mutations_count(self) -> None:
        from protoskipper.builtin_drivers.bacnet.fuzzer import _sequence_mutations

        mutations = list(_sequence_mutations())
        assert len(mutations) >= 1

    def test_classify_response_with_data(self) -> None:
        from protoskipper.builtin_drivers.bacnet.fuzzer import _classify_response

        # BVLC-type=0x81 + NPDU + SimpleACK prefix → should classify as ACK-ish
        data = bytes([0x81, 0x0A, 0x00, 0x08, 0x01, 0x00, 0x20, 0x00])
        result = _classify_response(data)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mutations_categories_all_present(self) -> None:
        from protoskipper.builtin_drivers.bacnet.fuzzer import MUTATION_CATEGORIES

        assert "bvlc" in MUTATION_CATEGORIES
        assert "npdu" in MUTATION_CATEGORIES
        assert "apdu" in MUTATION_CATEGORIES
        assert "sequence" in MUTATION_CATEGORIES


# ---------------------------------------------------------------------------
# P7.H.2 — ConformanceRunner / ConformanceReport
# ---------------------------------------------------------------------------


class TestConformanceRunner:
    def test_list_profiles(self) -> None:
        from protoskipper.builtin_drivers.bacnet.conformance.runner import list_profiles

        profiles = list_profiles()
        assert "B-BC" in profiles
        assert "B-OWS" in profiles
        assert "B-BBMD" in profiles
        assert len(profiles) >= 8

    def test_unknown_profile_raises(self) -> None:
        from protoskipper.builtin_drivers.bacnet.conformance.runner import ConformanceRunner

        mock_session = MagicMock()
        with pytest.raises(ValueError, match="Unknown profile"):
            ConformanceRunner(mock_session, profile="B-NONEXISTENT")

    def test_report_to_markdown(self) -> None:
        from protoskipper.builtin_drivers.bacnet.conformance.runner import (
            ConformanceReport,
            TestResult,
        )

        results = [
            TestResult("DS-RP-A-1", "DS-RP-A", "§15.5", "desc", True, detail="ok"),
            TestResult("DS-RP-A-2", "DS-RP-A", "§15.5", "desc", False, error="timeout"),
            TestResult("DS-RP-A-3", "DS-RP-A", "§15.5", "desc", False, skip_reason="LAB only"),
        ]
        report = ConformanceReport(
            profile="B-OWS",
            target="192.168.1.1",
            operator="tester",
            started_at=datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc),
            results=results,
        )
        md = report.to_markdown()
        assert "B-OWS" in md
        assert "DS-RP-A-1" in md
        assert "1 PASS" in md
        assert "1 FAIL" in md
        assert "1 SKIP" in md

    def test_report_to_dict(self) -> None:
        from protoskipper.builtin_drivers.bacnet.conformance.runner import (
            ConformanceReport,
            TestResult,
        )

        results = [TestResult("TC-1", "BIBB-A", "§1.1", "desc", True)]
        report = ConformanceReport(
            profile="B-BC",
            target="10.0.0.1",
            operator="auto",
            started_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
            results=results,
        )
        d = report.to_dict()
        assert d["profile"] == "B-BC"
        assert d["passed"] == 1
        assert d["failed"] == 0
        assert len(d["results"]) == 1

    def test_report_statistics(self) -> None:
        from protoskipper.builtin_drivers.bacnet.conformance.runner import (
            ConformanceReport,
            TestResult,
        )

        report = ConformanceReport(
            profile="B-ASC",
            target="10.0.0.1",
            operator="auto",
            started_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
            results=[
                TestResult("T1", "B", "§1", "d", True),
                TestResult("T2", "B", "§1", "d", False, error="e"),
                TestResult("T3", "B", "§1", "d", False, skip_reason="s"),
            ],
        )
        assert report.passed == 1
        assert report.failed == 1
        assert report.skipped == 1


# ---------------------------------------------------------------------------
# P7.C — BACnetSCSession
# ---------------------------------------------------------------------------


class TestBACnetSCSession:
    def test_valid_construction(self) -> None:
        from protoskipper.builtin_drivers.bacnet.sc import BACnetSCSession

        sess = BACnetSCSession(
            "wss://hub.example.com:47808",
            ca_cert="/etc/ssl/ca.pem",
            client_cert="/etc/ssl/client.pem",
            client_key="/etc/ssl/client.key",
        )
        assert sess.hub_uri.startswith("wss://")

    def test_invalid_uri_raises(self) -> None:
        from protoskipper.builtin_drivers.bacnet.sc import BACnetSCSession

        with pytest.raises(ValueError, match="wss://"):
            BACnetSCSession(
                "ws://hub.example.com",  # not secure
                ca_cert="/etc/ssl/ca.pem",
                client_cert="/etc/ssl/client.pem",
                client_key="/etc/ssl/client.key",
            )

    def test_connect_raises_not_implemented(self) -> None:
        from protoskipper.builtin_drivers.bacnet.sc import BACnetSCSession

        sess = BACnetSCSession(
            "wss://hub.example.com",
            ca_cert="ca.pem",
            client_cert="c.pem",
            client_key="k.pem",
        )
        with pytest.raises(NotImplementedError):
            sess.connect()

    def test_repr(self) -> None:
        from protoskipper.builtin_drivers.bacnet.sc import BACnetSCSession

        sess = BACnetSCSession(
            "wss://hub.example.com",
            ca_cert="ca.pem",
            client_cert="c.pem",
            client_key="k.pem",
            device_id=42,
        )
        assert "42" in repr(sess)


# ---------------------------------------------------------------------------
# P7.D — MSTPSession
# ---------------------------------------------------------------------------


class TestMSTPSession:
    def test_valid_construction(self) -> None:
        from protoskipper.builtin_drivers.bacnet.mstp import MSTPSession

        sess = MSTPSession("/dev/ttyUSB0", mac=5, baud=76800)
        assert sess.mac == 5

    def test_invalid_mac_raises(self) -> None:
        from protoskipper.builtin_drivers.bacnet.mstp import MSTPSession

        with pytest.raises(ValueError, match="0-127"):
            MSTPSession("/dev/ttyUSB0", mac=200)

    def test_open_raises_not_implemented(self) -> None:
        from protoskipper.builtin_drivers.bacnet.mstp import MSTPSession

        sess = MSTPSession("/dev/ttyUSB0", mac=1)
        with pytest.raises(NotImplementedError):
            sess.open()

    def test_repr(self) -> None:
        from protoskipper.builtin_drivers.bacnet.mstp import MSTPSession

        sess = MSTPSession("/dev/ttyS0", mac=3, baud=9600)
        assert "ttyS0" in repr(sess)
        assert "INITIALIZE" in repr(sess)

    def test_constants_defined(self) -> None:
        from protoskipper.builtin_drivers.bacnet.mstp import MSTP_CONSTANTS

        assert "PREAMBLE_1" in MSTP_CONSTANTS
        assert MSTP_CONSTANTS["PREAMBLE_1"] == 0x55

    def test_state_enum_idle(self) -> None:
        from protoskipper.builtin_drivers.bacnet.mstp import MSTPState

        assert MSTPState.IDLE == 1


# ---------------------------------------------------------------------------
# P7.B.2 — BACnetIPv6Session
# ---------------------------------------------------------------------------


class TestBACnetIPv6Session:
    def test_valid_construction(self) -> None:
        from protoskipper.builtin_drivers.bacnet.ipv6 import BACnetIPv6Session

        sess = BACnetIPv6Session("fe80::1", interface="eth0", device_id=5678)
        assert sess.device_id == 5678

    def test_ipv4_address_raises(self) -> None:
        from protoskipper.builtin_drivers.bacnet.ipv6 import BACnetIPv6Session

        with pytest.raises(ValueError, match="IPv6"):
            BACnetIPv6Session("192.168.1.1")

    def test_connect_raises_not_implemented(self) -> None:
        from protoskipper.builtin_drivers.bacnet.ipv6 import BACnetIPv6Session

        sess = BACnetIPv6Session("::1")
        with pytest.raises(NotImplementedError):
            sess.connect()

    def test_multicast_address(self) -> None:
        from protoskipper.builtin_drivers.bacnet.ipv6 import BACnetIPv6Session

        sess = BACnetIPv6Session("::1", multicast_scope="e")
        assert "BAC0" in sess.all_devices_multicast

    def test_bvlci6_functions_defined(self) -> None:
        from protoskipper.builtin_drivers.bacnet.ipv6 import BVLCI6_FUNCTIONS

        assert 0x00 in BVLCI6_FUNCTIONS  # BVLCI6-Result
        assert 0x01 in BVLCI6_FUNCTIONS  # Original-Unicast-NPDU


# ---------------------------------------------------------------------------
# P7.G — PcapReader import error
# ---------------------------------------------------------------------------


class TestPcapReader:
    def test_import_error_on_missing_dpkt(self) -> None:
        from protoskipper.builtin_drivers.bacnet.pcap import PcapReader, PcapUnavailable

        with patch.dict("sys.modules", {"dpkt": None}), pytest.raises(PcapUnavailable):
            PcapReader("nonexistent.pcap")

    def test_bacnet_port_constant(self) -> None:
        from protoskipper.builtin_drivers.bacnet.pcap import BACNET_PORT

        assert BACNET_PORT == 47808

    def test_bvlc_functions_table(self) -> None:
        from protoskipper.builtin_drivers.bacnet.pcap import BVLC_FUNCTIONS

        assert 0x0A in BVLC_FUNCTIONS  # Original-Unicast-NPDU
        assert 0x0B in BVLC_FUNCTIONS  # Original-Broadcast-NPDU
        assert 0x04 in BVLC_FUNCTIONS  # Forwarded-NPDU

    def test_apdu_types_table(self) -> None:
        from protoskipper.builtin_drivers.bacnet.pcap import APDU_TYPES

        assert APDU_TYPES[0] == "Confirmed-Request"
        assert APDU_TYPES[1] == "Unconfirmed-Request"


# ---------------------------------------------------------------------------
# P7.H.6 — DiffResult
# ---------------------------------------------------------------------------


class TestDiffResult:
    def _make_result(self) -> Any:
        from protoskipper.builtin_drivers.bacnet.diff import DiffResult, DiffRow

        return DiffResult(
            rows=[
                DiffRow(
                    objid="analog-input:0",
                    status="expected-only",
                    expected={"objectName": "AI-0"},
                ),
                DiffRow(
                    objid="analog-input:1",
                    status="actual-only",
                    actual={"objectName": "AI-1"},
                ),
                DiffRow(
                    objid="analog-input:2",
                    status="mismatch",
                    expected={"objectName": "Pressure", "units": "pascals"},
                    actual={"objectName": "Pressure", "units": "bars"},
                    mismatches=["units"],
                ),
                DiffRow(
                    objid="analog-input:3",
                    status="match",
                    expected={"objectName": "Temp"},
                    actual={"objectName": "Temp"},
                ),
            ],
            expected_path="test.ede.csv",
            device_address="192.168.1.10",
        )

    def test_expected_only_filter(self) -> None:
        result = self._make_result()
        assert len(result.expected_only()) == 1
        assert result.expected_only()[0].objid == "analog-input:0"

    def test_actual_only_filter(self) -> None:
        result = self._make_result()
        assert len(result.actual_only()) == 1

    def test_mismatches_filter(self) -> None:
        result = self._make_result()
        assert len(result.mismatches()) == 1
        assert "units" in result.mismatches()[0].mismatches

    def test_matches_filter(self) -> None:
        result = self._make_result()
        assert len(result.matches()) == 1

    def test_to_markdown(self) -> None:
        result = self._make_result()
        md = result.to_markdown()
        assert "BACnet Points-List Diff" in md
        assert "analog-input:0" in md
        assert "mismatch" in md.lower() or "Mismatch" in md

    def test_to_csv_returns_string(self) -> None:
        result = self._make_result()
        csv_str = result.to_csv()
        assert isinstance(csv_str, str)
        assert "objid" in csv_str  # header row
        assert "analog-input:0" in csv_str

    def test_points_diff_requires_expected(self) -> None:
        from protoskipper.builtin_drivers.bacnet.diff import PointsDiff

        mock_session = MagicMock()
        with pytest.raises(ValueError, match="expected"):
            PointsDiff(mock_session)
