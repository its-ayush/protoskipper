# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 Fuzzer GUI panel + report (P4.D.5).

Presents a dialog for launching the four IEC 104 fuzzing modes:

* Codec round-trip    — ``fuzz_codec_roundtrip``
* ASDU codec          — ``fuzz_asdu_codec``
* APDU mutation       — ``fuzz_apdu_mutation``
* State-machine / network — ``fuzz_against_target``
* TLS handshake       — ``fuzz_tls_handshake``

**Safety gate:** The dialog refuses to open (or run) outside LAB profile.
If profile is COMMISSIONING or PRODUCTION the user sees a clear error and
all controls are disabled.

The long-running fuzzer is executed on a ``QThread`` worker.  Results are
collected into a plain-text / tabular report that can be saved as CSV or
displayed inline.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import SessionProfile

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Run parameters + result dataclass
# ---------------------------------------------------------------------------


@dataclass
class FuzzParams:
    """Parameters collected from the dialog."""

    mode: str  # "codec" | "asdu" | "mutation" | "state_machine" | "tls"
    iterations: int
    seed: int
    # network modes only
    host: str
    port: int
    connect_timeout: float


@dataclass
class FuzzResult:
    """Unified result from any fuzzing mode."""

    mode: str
    iterations: int
    seed: int
    ok: bool
    detail: dict[str, Any]  # mode-specific fields
    duration_seconds: float
    error: str  # empty on success; exception message on fatal error


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

_MODES = [
    ("Codec round-trip (APCI/ASDU parser)", "codec"),
    ("ASDU codec (random type payloads)", "asdu"),
    ("APDU mutation (bit-flip / truncate)", "mutation"),
    ("State-machine / network target", "state_machine"),
    ("TLS handshake mutation", "tls"),
]


class _FuzzWorker(QObject):
    progress = Signal(int)  # 0-100
    finished = Signal(object)  # FuzzResult

    def __init__(self, params: FuzzParams) -> None:
        super().__init__()
        self._params = params
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        p = self._params
        start = time.monotonic()
        error = ""
        ok = False
        detail: dict[str, Any] = {}
        try:
            from protoskipper.builtin_drivers.iec104.fuzzer import (
                fuzz_against_target,
                fuzz_apdu_mutation,
                fuzz_asdu_codec,
                fuzz_codec_roundtrip,
                fuzz_tls_handshake,
            )

            if p.mode == "codec":
                self.progress.emit(10)
                r = fuzz_codec_roundtrip(p.iterations, seed=p.seed)
                ok = r.ok
                detail = {
                    "valid_decodes": r.valid_decodes,
                    "expected_errors": r.expected_errors,
                    "unexpected_errors": r.unexpected_errors,
                }
            elif p.mode == "asdu":
                self.progress.emit(10)
                r = fuzz_asdu_codec(p.iterations, seed=p.seed)
                ok = r.ok
                detail = {
                    "valid_decodes": r.valid_decodes,
                    "expected_errors": r.expected_errors,
                    "unexpected_errors": r.unexpected_errors,
                }
            elif p.mode == "mutation":
                self.progress.emit(10)
                r = fuzz_apdu_mutation(p.iterations, seed=p.seed)
                ok = r.ok
                detail = {
                    "valid_decodes": r.valid_decodes,
                    "expected_errors": r.expected_errors,
                    "unexpected_errors": r.unexpected_errors,
                }
            elif p.mode == "state_machine":
                self.progress.emit(5)
                r = fuzz_against_target(  # type: ignore[assignment]
                    p.host,
                    p.port,
                    iterations=p.iterations,
                    seed=p.seed,
                )
                ok = r.ok  # type: ignore[attr-defined]
                detail = {
                    "sent": r.sent,  # type: ignore[attr-defined]
                    "peer_closed": r.peer_closed,  # type: ignore[attr-defined]
                    "duration_seconds": r.duration_seconds,  # type: ignore[attr-defined]
                }
            elif p.mode == "tls":
                self.progress.emit(5)
                r = fuzz_tls_handshake(  # type: ignore[assignment]
                    p.host,
                    p.port,
                    iterations=p.iterations,
                    seed=p.seed,
                    connect_timeout=p.connect_timeout,
                )
                ok = r.ok  # type: ignore[attr-defined]
                detail = {
                    "clean_rejects": r.clean_rejects,  # type: ignore[attr-defined]
                    "unexpected_errors": r.unexpected_errors,  # type: ignore[attr-defined]
                }
            else:
                error = f"Unknown mode: {p.mode!r}"
        except Exception as exc:
            error = str(exc)
            _logger.exception("Fuzzer raised: %s", exc)

        self.progress.emit(100)
        self.finished.emit(
            FuzzResult(
                mode=p.mode,
                iterations=p.iterations,
                seed=p.seed,
                ok=ok and not error,
                detail=detail,
                duration_seconds=time.monotonic() - start,
                error=error,
            )
        )


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class Iec104FuzzerDialog(QDialog):
    """IEC 104 Fuzzer control panel.

    Disabled outside LAB profile.  Runs the selected fuzzing mode on a
    QThread.  Results can be exported as CSV.
    """

    def __init__(
        self,
        profile: SessionProfile = SessionProfile.LAB,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("IEC 104 Fuzzer")
        self.setMinimumSize(600, 520)
        self._profile = profile
        self._results: list[FuzzResult] = []
        self._worker: _FuzzWorker | None = None
        self._thread: QThread | None = None
        self._build_ui()
        self._apply_profile_guard()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Safety banner
        self._safety_label = QLabel()
        self._safety_label.setWordWrap(True)
        root.addWidget(self._safety_label)

        # ---- Mode + parameters group ----
        params_box = QGroupBox("Fuzzing Parameters")
        form = QFormLayout(params_box)

        self._mode_combo = QComboBox()
        for label, _ in _MODES:
            self._mode_combo.addItem(label)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Mode:", self._mode_combo)

        self._iterations_spin = QSpinBox()
        self._iterations_spin.setRange(10, 100_000)
        self._iterations_spin.setValue(5000)
        self._iterations_spin.setSingleStep(500)
        form.addRow("Iterations:", self._iterations_spin)

        self._seed_spin = QSpinBox()
        self._seed_spin.setRange(0, 2**31 - 1)
        self._seed_spin.setValue(0)
        form.addRow("Seed:", self._seed_spin)

        # Network target (state-machine / TLS modes)
        self._net_group = QGroupBox("Network Target")
        net_form = QFormLayout(self._net_group)

        self._host_edit = QLineEdit("127.0.0.1")
        net_form.addRow("Host:", self._host_edit)

        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(2404)
        net_form.addRow("Port:", self._port_spin)

        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(1, 30)
        self._timeout_spin.setValue(3)
        self._timeout_spin.setSuffix(" s")
        net_form.addRow("Connect timeout:", self._timeout_spin)

        form.addRow(self._net_group)

        root.addWidget(params_box)

        # ---- Run / Stop buttons ----
        btn_row = QHBoxLayout()
        self._run_btn = QPushButton("Run Fuzzer")
        self._run_btn.clicked.connect(self._run)
        btn_row.addWidget(self._run_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop)
        btn_row.addWidget(self._stop_btn)

        self._export_btn = QPushButton("Export CSV…")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_csv)
        btn_row.addWidget(self._export_btn)
        root.addLayout(btn_row)

        # ---- Progress ----
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        root.addWidget(self._progress)

        # ---- Results ----
        results_box = QGroupBox("Results")
        rl = QVBoxLayout(results_box)
        self._results_view = QPlainTextEdit()
        self._results_view.setReadOnly(True)
        self._results_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        rl.addWidget(self._results_view)
        root.addWidget(results_box, stretch=1)

        # ---- Close button ----
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._on_mode_changed(0)

    def _apply_profile_guard(self) -> None:
        if self._profile is not SessionProfile.LAB:
            msg = (
                f"Fuzzer is disabled outside LAB profile.  "
                f"Current profile: {self._profile.name}.  "
                "Connect a session in LAB mode to enable."
            )
            self._safety_label.setText(f"⚠ {msg}")
            self._safety_label.setStyleSheet("color: red; font-weight: bold;")
            self._run_btn.setEnabled(False)
            self._mode_combo.setEnabled(False)
            self._iterations_spin.setEnabled(False)
            self._seed_spin.setEnabled(False)
            self._net_group.setEnabled(False)
        else:
            self._safety_label.setText("LAB mode — fuzzing enabled.")
            self._safety_label.setStyleSheet("color: green;")

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_mode_changed(self, idx: int) -> None:
        mode = _MODES[idx][1]
        needs_network = mode in ("state_machine", "tls")
        self._net_group.setVisible(needs_network)
        self.adjustSize()

    def _run(self) -> None:
        if self._profile is not SessionProfile.LAB:
            return
        idx = self._mode_combo.currentIndex()
        mode = _MODES[idx][1]
        params = FuzzParams(
            mode=mode,
            iterations=self._iterations_spin.value(),
            seed=self._seed_spin.value(),
            host=self._host_edit.text().strip() or "127.0.0.1",
            port=self._port_spin.value(),
            connect_timeout=float(self._timeout_spin.value()),
        )
        self._progress.setValue(0)
        self._results_view.appendPlainText(
            f"--- Starting {mode} fuzz (iterations={params.iterations}, seed={params.seed}) ---"
        )
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._export_btn.setEnabled(False)

        self._worker = _FuzzWorker(params)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._progress.setValue)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self._results_view.appendPlainText("--- Cancelled by user ---")
        self._cleanup_thread()

    def _on_finished(self, result: FuzzResult) -> None:
        self._results.append(result)
        lines = [
            f"Mode: {result.mode}",
            f"Iterations: {result.iterations}",
            f"Seed: {result.seed}",
            f"Duration: {result.duration_seconds:.2f}s",
            f"Overall: {'PASS ✓' if result.ok else 'FAIL ✗'}",
        ]
        for k, v in result.detail.items():
            lines.append(f"  {k}: {v}")
        if result.error:
            lines.append(f"  Error: {result.error}")
        self._results_view.appendPlainText("\n".join(lines))
        self._results_view.appendPlainText("")
        self._cleanup_thread()
        self._export_btn.setEnabled(bool(self._results))

    def _cleanup_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
            self._worker = None
        self._run_btn.setEnabled(self._profile is SessionProfile.LAB)
        self._stop_btn.setEnabled(False)

    def _export_csv(self) -> None:
        if not self._results:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Fuzzer Report",
            "iec104_fuzz_report.csv",
            "CSV files (*.csv)",
        )
        if not path:
            return
        buf = io.StringIO()
        # Collect all possible detail keys across results
        detail_keys: list[str] = []
        for r in self._results:
            for k in r.detail:
                if k not in detail_keys:
                    detail_keys.append(k)
        fieldnames = [
            "mode",
            "iterations",
            "seed",
            "ok",
            "duration_seconds",
            "error",
            *detail_keys,
        ]
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
        writer.writeheader()
        for r in self._results:
            row: dict[str, Any] = {
                "mode": r.mode,
                "iterations": r.iterations,
                "seed": r.seed,
                "ok": r.ok,
                "duration_seconds": f"{r.duration_seconds:.3f}",
                "error": r.error,
            }
            for k in detail_keys:
                row[k] = r.detail.get(k, "")
            writer.writerow(row)
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write(buf.getvalue())
        self._results_view.appendPlainText(f"Report saved to {path}")
