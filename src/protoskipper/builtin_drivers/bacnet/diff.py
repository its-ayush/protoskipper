# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Diff between expected points list and live RPM enumeration (P7.H.6).

Loads an EDE / AT / CSV ("expected") file and compares it against the live
device object model ("actual") using ReadPropertyMultiple.

Usage::

    from protoskipper.builtin_drivers.bacnet.diff import PointsDiff

    diff = PointsDiff(session=my_session, expected_path="floor3-ahu1.ede.csv")
    result = diff.run()
    print(result.to_markdown())
    result.to_csv("/tmp/floor3-ahu1-diff.csv")
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from protoskipper.builtin_drivers.bacnet.client import BacnetIpSession

_logger = logging.getLogger(__name__)

__all__ = ["DiffResult", "DiffRow", "PointsDiff"]

# Properties fetched from the live device for comparison
_COMPARE_PROPS = ["objectName", "description", "units", "stateText", "covIncrement"]


@dataclass
class DiffRow:
    """A single row in the diff output."""

    objid: str
    status: str  # "expected-only" | "actual-only" | "match" | "mismatch"
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)  # property names that differ

    @property
    def has_mismatch(self) -> bool:
        return bool(self.mismatches)


@dataclass
class DiffResult:
    """Aggregated diff between expected and actual object models."""

    rows: list[DiffRow] = field(default_factory=list)
    expected_path: str = ""
    device_address: str = ""

    # ------------------------------------------------------------------
    # Derived views
    # ------------------------------------------------------------------

    def expected_only(self) -> list[DiffRow]:
        return [r for r in self.rows if r.status == "expected-only"]

    def actual_only(self) -> list[DiffRow]:
        return [r for r in self.rows if r.status == "actual-only"]

    def mismatches(self) -> list[DiffRow]:
        return [r for r in self.rows if r.status == "mismatch"]

    def matches(self) -> list[DiffRow]:
        return [r for r in self.rows if r.status == "match"]

    # ------------------------------------------------------------------
    # Output formats
    # ------------------------------------------------------------------

    def to_markdown(self) -> str:
        lines = [
            "# BACnet Points-List Diff",
            f"**Expected:** {self.expected_path or '(embedded)'}  ",
            f"**Device:** {self.device_address or '(unknown)'}  ",
            "",
            f"- In expected only: **{len(self.expected_only())}**",
            f"- In actual only: **{len(self.actual_only())}**",
            f"- Property mismatches: **{len(self.mismatches())}**",
            f"- Exact matches: **{len(self.matches())}**",
            "",
            "## In expected only (missing from device)",
            "",
        ]
        for r in self.expected_only():
            lines.append(f"- `{r.objid}` — {r.expected.get('objectName', '')}")

        lines.extend(["", "## In actual only (not in EDE)", ""])
        for r in self.actual_only():
            lines.append(f"- `{r.objid}`")

        lines.extend(["", "## Property mismatches", ""])
        for r in self.mismatches():
            lines.append(f"### `{r.objid}`")
            for prop in r.mismatches:
                lines.append(
                    f"- **{prop}**: expected `{r.expected.get(prop, '—')!r}` "
                    f"/ actual `{r.actual.get(prop, '—')!r}`"
                )

        return "\n".join(lines)

    def to_csv(self, path: str | Path | None = None) -> str:
        """Render as CSV (write to *path* if given, otherwise return string)."""
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=["objid", "status", "mismatches", *_COMPARE_PROPS],
            extrasaction="ignore",
        )
        writer.writeheader()
        for r in self.rows:
            row: dict[str, Any] = {
                "objid": r.objid,
                "status": r.status,
                "mismatches": "|".join(r.mismatches),
            }
            for prop in _COMPARE_PROPS:
                row[prop] = r.expected.get(prop, "")
            writer.writerow(row)

        csv_str = output.getvalue()
        if path is not None:
            Path(path).write_text(csv_str, encoding="utf-8")
        return csv_str


class PointsDiff:
    """Compute the diff between an expected points list and a live device.

    Parameters
    ----------
    session:
        Active :class:`BacnetIpSession` open to the target device.
    expected_path:
        Path to an EDE / AT / native CSV or JSON points-list file.
    expected_points:
        Alternatively, pass points as a list of dicts (skip file loading).
    rpm_batch_size:
        Max objects per ReadPropertyMultiple call.
    """

    def __init__(
        self,
        session: BacnetIpSession,
        *,
        expected_path: str | Path | None = None,
        expected_points: list[dict[str, Any]] | None = None,
        rpm_batch_size: int = 16,
    ) -> None:
        self._session = session
        self._expected_path = str(expected_path) if expected_path else ""
        self._expected_points = expected_points
        self._rpm_batch = rpm_batch_size

        if expected_path is None and expected_points is None:
            raise ValueError("Provide either expected_path or expected_points")

    def run(self) -> DiffResult:
        """Enumerate the live device and compute the diff. Blocking."""
        # Load expected points
        expected: dict[str, dict[str, Any]] = self._load_expected()

        # Enumerate live objects
        actual: dict[str, dict[str, Any]] = self._enumerate_actual(list(expected))

        return _compute_diff(
            expected, actual, self._expected_path, str(self._session.device.address)
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_expected(self) -> dict[str, dict[str, Any]]:
        """Load the expected points list from file or the inline dict."""
        if self._expected_points is not None:
            return {p["objid"]: p for p in self._expected_points if "objid" in p}

        from protoskipper.builtin_drivers.bacnet.points import PointsList

        pl = PointsList.load(self._expected_path)
        result: dict[str, dict[str, Any]] = {}
        for pt in pl.points:
            oid = f"{pt.object_type}:{pt.instance}"
            result[oid] = {
                "objectName": pt.object_name,
                "description": getattr(pt, "description", ""),
                "units": getattr(pt, "units", ""),
                "stateText": getattr(pt, "state_text", None),
                "covIncrement": getattr(pt, "cov_increment", None),
            }
        return result

    def _enumerate_actual(self, hint_objids: list[str]) -> dict[str, dict[str, Any]]:
        """RPM-read properties for all objects enumerated from the device."""

        # First: enumerate all objects on the device
        live_objects: dict[str, dict[str, Any]] = {}

        all_refs = list(self._session.enumerate_objects())
        objids = [r.object_id for r in all_refs]

        # Batch RPM
        for batch_start in range(0, len(objids), self._rpm_batch):
            batch = objids[batch_start : batch_start + self._rpm_batch]
            rpm_requests = [(oid, _COMPARE_PROPS) for oid in batch]
            try:
                rpm_result = self._session._loop_thread.submit(
                    self._session._async_rpm(rpm_requests),
                    timeout=self._session._apdu_timeout + 4,
                )
                live_objects.update(rpm_result)
            except Exception as exc:
                _logger.warning("RPM batch failed: %s", exc)
                for oid in batch:
                    live_objects.setdefault(oid, {})

        return live_objects


# ---------------------------------------------------------------------------
# Pure diff computation
# ---------------------------------------------------------------------------


def _compute_diff(
    expected: dict[str, dict[str, Any]],
    actual: dict[str, dict[str, Any]],
    expected_path: str,
    device_address: str,
) -> DiffResult:
    result = DiffResult(expected_path=expected_path, device_address=device_address)

    all_keys = set(expected) | set(actual)
    for objid in sorted(all_keys):
        in_exp = objid in expected
        in_act = objid in actual

        if in_exp and not in_act:
            result.rows.append(
                DiffRow(objid=objid, status="expected-only", expected=expected[objid])
            )
            continue

        if in_act and not in_exp:
            result.rows.append(DiffRow(objid=objid, status="actual-only", actual=actual[objid]))
            continue

        # Both present — check property values
        exp_props = expected[objid]
        act_props = actual[objid]
        mismatches: list[str] = []
        for prop in _COMPARE_PROPS:
            ev = exp_props.get(prop)
            av = act_props.get(prop)
            if ev is None and av is None:
                continue
            if _normalise(ev) != _normalise(av):
                mismatches.append(prop)

        if mismatches:
            result.rows.append(
                DiffRow(
                    objid=objid,
                    status="mismatch",
                    expected=exp_props,
                    actual=act_props,
                    mismatches=mismatches,
                )
            )
        else:
            result.rows.append(
                DiffRow(objid=objid, status="match", expected=exp_props, actual=act_props)
            )

    return result


def _normalise(v: Any) -> str:
    """Normalise a property value to a string for comparison."""
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v).strip().lower()
