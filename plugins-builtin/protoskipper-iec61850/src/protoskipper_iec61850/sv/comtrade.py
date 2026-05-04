# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""COMTRADE 1991/1999/2013 exporter — P8.D.4.

Generates a pair of ASCII files — ``.cfg`` and ``.dat`` — for a captured
SV channel buffer.  The format follows IEEE/IEC C37.111-2013 (COMTRADE 2013)
which is backward-compatible with 1991 and 1999 readers.

Usage example
-------------
::

    from protoskipper_iec61850.sv.comtrade import ComtradeExporter, ChannelSpec
    from pathlib import Path

    exporter = ComtradeExporter(
        station_name="TEST_SSD",
        rec_dev_id="PROT_01",
        frequency=50.0,
        channels=[
            ChannelSpec("iA", unit="A", primary=100.0, secondary=1.0),
            ChannelSpec("iB", unit="A", primary=100.0, secondary=1.0),
            ChannelSpec("iC", unit="A", primary=100.0, secondary=1.0),
            ChannelSpec("uA", unit="V", primary=110e3, secondary=110.0),
        ],
    )
    # Feed samples (list of per-channel raw int32 values, timestamp μs)
    for ts_us, raw_values in stream:
        exporter.add_sample(ts_us, raw_values)

    exporter.write(Path("/tmp/capture"))  # writes capture.cfg + capture.dat
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class ChannelSpec:
    """Metadata for one analogue channel exported to COMTRADE.

    Attributes
    ----------
    name:
        Channel name (e.g. ``"iA"``).
    unit:
        Engineering unit (e.g. ``"A"`` or ``"V"``).
    primary:
        Primary transformer ratio numerator (e.g. 100.0 for a 100 A CT).
    secondary:
        Secondary transformer ratio denominator (e.g. 1.0).
    scale:
        Linear scale factor applied to raw int32 values before writing.
        The COMTRADE ``a`` multiplier; default 1.0.
    offset:
        Additive offset applied after scaling.  The COMTRADE ``b`` adder;
        default 0.0.
    min_val:
        Minimum expected value (COMTRADE ``min`` field); default -32768.
    max_val:
        Maximum expected value (COMTRADE ``max`` field); default 32767.
    phase:
        Phase label (A/B/C/N or empty), optional.
    circuit_component:
        Circuit component label (e.g. ``"Bay1"``), optional.
    """

    name: str
    unit: str = "A"
    primary: float = 1.0
    secondary: float = 1.0
    scale: float = 1.0
    offset: float = 0.0
    min_val: int = -32768
    max_val: int = 32767
    phase: str = ""
    circuit_component: str = ""


@dataclass
class ComtradeExporter:
    """Accumulates SV samples and writes a COMTRADE CFG+DAT pair.

    Parameters
    ----------
    station_name:
        Substation/station identifier written into the CFG header.
    rec_dev_id:
        Recording device identifier written into the CFG header.
    frequency:
        Nominal power-system frequency in Hz (50 or 60).
    channels:
        Ordered list of :class:`ChannelSpec` objects.  The order must match
        the values passed to :meth:`add_sample`.
    """

    station_name: str
    rec_dev_id: str
    frequency: float
    channels: list[ChannelSpec]
    _samples: list[tuple[int, list[int]]] = field(default_factory=list, init=False)
    _start_ts_us: int | None = field(default=None, init=False)

    def add_sample(self, timestamp_us: int, raw_values: Sequence[int]) -> None:
        """Append one sample row.

        Parameters
        ----------
        timestamp_us:
            Absolute timestamp in microseconds since the Unix epoch.
        raw_values:
            Per-channel signed int32 values in channel order.
        """
        if len(raw_values) != len(self.channels):
            raise ValueError(f"Expected {len(self.channels)} values, got {len(raw_values)}")
        if self._start_ts_us is None:
            self._start_ts_us = timestamp_us
        self._samples.append((timestamp_us, list(raw_values)))

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def write(self, path: Path) -> None:
        """Write *path*.cfg and *path*.dat.

        Parameters
        ----------
        path:
            Base path without extension (e.g. ``Path("/tmp/capture")``).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path = path.with_suffix(".cfg")
        dat_path = path.with_suffix(".dat")
        cfg_path.write_text(self._build_cfg(dat_path.name), encoding="ascii")
        dat_path.write_bytes(self._build_dat_binary())

    # ------------------------------------------------------------------
    # CFG builder
    # ------------------------------------------------------------------

    def _build_cfg(self, dat_filename: str) -> str:
        lines: list[str] = []

        # Line 1: station_name, rec_dev_id, rev_year
        lines.append(f"{self.station_name},{self.rec_dev_id},2013")

        # Line 2: nA+nD,nA,nD
        n_a = len(self.channels)
        lines.append(f"{n_a}A,{n_a}A,0D")

        # Lines 3..(3+nA-1): analogue channel info
        for i, ch in enumerate(self.channels, start=1):
            # An, ch_id, ph, ccbm, uu, a, b, skew, min, max, primary, secondary, P/S
            a_field = f"{ch.scale:.9f}"
            b_field = f"{ch.offset:.9f}"
            prim = f"{ch.primary:.6f}"
            sec = f"{ch.secondary:.6f}"
            lines.append(
                f"{i},{ch.name},{ch.phase},{ch.circuit_component},{ch.unit},"
                f"{a_field},{b_field},0,{ch.min_val},{ch.max_val},"
                f"{prim},{sec},P"
            )

        # Frequency line
        lines.append(f"{self.frequency:.3f}")

        # Number of sample rates
        lines.append("1")

        # Sample rate, endsamp
        n_samples = len(self._samples)
        if n_samples > 1 and self._start_ts_us is not None:
            last_ts = self._samples[-1][0]
            duration_s = (last_ts - self._start_ts_us) / 1e6
            rate = (n_samples - 1) / duration_s if duration_s > 0 else 0.0
        else:
            rate = self.frequency * 80  # default: assume 80 samp/cycle
        lines.append(f"{rate:.3f},{n_samples}")

        # Start / trigger time
        if self._start_ts_us is not None:
            dt_start = datetime.fromtimestamp(self._start_ts_us / 1e6, tz=timezone.utc)
            trigger_dt = dt_start
        else:
            dt_start = datetime.now(tz=timezone.utc)
            trigger_dt = dt_start

        lines.append(dt_start.strftime("%d/%m/%Y,%H:%M:%S.%f"))
        lines.append(trigger_dt.strftime("%d/%m/%Y,%H:%M:%S.%f"))

        # Data file type (binary32 for our output)
        lines.append("BINARY32")

        # Time multiplier (μs per timestamp unit)
        lines.append("1")

        # Time code / local code (UTC / UTC)
        lines.append("0,0")

        # Leap second indicator
        lines.append("4")

        return "\r\n".join(lines) + "\r\n"

    # ------------------------------------------------------------------
    # DAT builder (binary32 format)
    # ------------------------------------------------------------------

    def _build_dat_binary(self) -> bytes:
        """Build binary COMTRADE .dat (BINARY32 format).

        Each sample record:
          n (uint32) — sample number (1-based)
          timestamp (uint32) — microseconds from trigger
          channel values (int32 x n_channels)
        """
        buf = bytearray()
        t0 = self._start_ts_us if self._start_ts_us is not None else 0
        for idx, (ts_us, values) in enumerate(self._samples, start=1):
            ts_delta = ts_us - t0
            buf += struct.pack("<II", idx, ts_delta)
            for v in values:
                buf += struct.pack("<i", v)
        return bytes(buf)


# ---------------------------------------------------------------------------
# ASCII .dat variant (for compatibility)
# ---------------------------------------------------------------------------


def write_comtrade_ascii(
    exporter: ComtradeExporter,
    path: Path,
) -> None:
    """Write COMTRADE files using ASCII .dat format (space-delimited).

    Parameters
    ----------
    exporter:
        A populated :class:`ComtradeExporter`.
    path:
        Base path without extension.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg_text = exporter._build_cfg(path.with_suffix(".dat").name).replace("BINARY32", "ASCII")
    path.with_suffix(".cfg").write_text(cfg_text, encoding="ascii")

    t0 = exporter._start_ts_us if exporter._start_ts_us is not None else 0
    lines: list[str] = []
    for idx, (ts_us, values) in enumerate(exporter._samples, start=1):
        ts_delta = ts_us - t0
        row = f"{idx},{ts_delta}," + ",".join(str(v) for v in values)
        lines.append(row)
    path.with_suffix(".dat").write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
