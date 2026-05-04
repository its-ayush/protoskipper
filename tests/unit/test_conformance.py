# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for conformance test framework — P8.F."""

from __future__ import annotations

import pytest
from protoskipper_iec61850.conformance.runner import (
    ConformanceReport,
    ConformanceRunner,
    TestStatus,
)
from protoskipper_iec61850.conformance.schema import (
    ConformanceProfile,
    ConformanceTestCase,
    Severity,
    TestStep,
    list_builtin_profiles,
    load_builtin_profile,
)

# ===========================================================================
# TestConformanceSchema
# ===========================================================================


class TestConformanceSchema:
    def _make_tc(self, test_id: str = "TC_001", severity: Severity = Severity.MANDATORY):
        return ConformanceTestCase(
            test_id=test_id,
            name="Test name",
            description="Desc",
            service="Read",
            severity=severity,
            steps=[
                TestStep(action="connect"),
                TestStep(action="Read", params={"domain": "D1", "item_id": "V1"}),
            ],
        )

    def test_test_case_roundtrip(self):
        tc = self._make_tc()
        tc2 = ConformanceTestCase.from_dict(tc.to_dict())
        assert tc2.test_id == "TC_001"
        assert tc2.severity == Severity.MANDATORY
        assert len(tc2.steps) == 2
        assert tc2.steps[1].action == "Read"
        assert tc2.steps[1].params == {"domain": "D1", "item_id": "V1"}

    def test_profile_roundtrip(self):
        profile = ConformanceProfile(
            profile_id="test_profile",
            edition="2.1",
            category="MMS",
            description="Test",
            tests=[self._make_tc("TC_001"), self._make_tc("TC_002", Severity.OPTIONAL)],
        )
        profile2 = ConformanceProfile.from_dict(profile.to_dict())
        assert profile2.profile_id == "test_profile"
        assert len(profile2.tests) == 2
        assert profile2.tests[1].severity == Severity.OPTIONAL

    def test_mandatory_tests_filter(self):
        profile = ConformanceProfile(
            profile_id="p",
            edition="2",
            category="C",
            description="D",
            tests=[
                self._make_tc("TC_001", Severity.MANDATORY),
                self._make_tc("TC_002", Severity.OPTIONAL),
                self._make_tc("TC_003", Severity.CONDITIONAL),
            ],
        )
        mand = profile.mandatory_tests()
        assert len(mand) == 1
        assert mand[0].test_id == "TC_001"

    def test_by_service_filter(self):
        tc_read = ConformanceTestCase("TC_R", "Read test", "", service="Read", steps=[])
        tc_write = ConformanceTestCase("TC_W", "Write test", "", service="Write", steps=[])
        profile = ConformanceProfile(
            profile_id="p",
            edition="2",
            category="C",
            description="D",
            tests=[tc_read, tc_write],
        )
        reads = profile.by_service("Read")
        assert len(reads) == 1
        assert reads[0].test_id == "TC_R"


# ===========================================================================
# TestBuiltinProfiles
# ===========================================================================


class TestBuiltinProfiles:
    def test_list_builtin_profiles_non_empty(self):
        profiles = list_builtin_profiles()
        assert len(profiles) >= 2
        assert "mms_ed21" in profiles
        assert "goose_pub_ed21" in profiles

    def test_load_mms_ed21(self):
        profile = load_builtin_profile("mms_ed21")
        assert profile.profile_id == "mms_ed21"
        assert profile.edition == "2.1"
        assert len(profile.tests) >= 5
        # All should have steps
        for tc in profile.tests:
            assert len(tc.steps) >= 1

    def test_load_goose_pub_ed21(self):
        profile = load_builtin_profile("goose_pub_ed21")
        assert profile.profile_id == "goose_pub_ed21"
        assert len(profile.tests) >= 3

    def test_load_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            load_builtin_profile("nonexistent_xyz")

    def test_load_from_yaml_file(self, tmp_path):
        yaml_content = """
profile_id: test_custom
edition: "2.0"
category: SV
description: Custom SV profile
tests:
  - id: TC_SV_001
    name: "SV subscription"
    description: "Test SV subscription"
    service: SV
    severity: MANDATORY
    steps:
      - action: subscribe_sv
"""
        p = tmp_path / "test_custom.yaml"
        p.write_text(yaml_content, encoding="utf-8")
        profile = ConformanceProfile.load_yaml(p)
        assert profile.profile_id == "test_custom"
        assert len(profile.tests) == 1
        assert profile.tests[0].test_id == "TC_SV_001"

    def test_load_invalid_yaml_raises(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("- just a list", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            ConformanceProfile.load_yaml(p)


# ===========================================================================
# TestConformanceRunner
# ===========================================================================


def _make_simple_profile(n_tests: int = 3) -> ConformanceProfile:
    tests = [
        ConformanceTestCase(
            test_id=f"TC_{i:03d}",
            name=f"Test {i}",
            description="",
            service="Read",
            steps=[
                TestStep(action="connect"),
                TestStep(action="assert_connection", params={"status": "connected"}),
            ],
        )
        for i in range(1, n_tests + 1)
    ]
    return ConformanceProfile(
        profile_id="test_profile", edition="2", category="Test", description="", tests=tests
    )


class TestConformanceRunner:
    def test_all_pass_with_no_client(self):
        """Without an mms_client, all built-in stubs return ok."""
        profile = load_builtin_profile("mms_ed21")
        runner = ConformanceRunner(profile, session_ctx={})
        report = runner.run()
        assert report.total == len(profile.tests)
        # All should pass since stubs return ok
        assert report.passed == report.total

    def test_overall_pass_all_mandatory_pass(self):
        profile = _make_simple_profile(3)
        runner = ConformanceRunner(profile, session_ctx={"connected": True})
        report = runner.run()
        assert report.overall_pass is True

    def test_progress_callback_called(self):
        progress: list[str] = []
        profile = _make_simple_profile(2)

        runner = ConformanceRunner(
            profile,
            session_ctx={"connected": True},
            on_progress=lambda tc, r: progress.append(tc.test_id),
        )
        runner.run()
        assert len(progress) == 2

    def test_skip_optional(self):
        tc_mand = ConformanceTestCase(
            "TC_M",
            "Mandatory",
            "",
            "Read",
            severity=Severity.MANDATORY,
            steps=[TestStep("connect")],
        )
        tc_opt = ConformanceTestCase(
            "TC_O",
            "Optional",
            "",
            "Write",
            severity=Severity.OPTIONAL,
            steps=[TestStep("connect")],
        )
        profile = ConformanceProfile(
            profile_id="p",
            edition="2",
            category="C",
            description="D",
            tests=[tc_mand, tc_opt],
        )
        runner = ConformanceRunner(profile, skip_optional=True)
        report = runner.run()
        assert report.skipped == 1
        skipped_ids = {r.test_case.test_id for r in report.results if r.status == TestStatus.SKIP}
        assert "TC_O" in skipped_ids

    def test_fail_on_step_failure(self):
        # Inject a handler that always fails
        def _always_fail(step, ctx):
            from protoskipper_iec61850.conformance.runner import StepResult

            return StepResult(step.action, "fail", "injected failure")

        tc = ConformanceTestCase(
            "TC_F",
            "Fail test",
            "",
            "Read",
            severity=Severity.MANDATORY,
            steps=[TestStep("always_fail")],
        )
        profile = ConformanceProfile(
            profile_id="p", edition="2", category="C", description="D", tests=[tc]
        )
        runner = ConformanceRunner(profile, extra_handlers={"always_fail": _always_fail})
        report = runner.run()
        assert report.failed == 1
        assert report.overall_pass is False

    def test_report_summary_counts(self):
        profile = _make_simple_profile(4)
        runner = ConformanceRunner(profile)
        report = runner.run()
        assert report.total == 4
        assert report.passed + report.failed + report.errored + report.skipped == 4

    def test_operator_in_report(self):
        profile = _make_simple_profile(1)
        runner = ConformanceRunner(profile, operator="Alice Engineer")
        report = runner.run()
        assert report.operator == "Alice Engineer"

    def test_device_info_in_report(self):
        profile = _make_simple_profile(1)
        runner = ConformanceRunner(profile, device_info={"firmware": "v2.3.1", "model": "IED-X100"})
        report = runner.run()
        assert report.device_info["firmware"] == "v2.3.1"

    def test_elapsed_ms_positive(self):
        profile = _make_simple_profile(1)
        report = ConformanceRunner(profile).run()
        assert report.elapsed_ms >= 0


# ===========================================================================
# TestConformanceReporter
# ===========================================================================


class TestConformanceReporter:
    def _make_report(self) -> ConformanceReport:
        profile = _make_simple_profile(2)
        runner = ConformanceRunner(profile)
        return runner.run()

    def test_write_html_report(self, tmp_path):
        from protoskipper_iec61850.conformance.reporter import write_html_report

        report = self._make_report()
        path = tmp_path / "report.html"
        write_html_report(report, path, audit_sig="abc123def456")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "IEC 61850 Conformance Test Report" in content
        assert "abc123def456" in content
        assert "PASS" in content

    def test_html_report_contains_test_ids(self, tmp_path):
        from protoskipper_iec61850.conformance.reporter import write_html_report

        profile = load_builtin_profile("mms_ed21")
        runner = ConformanceRunner(profile)
        report = runner.run()
        path = tmp_path / "mms_report.html"
        write_html_report(report, path)
        content = path.read_text(encoding="utf-8")
        assert "TC_MMS_001" in content
        assert "TC_MMS_002" in content

    def test_pdf_raises_on_missing_weasyprint(self, tmp_path, monkeypatch):
        """If weasyprint isn't installed, write_pdf_report raises ImportError."""
        import sys

        from protoskipper_iec61850.conformance.reporter import write_pdf_report

        # Simulate weasyprint not being installed
        monkeypatch.setitem(sys.modules, "weasyprint", None)  # type: ignore[call-overload]
        report = self._make_report()
        path = tmp_path / "report.pdf"
        with pytest.raises((ImportError, TypeError)):
            write_pdf_report(report, path)
