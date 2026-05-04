# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Conformance report writer — P8.F.3.

Writes a :class:`ConformanceReport` to HTML (and optionally PDF via
``weasyprint`` if available).

The HTML report includes:
* Header: profile ID, edition, operator, device info, date/time.
* Summary: total / passed / failed / skipped / errored counts.
* Per-test-case table: ID, name, severity, status, elapsed, steps.
* Audit log signature (if provided).

Usage::

    from protoskipper_iec61850.conformance.reporter import write_html_report
    write_html_report(report, Path("report.html"), audit_sig="abc123")
"""

from __future__ import annotations

import datetime
import html
from pathlib import Path

from .runner import ConformanceReport, TestStatus

# ---------------------------------------------------------------------------
# HTML writer
# ---------------------------------------------------------------------------

_STATUS_COLOUR = {
    TestStatus.PASS: "#00aa44",
    TestStatus.FAIL: "#cc0000",
    TestStatus.ERROR: "#cc6600",
    TestStatus.SKIP: "#888888",
}

_STATUS_LABEL = {
    TestStatus.PASS: "PASS",
    TestStatus.FAIL: "FAIL",
    TestStatus.ERROR: "ERROR",
    TestStatus.SKIP: "SKIP",
}

_CSS = """
body { font-family: sans-serif; margin: 2em; color: #222; }
h1 { font-size: 1.4em; }
h2 { font-size: 1.1em; border-bottom: 1px solid #ccc; padding-bottom: 0.2em; }
table { border-collapse: collapse; width: 100%; margin-bottom: 1.5em; }
th, td { text-align: left; padding: 6px 10px; border: 1px solid #ddd; }
th { background: #f5f5f5; }
.pass   { color: #00aa44; font-weight: bold; }
.fail   { color: #cc0000; font-weight: bold; }
.error  { color: #cc6600; font-weight: bold; }
.skip   { color: #888888; }
.badge  { display: inline-block; padding: 2px 8px; border-radius: 4px;
           color: #fff; font-size: 0.85em; }
.summary-box { display: inline-block; margin-right: 1em; padding: 0.5em 1em;
               border-radius: 6px; color: #fff; font-weight: bold; }
.step-ok   { color: #00aa44; }
.step-fail { color: #cc0000; font-weight: bold; }
pre { background: #f8f8f8; border: 1px solid #ddd; padding: 0.5em; font-size: 0.85em; }
"""


def _esc(s: str) -> str:
    return html.escape(str(s))


def _status_badge(status: TestStatus) -> str:
    colour = _STATUS_COLOUR.get(status, "#888")
    label = _STATUS_LABEL.get(status, str(status))
    return f'<span class="badge" style="background:{colour}">{label}</span>'


def write_html_report(
    report: ConformanceReport,
    path: Path,
    audit_sig: str = "",
    title: str = "IEC 61850 Conformance Test Report",
) -> None:
    """Write *report* to an HTML file at *path*.

    Parameters
    ----------
    report:
        The :class:`ConformanceReport` to render.
    path:
        Output file path.
    audit_sig:
        Optional audit log HMAC signature to include in the report.
    title:
        Page title.
    """
    ts = datetime.datetime.fromtimestamp(report.started_at, tz=datetime.timezone.utc)
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    overall_colour = "#00aa44" if report.overall_pass else "#cc0000"
    overall_label = "OVERALL: PASS" if report.overall_pass else "OVERALL: FAIL"

    lines: list[str] = []
    a = lines.append

    a("<!DOCTYPE html>")
    a("<html lang='en'>")
    a(f"<head><meta charset='utf-8'><title>{_esc(title)}</title>")
    a(f"<style>{_CSS}</style></head>")
    a("<body>")
    a(f"<h1>{_esc(title)}</h1>")

    # Overall badge
    a(f'<p><span class="badge" style="background:{overall_colour};font-size:1.1em">')
    a(f"  {_esc(overall_label)}</span></p>")

    # Metadata table
    a("<h2>Test Information</h2>")
    a("<table>")
    a(
        f"<tr><th>Profile</th><td>{_esc(report.profile.profile_id)}</td>"
        f"<th>Edition</th><td>{_esc(report.profile.edition)}</td></tr>"
    )
    a(
        f"<tr><th>Category</th><td>{_esc(report.profile.category)}</td>"
        f"<th>Date / Time</th><td>{_esc(ts_str)}</td></tr>"
    )
    a(
        f"<tr><th>Operator</th><td>{_esc(report.operator or '—')}</td>"
        f"<th>Duration</th><td>{report.elapsed_ms:.0f} ms</td></tr>"
    )
    if report.device_info:
        for k, v in report.device_info.items():
            a(f"<tr><th>{_esc(k)}</th><td colspan='3'>{_esc(v)}</td></tr>")
    if audit_sig:
        a(f"<tr><th>Audit Signature</th><td colspan='3'><code>{_esc(audit_sig)}</code></td></tr>")
    a("</table>")

    # Summary
    a("<h2>Summary</h2>")
    for label, count, colour in [
        ("Passed", report.passed, "#00aa44"),
        ("Failed", report.failed, "#cc0000"),
        ("Errored", report.errored, "#cc6600"),
        ("Skipped", report.skipped, "#888888"),
        ("Total", report.total, "#334466"),
    ]:
        a(f'<span class="summary-box" style="background:{colour}">')
        a(f"  {count} {label}</span>")
    a("<br><br>")

    # Per-test-case table
    a("<h2>Test Results</h2>")
    a("<table>")
    a(
        "<tr><th>ID</th><th>Name</th><th>Severity</th>"
        "<th>Status</th><th>Duration (ms)</th><th>Details</th></tr>"
    )
    for r in report.results:
        tc = r.test_case
        badge = _status_badge(r.status)
        # Build step detail
        step_lines: list[str] = []
        for sr in r.step_results:
            step_cls = "step-ok" if sr.status == "ok" else "step-fail"
            step_lines.append(
                f'<span class="{step_cls}">[{_esc(sr.status)}]</span>'
                f" <b>{_esc(sr.action)}</b>" + (f" — {_esc(sr.message)}" if sr.message else "")
            )
        if r.error:
            step_lines.append(f'<span class="step-fail">ERROR: {_esc(r.error)}</span>')
        detail_html = "<br>".join(step_lines) if step_lines else "—"
        a(
            f"<tr><td>{_esc(tc.test_id)}</td><td>{_esc(tc.name)}</td>"
            f"<td>{_esc(tc.severity.value)}</td><td>{badge}</td>"
            f"<td>{r.elapsed_ms:.1f}</td><td>{detail_html}</td></tr>"
        )
    a("</table>")

    a(f"<p style='color:#888;font-size:0.85em'>Generated by ProtoSkipper {ts_str}</p>")
    a("</body></html>")

    path.write_text("\n".join(lines), encoding="utf-8")


def write_pdf_report(
    report: ConformanceReport,
    path: Path,
    audit_sig: str = "",
    title: str = "IEC 61850 Conformance Test Report",
) -> None:
    """Write *report* to a PDF file at *path* using ``weasyprint``.

    Falls back gracefully: if ``weasyprint`` is not installed, writes an
    HTML file alongside the requested PDF path with a ``".html"`` suffix
    and raises :class:`ImportError` so the caller can notify the user.

    Parameters
    ----------
    report:
        The :class:`ConformanceReport` to render.
    path:
        Output file path (should end in ``.pdf``).
    audit_sig:
        Optional audit log HMAC signature.
    title:
        Page title.

    Raises
    ------
    ImportError:
        If ``weasyprint`` is not installed.
    """
    html_path = path.with_suffix(".html")
    write_html_report(report, html_path, audit_sig=audit_sig, title=title)

    try:
        import weasyprint  # type: ignore[import-untyped]
    except ImportError as exc:
        msg = (
            "weasyprint is not installed; HTML report written to "
            f"{html_path}.  Install weasyprint to generate PDF."
        )
        raise ImportError(msg) from exc

    weasyprint.HTML(filename=str(html_path)).write_pdf(str(path))
