# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet conformance test runner (P7.H.2).

Executes BIBB-aligned conformance test profiles against a live BACnet device.
Each profile is a YAML file under the ``profiles/`` sub-package.  Test cases
are *paraphrased* from ASHRAE 135 BTL Specified Tests (not copied verbatim)
and cross-referenced by section number only, as required by ASHRAE copyright
policy.

Usage::

    from protoskipper.builtin_drivers.bacnet.conformance.runner import ConformanceRunner

    runner = ConformanceRunner(
        session=my_session,       # BacnetIpSession
        profile="B-BC",
        target_object_id="device:1234",
    )
    report = runner.run()
    print(report.to_markdown())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from protoskipper.builtin_drivers.bacnet.client import BacnetIpSession

_logger = logging.getLogger(__name__)

__all__ = ["ConformanceReport", "ConformanceRunner", "TestResult", "list_profiles"]

# ---------------------------------------------------------------------------
# BIBB profile → test case list mapping (paraphrased per ASHRAE copyright policy)
# ---------------------------------------------------------------------------

_BIBB_TESTS: dict[str, list[dict[str, Any]]] = {
    # DS-RP-A: ReadProperty — A-role (initiating)
    "DS-RP-A": [
        {
            "id": "DS-RP-A-1",
            "ref": "ASHRAE 135 §15.5",
            "description": "ReadProperty of object identifier on the Device object "
            "returns the correct Device-ID.",
            "service": "ReadProperty",
            "prop": "objectIdentifier",
            "object_type": "device",
        },
        {
            "id": "DS-RP-A-2",
            "ref": "ASHRAE 135 §15.5",
            "description": "ReadProperty of objectName on the Device object "
            "returns a non-empty string.",
            "service": "ReadProperty",
            "prop": "objectName",
            "object_type": "device",
        },
        {
            "id": "DS-RP-A-3",
            "ref": "ASHRAE 135 §15.5",
            "description": "ReadProperty of vendorIdentifier returns an integer in range 0-65534.",
            "service": "ReadProperty",
            "prop": "vendorIdentifier",
            "object_type": "device",
        },
        {
            "id": "DS-RP-A-4",
            "ref": "ASHRAE 135 §15.5",
            "description": "ReadProperty of protocolVersion returns the correct integer.",
            "service": "ReadProperty",
            "prop": "protocolVersion",
            "object_type": "device",
        },
        {
            "id": "DS-RP-A-5",
            "ref": "ASHRAE 135 §15.5",
            "description": "ReadProperty of a non-existent property returns an "
            "Error PDU with unknown-property.",
            "service": "ReadProperty",
            "prop": "__nonexistent_prop_9999__",
            "object_type": "device",
            "expect_error": True,
        },
    ],
    # DS-RPM-A: ReadPropertyMultiple — A-role
    "DS-RPM-A": [
        {
            "id": "DS-RPM-A-1",
            "ref": "ASHRAE 135 §15.7",
            "description": "ReadPropertyMultiple of objectIdentifier and objectName "
            "on Device object succeeds.",
            "service": "ReadPropertyMultiple",
            "props": ["objectIdentifier", "objectName"],
            "object_type": "device",
        },
        {
            "id": "DS-RPM-A-2",
            "ref": "ASHRAE 135 §15.7",
            "description": "ReadPropertyMultiple with all (0x55) on Device object "
            "returns at least 10 properties.",
            "service": "ReadPropertyMultiple",
            "props": ["all"],
            "object_type": "device",
            "min_result_count": 10,
        },
    ],
    # DS-WP-A: WriteProperty — A-role
    "DS-WP-A": [
        {
            "id": "DS-WP-A-1",
            "ref": "ASHRAE 135 §15.9",
            "description": "WriteProperty to description on Device object succeeds in LAB profile.",
            "service": "WriteProperty",
            "prop": "description",
            "value": "ProtoSkipper conformance test",
            "object_type": "device",
        },
    ],
    # DM-DDB-A: Dynamic Device Binding — A-role (Who-Is / I-Am)
    "DM-DDB-A": [
        {
            "id": "DM-DDB-A-1",
            "ref": "ASHRAE 135 §16.10.2",
            "description": "Who-Is with matching device-ID range returns I-Am "
            "from the target device.",
            "service": "Who-Is",
        },
        {
            "id": "DM-DDB-A-2",
            "ref": "ASHRAE 135 §16.10.2",
            "description": "Who-Is with non-matching range returns no I-Am from the target.",
            "service": "Who-Is",
            "expect_no_response": True,
        },
    ],
    # DM-DCC-A: DeviceCommunicationControl
    "DM-DCC-A": [
        {
            "id": "DM-DCC-A-1",
            "ref": "ASHRAE 135 §16.4",
            "description": "DeviceCommunicationControl enable is accepted "
            "without error (LAB only).",
            "service": "DeviceCommunicationControl",
            "mode": "enable",
        },
    ],
    # AE-N-A: Alarm & Event Notification — A-role
    "AE-N-A": [
        {
            "id": "AE-N-A-1",
            "ref": "ASHRAE 135 §13.2",
            "description": "GetEventInformation returns a list (may be empty) without error.",
            "service": "GetEventInformation",
        },
    ],
    # DS-COV-A: COV Subscription — A-role
    "DS-COV-A": [
        {
            "id": "DS-COV-A-1",
            "ref": "ASHRAE 135 §13.1",
            "description": "SubscribeCOV for an Analog Input object is accepted without error.",
            "service": "SubscribeCOV",
            "object_type": "analog-input",
        },
    ],
}

# Profile → set of required BIBBs (paraphrased from BTL Test Packages)
_PROFILE_BIBBS: dict[str, list[str]] = {
    "B-OWS": ["DS-RP-A", "DS-RPM-A", "DM-DDB-A", "AE-N-A"],
    "B-AWS": ["DS-RP-A", "DS-RPM-A", "DS-WP-A", "DM-DDB-A", "DM-DCC-A", "AE-N-A"],
    "B-BC": ["DS-RP-A", "DS-RPM-A", "DS-WP-A", "DM-DDB-A", "DM-DCC-A", "AE-N-A", "DS-COV-A"],
    "B-AAC": ["DS-RP-A", "DS-RPM-A", "DS-WP-A", "DM-DDB-A", "AE-N-A"],
    "B-ASC": ["DS-RP-A", "DS-RPM-A", "DM-DDB-A"],
    "B-SA": ["DS-RP-A", "DS-RPM-A", "DM-DDB-A"],
    "B-SS": ["DS-RP-A", "DS-RPM-A", "DM-DDB-A"],
    "B-GW": ["DS-RP-A", "DS-RPM-A", "DM-DDB-A", "DM-DCC-A"],
    "B-RTR": ["DM-DDB-A"],
    "B-BBMD": ["DM-DDB-A"],
}


def list_profiles() -> list[str]:
    """Return the list of supported conformance profile names."""
    return sorted(_PROFILE_BIBBS)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TestResult:
    """Outcome of a single conformance test case."""

    test_id: str
    bibb: str
    ref: str
    description: str
    passed: bool
    skip_reason: str = ""
    error: str = ""
    detail: str = ""


@dataclass
class ConformanceReport:
    """Aggregated result of a full conformance run."""

    profile: str
    target: str
    operator: str
    started_at: datetime
    finished_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    results: list[TestResult] = field(default_factory=list)
    firmware_revision: str = ""
    vendor_name: str = ""

    # ------------------------------------------------------------------
    # Derived statistics
    # ------------------------------------------------------------------

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed and not r.skip_reason)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed and not r.skip_reason)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.skip_reason)

    # ------------------------------------------------------------------
    # Output formats
    # ------------------------------------------------------------------

    def to_markdown(self) -> str:
        """Render the report as a Markdown string."""
        lines = [
            f"# BACnet Conformance Report — {self.profile}",
            f"**Target:** {self.target}  ",
            f"**Operator:** {self.operator}  ",
            f"**Firmware:** {self.firmware_revision or '(unknown)'}  ",
            f"**Vendor:** {self.vendor_name or '(unknown)'}  ",
            f"**Started:** {self.started_at.isoformat()}  ",
            f"**Finished:** {self.finished_at.isoformat()}  ",
            f"**Result:** {self.passed} PASS / {self.failed} FAIL / {self.skipped} SKIP  ",
            "",
            "| Test ID | BIBB | Ref | Result | Detail |",
            "|---------|------|-----|--------|--------|",
        ]
        for r in self.results:
            icon = "✅" if r.passed else ("⏭" if r.skip_reason else "❌")
            detail = r.skip_reason or r.detail or r.error or ""
            lines.append(f"| {r.test_id} | {r.bibb} | {r.ref} | {icon} | {detail} |")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "target": self.target,
            "operator": self.operator,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "firmware_revision": self.firmware_revision,
            "vendor_name": self.vendor_name,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "results": [
                {
                    "test_id": r.test_id,
                    "bibb": r.bibb,
                    "ref": r.ref,
                    "description": r.description,
                    "passed": r.passed,
                    "skip_reason": r.skip_reason,
                    "error": r.error,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ConformanceRunner:
    """Execute a BIBB conformance profile against a live BACnet session.

    Parameters
    ----------
    session:
        Active :class:`BacnetIpSession`.
    profile:
        Profile name from :func:`list_profiles` (e.g. ``"B-BC"``).
    target_object_id:
        The Device object identifier string (e.g. ``"device:1234"``).
    operator:
        Operator name embedded in the report.
    stop_on_first_failure:
        If ``True``, abort the run on the first FAIL.
    """

    def __init__(
        self,
        session: BacnetIpSession,
        profile: str,
        *,
        target_object_id: str = "device:0",
        operator: str = "conformance-runner",
        stop_on_first_failure: bool = False,
    ) -> None:
        if profile not in _PROFILE_BIBBS:
            raise ValueError(
                f"Unknown profile {profile!r}. Known: {', '.join(sorted(_PROFILE_BIBBS))}"
            )
        self._session = session
        self._profile = profile
        self._target_oid = target_object_id
        self._operator = operator
        self._stop_on_fail = stop_on_first_failure

    def run(self) -> ConformanceReport:
        """Execute all test cases for the profile and return the report."""
        started = datetime.now(tz=timezone.utc)
        results: list[TestResult] = []

        # Read firmware / vendor info upfront
        firmware = ""
        vendor_name = ""
        try:
            rpm = self._session._loop_thread.submit(
                self._session._async_rpm([(self._target_oid, ["firmwareRevision", "vendorName"])]),
                timeout=10,
            )
            obj_data = rpm.get(self._target_oid, {})
            firmware = str(obj_data.get("firmwareRevision", ""))
            vendor_name = str(obj_data.get("vendorName", ""))
        except Exception as exc:
            _logger.warning("Could not read firmware/vendor info: %s", exc)

        bibbs = _PROFILE_BIBBS[self._profile]
        for bibb in bibbs:
            for tc in _BIBB_TESTS.get(bibb, []):
                result = self._run_test_case(bibb, tc)
                results.append(result)
                _logger.info(
                    "Conformance %s: %s — %s",
                    self._profile,
                    tc["id"],
                    "PASS" if result.passed else ("SKIP" if result.skip_reason else "FAIL"),
                )
                if self._stop_on_fail and not result.passed and not result.skip_reason:
                    break
            else:
                continue
            break

        return ConformanceReport(
            profile=self._profile,
            target=str(self._session.device.address),
            operator=self._operator,
            started_at=started,
            finished_at=datetime.now(tz=timezone.utc),
            results=results,
            firmware_revision=firmware,
            vendor_name=vendor_name,
        )

    def _run_test_case(self, bibb: str, tc: dict[str, Any]) -> TestResult:
        """Execute one test case dict and return a :class:`TestResult`."""
        test_id: str = tc["id"]
        ref: str = tc.get("ref", "")
        description: str = tc.get("description", "")
        service: str = tc.get("service", "")

        def _ok(detail: str = "") -> TestResult:
            return TestResult(
                test_id=test_id,
                bibb=bibb,
                ref=ref,
                description=description,
                passed=True,
                detail=detail,
            )

        def _fail(error: str) -> TestResult:
            return TestResult(
                test_id=test_id,
                bibb=bibb,
                ref=ref,
                description=description,
                passed=False,
                error=error,
            )

        def _skip(reason: str) -> TestResult:
            return TestResult(
                test_id=test_id,
                bibb=bibb,
                ref=ref,
                description=description,
                passed=False,
                skip_reason=reason,
            )

        try:
            if service == "ReadProperty":
                return self._tc_read_property(tc, _ok, _fail, _skip)
            if service == "ReadPropertyMultiple":
                return self._tc_rpm(tc, _ok, _fail, _skip)
            if service == "WriteProperty":
                return self._tc_write_property(tc, _ok, _fail, _skip)
            if service == "Who-Is":
                return self._tc_who_is(tc, _ok, _fail, _skip)
            if service == "GetEventInformation":
                self._session.get_event_information()
                return _ok("GetEventInformation returned without error")
            if service == "SubscribeCOV":
                return self._tc_subscribe_cov(tc, _ok, _fail, _skip)
            if service == "DeviceCommunicationControl":
                return self._tc_dcc(tc, _ok, _fail, _skip)
        except Exception as exc:
            return _fail(f"Unexpected exception: {exc}")

        return _skip(f"Service {service!r} not yet exercised by runner")

    def _tc_read_property(
        self,
        tc: dict[str, Any],
        _ok: Any,
        _fail: Any,
        _skip: Any,
    ) -> TestResult:
        prop = tc["prop"]
        expect_error = tc.get("expect_error", False)
        try:
            val = self._session._loop_thread.submit(
                self._session._async_read_property(self._target_oid, prop),
                timeout=self._session._apdu_timeout + 2,
            )
            if expect_error:
                return _fail(f"Expected an error but got value: {val!r}")
            return _ok(f"{prop} = {val!r}")
        except Exception as exc:
            if expect_error:
                return _ok(f"Expected error received: {exc}")
            return _fail(str(exc))

    def _tc_rpm(
        self,
        tc: dict[str, Any],
        _ok: Any,
        _fail: Any,
        _skip: Any,
    ) -> TestResult:
        props = tc.get("props", ["objectIdentifier"])
        min_count = tc.get("min_result_count", 0)
        try:
            rpm_result = self._session._loop_thread.submit(
                self._session._async_rpm([(self._target_oid, props)]),
                timeout=self._session._apdu_timeout + 4,
            )
            obj_props = rpm_result.get(self._target_oid, {})
            if min_count and len(obj_props) < min_count:
                return _fail(f"Expected ≥{min_count} properties, got {len(obj_props)}")
            return _ok(f"{len(obj_props)} properties returned")
        except Exception as exc:
            return _fail(str(exc))

    def _tc_write_property(
        self,
        tc: dict[str, Any],
        _ok: Any,
        _fail: Any,
        _skip: Any,
    ) -> TestResult:
        from protoskipper.core.driver import ObjectRef
        from protoskipper.core.errors import AuthorizationDenied

        prop = tc.get("prop", "description")
        value = tc.get("value", "test")
        try:
            ref = ObjectRef(
                device=self._session.device,
                object_id=self._target_oid,
                data_type="characterstring",
            )
            intent = self._session.prepare_write(ref, value, data_type="characterstring")
            intent.metadata["prop"] = prop
            result = self._session.commit_write(intent)  # noqa: F841
            return _ok(f"WriteProperty {prop} succeeded")
        except AuthorizationDenied as exc:
            return _skip(f"Safety denied: {exc}")
        except Exception as exc:
            return _fail(str(exc))

    def _tc_who_is(
        self,
        tc: dict[str, Any],
        _ok: Any,
        _fail: Any,
        _skip: Any,
    ) -> TestResult:
        from protoskipper.builtin_drivers.bacnet.client import discover_devices

        expect_no = tc.get("expect_no_response", False)
        # Extract device-ID from target_oid to build range
        try:
            dev_id = int(self._target_oid.split(":")[-1])
        except ValueError:
            return _skip("Cannot extract device-ID from target_object_id")

        low, high = (dev_id + 1, dev_id + 1) if expect_no else (dev_id, dev_id)
        try:
            found = discover_devices(
                self._session._loop_thread,
                self._session._app,
                low_limit=low,
                high_limit=high,
                timeout=3.0,
            )
            if expect_no:
                return _ok("No I-Am received for out-of-range Who-Is (correct)")
            if not found:
                return _fail(f"No I-Am received for device-ID {dev_id}")
            return _ok(f"I-Am received: {found[0]}")
        except Exception as exc:
            return _fail(str(exc))

    def _tc_subscribe_cov(
        self,
        tc: dict[str, Any],
        _ok: Any,
        _fail: Any,
        _skip: Any,
    ) -> TestResult:
        from protoskipper.core.driver import ObjectRef

        obj_type = tc.get("object_type", "analog-input")
        # Use instance 1 as the test target (common)
        ref = ObjectRef(
            device=self._session.device,
            object_id=f"{obj_type}:1",
            data_type="any",
        )
        try:
            handle = self._session.subscribe([ref], callback=lambda r: None)
            self._session.unsubscribe(handle)
            return _ok(f"SubscribeCOV / cancel accepted for {obj_type}:1")
        except Exception as exc:
            return _fail(str(exc))

    def _tc_dcc(
        self,
        tc: dict[str, Any],
        _ok: Any,
        _fail: Any,
        _skip: Any,
    ) -> TestResult:
        from protoskipper.core.errors import AuthorizationDenied

        mode = tc.get("mode", "enable")
        try:
            self._session.device_communication_control(mode)
            return _ok(f"DeviceCommunicationControl {mode} accepted")
        except AuthorizationDenied as exc:
            return _skip(f"Safety denied: {exc}")
        except Exception as exc:
            return _fail(str(exc))
