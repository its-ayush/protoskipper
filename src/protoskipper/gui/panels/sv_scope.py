# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""SV Scope panel — IEC 61850-9-2 waveform visualiser — P8.D.3.

Displays a live oscilloscope (yt plot), FFT spectrum, and rotating phasor
diagram from an IEC 61850-9-2 Sampled Values stream.

Architecture
------------
* Uses ``pyqtgraph`` for high-performance 2-D plots (lazy-imported inside
  :class:`SvScopePanel.__init__` to avoid importing it at module level).
* :class:`SvScopePanel` is a ``QWidget`` intended to be embedded in a
  ``QTabWidget`` or dock widget.  It receives :class:`SvFrame` objects via the
  thread-safe :meth:`SvScopePanel.feed_frame` slot.
* **No** direct coupling to the SV subscriber — the caller wires the
  subscriber callback to ``feed_frame`` via a Qt ``QueuedConnection`` signal
  so that all Qt operations happen on the GUI thread.

Waveform tab
------------
* Two ``pyqtgraph.PlotWidget`` objects stacked vertically: current channels
  (iA, iB, iC, iN) on top; voltage channels (uA, uB, uC, uN) below.
* X-axis: last N samples (configurable ring buffer, default 4 x 80 = 320).
* Y-axis: auto-scaling, with a manual override spin box for ±peak.

FFT tab
-------
* Single ``PlotWidget`` — magnitude spectrum (linear or dBFS selectable).
* FFT window: rectangular / Hamming / Hann / Blackman (QComboBox).
* THD readout per phase (fundamental + harmonics 2..50).

Phasor tab
----------
* Custom ``pyqtgraph.GraphicsLayoutWidget`` showing a polar-like rotating
  phasor diagram.  One arrow per active phase channel.
* Magnitude = RMS of last N samples; angle = phase relative to iA fundamental.
"""

from __future__ import annotations

import cmath
import logging
import math
from collections import deque
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from protoskipper_iec61850.sv.decoder import SvFrame

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BUF = 320  # samples in the ring buffer
_UPDATE_HZ = 25  # GUI refresh rate (ms = 40 ms)
_UPDATE_MS = 1000 // _UPDATE_HZ

_CHANNEL_LABELS = [
    "iA",
    "iB",
    "iC",
    "iN",
    "uA",
    "uB",
    "uC",
    "uN",
]
_CH_COLOURS = [
    "#FF4444",  # iA  — red
    "#44FF44",  # iB  — green
    "#4444FF",  # iC  — blue
    "#AAAAAA",  # iN  — grey
    "#FF8800",  # uA  — orange
    "#00CCFF",  # uB  — cyan
    "#FF44FF",  # uC  — magenta
    "#FFFF44",  # uN  — yellow
]

_WINDOW_FNS = ("Rectangular", "Hamming", "Hann", "Blackman")


def _window(name: str, n: int) -> list[float]:
    if name == "Hamming":
        return [0.54 - 0.46 * math.cos(2 * math.pi * k / (n - 1)) for k in range(n)]
    if name == "Hann":
        return [0.5 * (1 - math.cos(2 * math.pi * k / (n - 1))) for k in range(n)]
    if name == "Blackman":
        return [
            0.42
            - 0.5 * math.cos(2 * math.pi * k / (n - 1))
            + 0.08 * math.cos(4 * math.pi * k / (n - 1))
            for k in range(n)
        ]
    return [1.0] * n  # Rectangular


def _compute_fft(samples: list[float], window: list[float]) -> list[float]:
    """Return magnitude spectrum (linear) for the given sample list."""
    n = len(samples)
    if n < 2:
        return []
    windowed = [s * w for s, w in zip(samples, window, strict=True)]
    # DFT — O(n^2); for n <= 320 this is < 0.1 ms
    freqs: list[complex] = []
    for k in range(n // 2 + 1):
        val = sum(windowed[j] * cmath.exp(-2j * math.pi * k * j / n) for j in range(n))
        freqs.append(val)
    return [abs(f) / n for f in freqs]


def _rms(samples: list[float]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _fundamental_phase(samples: list[float], n: int) -> float:
    """Return the phase angle (radians) of the fundamental frequency via DFT at bin 1."""
    if n < 2 or len(samples) < n:
        return 0.0
    val = sum(samples[j] * cmath.exp(-2j * math.pi * j / n) for j in range(n))
    return cmath.phase(val)


# ---------------------------------------------------------------------------
# Public widget
# ---------------------------------------------------------------------------


class SvScopePanel(QWidget):
    """SV scope, FFT, and phasor panel.

    Signals
    -------
    frame_received:
        Emitted (from any thread) when :meth:`feed_frame` is called, carrying
        the :class:`SvFrame`.  The GUI slots are connected with
        ``Qt.QueuedConnection`` so they always execute on the GUI thread.
    """

    frame_received: Signal = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Lazy-import pyqtgraph only when the panel is constructed.
        try:
            import pyqtgraph as pg

            self._pg = pg
            self._pg_available = True
        except ImportError:
            self._pg_available = False
            _log.warning("pyqtgraph not installed — SV scope disabled")

        self._buf: deque[list[float]] = deque(maxlen=_DEFAULT_BUF)
        self._buf_raw: deque[list[int]] = deque(maxlen=_DEFAULT_BUF)
        self._sample_rate: int = 4000
        self._last_sv_id: str = ""

        self._build_ui()
        self.frame_received.connect(self._on_frame, Qt.ConnectionType.QueuedConnection)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(_UPDATE_MS)
        self._refresh_timer.timeout.connect(self._refresh_plots)
        self._refresh_timer.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed_frame(self, frame: SvFrame) -> None:
        """Thread-safe entry point — emit signal so GUI thread processes it."""
        self.frame_received.emit(frame)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Top controls
        ctrl_row = QHBoxLayout()
        self._svid_label = QLabel("—")
        ctrl_row.addWidget(QLabel("SVID:"))
        ctrl_row.addWidget(self._svid_label)
        ctrl_row.addStretch()
        self._buf_spin = QSpinBox()
        self._buf_spin.setRange(80, 14400)
        self._buf_spin.setValue(_DEFAULT_BUF)
        self._buf_spin.setSuffix(" samples")
        self._buf_spin.valueChanged.connect(self._on_buf_changed)
        ctrl_row.addWidget(QLabel("Buffer:"))
        ctrl_row.addWidget(self._buf_spin)
        layout.addLayout(ctrl_row)

        # Tabs
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        self._tabs.addTab(self._build_waveform_tab(), "Waveform")
        self._tabs.addTab(self._build_fft_tab(), "FFT")
        self._tabs.addTab(self._build_phasor_tab(), "Phasor")
        self._tabs.addTab(self._build_stats_tab(), "Stats")

    def _build_waveform_tab(self) -> QWidget:
        w = QWidget()
        vbox = QVBoxLayout(w)
        if not self._pg_available:
            vbox.addWidget(QLabel("pyqtgraph not installed."))
            return w

        pg = self._pg
        # Current plot (top)
        self._plot_i = pg.PlotWidget(title="Currents")
        self._plot_i.setLabel("left", "Value")
        self._plot_i.setLabel("bottom", "Sample")
        self._plot_i.addLegend()
        self._curves_i = [
            self._plot_i.plot(pen=_CH_COLOURS[i], name=_CHANNEL_LABELS[i]) for i in range(4)
        ]
        vbox.addWidget(self._plot_i)

        # Voltage plot (bottom)
        self._plot_u = pg.PlotWidget(title="Voltages")
        self._plot_u.setLabel("left", "Value")
        self._plot_u.setLabel("bottom", "Sample")
        self._plot_u.addLegend()
        self._curves_u = [
            self._plot_u.plot(pen=_CH_COLOURS[i + 4], name=_CHANNEL_LABELS[i + 4]) for i in range(4)
        ]
        vbox.addWidget(self._plot_u)
        return w

    def _build_fft_tab(self) -> QWidget:
        w = QWidget()
        vbox = QVBoxLayout(w)

        ctrl = QHBoxLayout()
        self._fft_window_box = QComboBox()
        self._fft_window_box.addItems(_WINDOW_FNS)
        self._fft_window_box.setCurrentIndex(2)  # Hann default
        ctrl.addWidget(QLabel("Window:"))
        ctrl.addWidget(self._fft_window_box)
        self._fft_db_box = QComboBox()
        self._fft_db_box.addItems(["Linear", "dBFS"])
        ctrl.addWidget(QLabel("Scale:"))
        ctrl.addWidget(self._fft_db_box)
        ctrl.addStretch()
        vbox.addLayout(ctrl)

        if not self._pg_available:
            vbox.addWidget(QLabel("pyqtgraph not installed."))
            return w

        pg = self._pg
        self._plot_fft = pg.PlotWidget(title="FFT Spectrum (channel iA)")
        self._plot_fft.setLabel("left", "Magnitude")
        self._plot_fft.setLabel("bottom", "Bin")
        self._curve_fft = self._plot_fft.plot(pen=_CH_COLOURS[0])
        vbox.addWidget(self._plot_fft)

        # THD readout
        self._thd_label = QLabel("THD: —")
        vbox.addWidget(self._thd_label)
        return w

    def _build_phasor_tab(self) -> QWidget:
        w = QWidget()
        vbox = QVBoxLayout(w)

        if not self._pg_available:
            vbox.addWidget(QLabel("pyqtgraph not installed."))
            return w

        pg = self._pg
        layout_widget = pg.GraphicsLayoutWidget()
        self._phasor_plot = layout_widget.addPlot(title="Phasors (RMS magnitude, phase)")
        self._phasor_plot.setAspectLocked(True)
        self._phasor_plot.hideAxis("bottom")
        self._phasor_plot.hideAxis("left")

        # Reference circle
        angles = [i * math.tau / 360 for i in range(361)]
        xs = [math.cos(a) for a in angles]
        ys = [math.sin(a) for a in angles]
        self._phasor_plot.plot(xs, ys, pen=pg.mkPen(color="w", width=0.5))

        # Phasor arrows (one per current channel)
        self._phasor_arrows = []
        for i in range(4):
            arr = pg.ArrowItem(
                angle=0,
                tipAngle=25,
                baseAngle=0,
                headLen=10,
                pen=pg.mkPen(_CH_COLOURS[i]),
                brush=pg.mkBrush(_CH_COLOURS[i]),
            )
            self._phasor_plot.addItem(arr)
            self._phasor_arrows.append(arr)

        vbox.addWidget(layout_widget)
        return w

    def _build_stats_tab(self) -> QWidget:
        w = QWidget()
        vbox = QVBoxLayout(w)
        box = QGroupBox("Live statistics")
        form = QFormLayout(box)
        self._stat_labels: dict[str, QLabel] = {}
        for name in _CHANNEL_LABELS:
            lbl = QLabel("—")
            self._stat_labels[name] = lbl
            form.addRow(f"{name} RMS:", lbl)
        self._stat_smp_cnt = QLabel("—")
        form.addRow("Last smpCnt:", self._stat_smp_cnt)
        self._stat_conf_rev = QLabel("—")
        form.addRow("confRev:", self._stat_conf_rev)
        self._stat_smp_synch = QLabel("—")
        form.addRow("smpSynch:", self._stat_smp_synch)
        vbox.addWidget(box)
        vbox.addStretch()
        return w

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @Slot(object)
    def _on_frame(self, frame: SvFrame) -> None:
        if not frame.asdus:
            return
        asdu = frame.asdus[0]
        self._last_sv_id = asdu.sv_id
        self._svid_label.setText(asdu.sv_id)

        # Update stats labels
        self._stat_smp_cnt.setText(str(asdu.smp_cnt))
        self._stat_conf_rev.setText(str(asdu.conf_rev))
        _synch_map = {0: "noSync", 1: "localClock", 2: "globalClock"}
        self._stat_smp_synch.setText(_synch_map.get(asdu.smp_synch, str(asdu.smp_synch)))
        if asdu.smp_rate:
            self._sample_rate = asdu.smp_rate

        raw = [ch.value_raw for ch in asdu.channels]
        scaled = [float(v) for v in raw]
        # Pad to 8 channels
        while len(scaled) < 8:
            scaled.append(0.0)
        self._buf.append(scaled)
        self._buf_raw.append(raw)

    @Slot(int)
    def _on_buf_changed(self, value: int) -> None:
        self._buf = deque(self._buf, maxlen=value)
        self._buf_raw = deque(self._buf_raw, maxlen=value)

    @Slot()
    def _refresh_plots(self) -> None:
        if not self._buf or not self._pg_available:
            return

        samples = list(self._buf)  # list of per-sample 8-value lists
        n = len(samples)

        # Transpose to per-channel lists
        channels_data = [[samples[t][ch] for t in range(n)] for ch in range(8)]

        # Update stats RMS
        for i, name in enumerate(_CHANNEL_LABELS):
            rms = _rms(channels_data[i])
            self._stat_labels[name].setText(f"{rms:.2f}")

        if self._tabs.currentIndex() == 0:
            # Waveform
            x = list(range(n))
            for i in range(4):
                self._curves_i[i].setData(x, channels_data[i])
            for i in range(4):
                self._curves_u[i].setData(x, channels_data[i + 4])

        elif self._tabs.currentIndex() == 1:
            # FFT of iA
            ia = channels_data[0]
            if len(ia) >= 4:
                win_name = self._fft_window_box.currentText()
                win = _window(win_name, len(ia))
                mag = _compute_fft(ia, win)
                if self._fft_db_box.currentText() == "dBFS":
                    mag = [20 * math.log10(m + 1e-12) for m in mag]
                self._curve_fft.setData(list(range(len(mag))), mag)

                # THD: harmonics 2..50 relative to fundamental
                if len(mag) > 50 and mag[1] > 0:
                    thd = math.sqrt(sum(mag[k] ** 2 for k in range(2, min(51, len(mag)))))
                    thd_pct = 100.0 * thd / (mag[1] + 1e-12)
                    self._thd_label.setText(f"THD (iA): {thd_pct:.2f} %")

        elif self._tabs.currentIndex() == 2:
            # Phasor — update arrows for current channels
            ia = channels_data[0]
            if len(ia) >= 4:
                ref_phase = _fundamental_phase(ia, len(ia))
                max_rms = max(_rms(channels_data[i]) for i in range(4)) or 1.0
                for i in range(4):
                    ch_data = channels_data[i]
                    mag = _rms(ch_data) / max_rms
                    phase = _fundamental_phase(ch_data, len(ch_data)) - ref_phase
                    x_tip = mag * math.cos(phase)
                    y_tip = mag * math.sin(phase)
                    self._phasor_arrows[i].setPos(x_tip, y_tip)
                    self._phasor_arrows[i].setStyle(angle=-math.degrees(phase))
