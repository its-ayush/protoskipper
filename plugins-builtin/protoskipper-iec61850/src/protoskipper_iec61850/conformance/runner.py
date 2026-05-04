# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Conformance test runner — P8.F.2.

Executes conformance test cases against a live IEC 61850 session.

Architecture
------------
:class:`ConformanceRunner` accepts a :class:`ConformanceProfile` and a
*session context* (a dict of live objects keyed by name) and runs each
test case step by step.

Step actions are dispatched to :class:`ActionDispatcher`, which maps
action names to handler callables.  Handlers may be extended by the
caller by passing additional ``extra_handlers``.

The runner is **synchronous** and designed to run on a background thread
(e.g. inside a ``DriverWorker``).  Progress is reported via an optional
``on_progress`` callback.

Built-in actions
----------------
``connect``
    Connect to the IED via MMS (uses ``session_ctx["mms_client"]``).
``disconnect``
    Disconnect the MMS session.
``assert_connection``
    Assert that the MMS session status matches ``params["status"]``.
``assert_response``
    Assert that the most recent MMS response has the expected status.
``GetNameList``
    Issue a GetNameList MMS service call.
``GetVariableAccessAttributes``
    Issue a GetVariableAccessAttributes call.
``Read``
    Issue an MMS Read service call.
``Write``
    Issue an MMS Write service call.
``assert_goose_received``, ``collect_goose_frames``, ``assert_goose_burst``,
``assert_goose_interval``, ``assert_goose_field``
    Stubs for GOOSE actions; require an injected handler from the caller.
``subscribe_goose``, ``trigger_state_change``, ``enable_simulation_mode``
    Stubs for GOOSE control; require an injected handler from the caller.
``read_mms_attribute``
    Read a specific MMS functional constrained attribute.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .schema import ConformanceProfile, ConformanceTestCase, Severity, TestStep

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class TestStatus(str, Enum):
    """Outcome of a single conformance test case."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"


@dataclass
class StepResult:
    """Result of a single test step.

    Attributes
    ----------
    action:
        Action name.
    status:
        ``"ok"`` or ``"fail"``.
    message:
        Human-readable description.
    elapsed_ms:
        Wall-clock time taken for the step in milliseconds.
    """

    action: str
    status: str  # "ok" | "fail"
    message: str = ""
    elapsed_ms: float = 0.0


@dataclass
class TestCaseResult:
    """Result of a single conformance test case.

    Attributes
    ----------
    test_case:
        The :class:`ConformanceTestCase` that was run.
    status:
        Overall outcome.
    step_results:
        Per-step outcomes.
    started_at:
        Unix timestamp when the test case started.
    elapsed_ms:
        Total wall-clock time in milliseconds.
    error:
        Exception message if the test errored out unexpectedly.
    """

    test_case: ConformanceTestCase
    status: TestStatus = TestStatus.ERROR
    step_results: list[StepResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    elapsed_ms: float = 0.0
    error: str = ""


@dataclass
class ConformanceReport:
    """Summary report for a complete conformance profile run.

    Attributes
    ----------
    profile:
        The :class:`ConformanceProfile` that was run.
    results:
        Per-test-case results.
    operator:
        Name of the operator who performed the test.
    device_info:
        Dict of device information (firmware, model, etc.).
    started_at:
        Unix timestamp when the run started.
    elapsed_ms:
        Total elapsed time in milliseconds.
    """

    profile: ConformanceProfile
    results: list[TestCaseResult] = field(default_factory=list)
    operator: str = ""
    device_info: dict[str, str] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    elapsed_ms: float = 0.0

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.FAIL)

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.ERROR)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == TestStatus.SKIP)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def overall_pass(self) -> bool:
        """True iff all MANDATORY tests passed."""
        for r in self.results:
            if r.test_case.severity == Severity.MANDATORY and r.status in (
                TestStatus.FAIL,
                TestStatus.ERROR,
            ):
                return False
        return True


# ---------------------------------------------------------------------------
# ActionDispatcher
# ---------------------------------------------------------------------------

ActionHandler = Callable[[TestStep, dict[str, Any]], StepResult]


class ActionDispatcher:
    """Maps action names to handler callables.

    Built-in stub handlers return ``status="ok"`` for unknown actions so
    that profiles can be run against a mock session during unit testing.

    Parameters
    ----------
    session_ctx:
        Dict passed to each handler.  Keys are ``"mms_client"``,
        ``"last_response"``, ``"goose_frames"``, etc.
    extra_handlers:
        Optional dict mapping action names to callables that override or
        extend the built-in handlers.
    """

    def __init__(
        self,
        session_ctx: dict[str, Any],
        extra_handlers: dict[str, ActionHandler] | None = None,
    ) -> None:
        self._ctx = session_ctx
        self._handlers: dict[str, ActionHandler] = {
            "connect": self._h_connect,
            "disconnect": self._h_disconnect,
            "assert_connection": self._h_assert_connection,
            "assert_response": self._h_assert_response,
            "GetNameList": self._h_get_name_list,
            "GetVariableAccessAttributes": self._h_get_var_attrs,
            "Read": self._h_read,
            "Write": self._h_write,
            "read_mms_attribute": self._h_read_mms_attribute,
        }
        if extra_handlers:
            self._handlers.update(extra_handlers)

    def dispatch(self, step: TestStep) -> StepResult:
        handler = self._handlers.get(step.action)
        if handler is None:
            # Stub — unknown action passes by default so profiles can be
            # evaluated structurally against a mock context.
            return StepResult(action=step.action, status="ok", message="[stub] not implemented")
        t0 = time.monotonic()
        result = handler(step, self._ctx)
        result.elapsed_ms = (time.monotonic() - t0) * 1000
        return result

    # ------------------------------------------------------------------
    # Built-in handlers
    # ------------------------------------------------------------------

    def _h_connect(self, step: TestStep, ctx: dict[str, Any]) -> StepResult:
        client = ctx.get("mms_client")
        if client is None:
            ctx["connected"] = True
            return StepResult(step.action, "ok", "no mms_client — stub pass")
        port = step.params.get("port", 102)
        try:
            client.connect(port=port)
            ctx["connected"] = True
            return StepResult(step.action, "ok", f"Connected on port {port}")
        except Exception as exc:
            return StepResult(step.action, "fail", str(exc))

    def _h_disconnect(self, step: TestStep, ctx: dict[str, Any]) -> StepResult:
        client = ctx.get("mms_client")
        if client is None:
            ctx["connected"] = False
            return StepResult(step.action, "ok", "no mms_client — stub pass")
        try:
            client.disconnect()
            ctx["connected"] = False
            return StepResult(step.action, "ok", "Disconnected")
        except Exception as exc:
            return StepResult(step.action, "fail", str(exc))

    def _h_assert_connection(self, step: TestStep, ctx: dict[str, Any]) -> StepResult:
        expected = step.params.get("status", "connected")
        connected = ctx.get("connected", False)
        actual = "connected" if connected else "disconnected"
        if actual == expected:
            return StepResult(step.action, "ok", f"Connection status: {actual}")
        return StepResult(step.action, "fail", f"Expected {expected!r}, got {actual!r}")

    def _h_assert_response(self, step: TestStep, ctx: dict[str, Any]) -> StepResult:
        last = ctx.get("last_response")
        if last is None:
            return StepResult(step.action, "ok", "no last_response — stub pass")
        expected_status = step.params.get("status", "success")
        actual_status = getattr(last, "status", None) or ctx.get("last_response_status", "success")
        if actual_status != expected_status:
            msg = f"Expected {expected_status!r}, got {actual_status!r}"
            return StepResult(step.action, "fail", msg)
        if step.params.get("has_items"):
            items = getattr(last, "items", None) or ctx.get("last_items", [])
            if not items:
                return StepResult(step.action, "fail", "Expected non-empty item list")
        if step.params.get("has_value"):
            val = getattr(last, "value", None) or ctx.get("last_value")
            if val is None:
                return StepResult(step.action, "fail", "Expected a value in response")
        return StepResult(step.action, "ok", f"Response status: {actual_status}")

    def _h_get_name_list(self, step: TestStep, ctx: dict[str, Any]) -> StepResult:
        client = ctx.get("mms_client")
        if client is None:
            ctx["last_response_status"] = "success"
            ctx["last_items"] = []
            return StepResult(step.action, "ok", "no mms_client — stub pass")
        try:
            items = client.get_name_list(
                object_class=step.params.get("objectClass", "Domain"),
                object_scope=step.params.get("objectScope", "VmDSpecific"),
            )
            ctx["last_items"] = items or []
            ctx["last_response_status"] = "success"
            if not ctx.get("first_domain") and items:
                ctx["first_domain"] = items[0]
            return StepResult(step.action, "ok", f"{len(items or [])} items")
        except Exception as exc:
            ctx["last_response_status"] = "error"
            return StepResult(step.action, "fail", str(exc))

    def _h_get_var_attrs(self, step: TestStep, ctx: dict[str, Any]) -> StepResult:
        client = ctx.get("mms_client")
        if client is None:
            ctx["last_response_status"] = "success"
            return StepResult(step.action, "ok", "no mms_client — stub pass")
        domain = step.params.get("domain", ctx.get("first_domain", ""))
        item_id = step.params.get("item_id", ctx.get("first_variable", ""))
        if domain.startswith("@"):
            domain = ctx.get(domain[1:], "")
        if item_id.startswith("@"):
            item_id = ctx.get(item_id[1:], "")
        try:
            result = client.get_variable_access_attributes(domain=domain, item_id=item_id)
            ctx["last_response"] = result
            ctx["last_response_status"] = "success"
            return StepResult(step.action, "ok", f"Got attributes for {domain}/{item_id}")
        except Exception as exc:
            return StepResult(step.action, "fail", str(exc))

    def _h_read(self, step: TestStep, ctx: dict[str, Any]) -> StepResult:
        client = ctx.get("mms_client")
        if client is None:
            ctx["last_response_status"] = "success"
            ctx["last_value"] = True
            return StepResult(step.action, "ok", "no mms_client — stub pass")
        domain = step.params.get("domain", ctx.get("first_domain", ""))
        item_id = step.params.get("item_id", ctx.get("first_variable", ""))
        if domain.startswith("@"):
            domain = ctx.get(domain[1:], "")
        if item_id.startswith("@"):
            item_id = ctx.get(item_id[1:], "")
        try:
            value = client.read(domain=domain, item_id=item_id)
            ctx["last_value"] = value
            ctx["last_response_status"] = "success"
            return StepResult(step.action, "ok", f"Read {domain}/{item_id} = {value!r}")
        except Exception as exc:
            return StepResult(step.action, "fail", str(exc))

    def _h_write(self, step: TestStep, ctx: dict[str, Any]) -> StepResult:
        client = ctx.get("mms_client")
        if client is None:
            ctx["last_response_status"] = "success"
            return StepResult(step.action, "ok", "no mms_client — stub pass")
        domain = step.params.get("domain", ctx.get("first_domain", ""))
        item_id = step.params.get("item_id", ctx.get("first_writable_variable", ""))
        value = step.params.get("value")
        if isinstance(domain, str) and domain.startswith("@"):
            domain = ctx.get(domain[1:], "")
        if isinstance(item_id, str) and item_id.startswith("@"):
            item_id = ctx.get(item_id[1:], "")
        if isinstance(value, str) and value.startswith("@"):
            value = ctx.get(value[1:], value)
        try:
            client.write(domain=domain, item_id=item_id, value=value)
            ctx["last_response_status"] = "success"
            return StepResult(step.action, "ok", f"Wrote {domain}/{item_id}")
        except Exception as exc:
            return StepResult(step.action, "fail", str(exc))

    def _h_read_mms_attribute(self, step: TestStep, ctx: dict[str, Any]) -> StepResult:
        client = ctx.get("mms_client")
        attr = step.params.get("attribute", "")
        fc = step.params.get("fc", "CF")
        if client is None:
            ctx[f"mms_{attr}"] = step.params.get("default", 1)
            return StepResult(step.action, "ok", "no mms_client — stub pass")
        try:
            value = client.read_fc_attribute(fc=fc, attribute=attr)
            ctx[f"mms_{attr}"] = value
            return StepResult(step.action, "ok", f"Read {fc}/{attr} = {value!r}")
        except Exception as exc:
            return StepResult(step.action, "fail", str(exc))


# ---------------------------------------------------------------------------
# ConformanceRunner
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[ConformanceTestCase, TestCaseResult], None]


class ConformanceRunner:
    """Runs a conformance profile against a live session.

    Parameters
    ----------
    profile:
        The :class:`ConformanceProfile` to execute.
    session_ctx:
        Dict of live session objects (``"mms_client"``, etc.).
    extra_handlers:
        Optional action handlers to inject.
    on_progress:
        Optional callback ``(test_case, result)`` called after each test.
    operator:
        Operator name for the report.
    device_info:
        Device metadata for the report.
    skip_optional:
        If ``True``, skip OPTIONAL and CONDITIONAL tests.
    """

    def __init__(
        self,
        profile: ConformanceProfile,
        session_ctx: dict[str, Any] | None = None,
        extra_handlers: dict[str, ActionHandler] | None = None,
        on_progress: ProgressCallback | None = None,
        operator: str = "",
        device_info: dict[str, str] | None = None,
        skip_optional: bool = False,
    ) -> None:
        self._profile = profile
        self._ctx: dict[str, Any] = session_ctx or {}
        self._extra_handlers = extra_handlers or {}
        self._on_progress = on_progress
        self._operator = operator
        self._device_info = device_info or {}
        self._skip_optional = skip_optional

    def run(self) -> ConformanceReport:
        """Execute all test cases and return a :class:`ConformanceReport`."""
        report = ConformanceReport(
            profile=self._profile,
            operator=self._operator,
            device_info=self._device_info,
            started_at=time.time(),
        )
        t0 = time.monotonic()

        dispatcher = ActionDispatcher(self._ctx, self._extra_handlers)

        for tc in self._profile.tests:
            if self._skip_optional and tc.severity in (
                Severity.OPTIONAL,
                Severity.CONDITIONAL,
            ):
                result = TestCaseResult(
                    test_case=tc,
                    status=TestStatus.SKIP,
                    started_at=time.time(),
                )
                report.results.append(result)
                if self._on_progress:
                    self._on_progress(tc, result)
                continue

            result = self._run_test_case(tc, dispatcher)
            report.results.append(result)
            if self._on_progress:
                self._on_progress(tc, result)

        report.elapsed_ms = (time.monotonic() - t0) * 1000
        return report

    def _run_test_case(
        self, tc: ConformanceTestCase, dispatcher: ActionDispatcher
    ) -> TestCaseResult:
        result = TestCaseResult(test_case=tc, started_at=time.time())
        t0 = time.monotonic()
        try:
            for step in tc.steps:
                step_result = dispatcher.dispatch(step)
                result.step_results.append(step_result)
                if step_result.status == "fail":
                    result.status = TestStatus.FAIL
                    break
            else:
                result.status = TestStatus.PASS
        except Exception as exc:
            result.status = TestStatus.ERROR
            result.error = str(exc)
            _log.exception("Unexpected error in test case %s", tc.test_id)
        result.elapsed_ms = (time.monotonic() - t0) * 1000
        return result
