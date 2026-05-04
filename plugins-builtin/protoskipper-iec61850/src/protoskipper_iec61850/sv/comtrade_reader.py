# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""COMTRADE CFG+DAT reader — P8.D.5.

Supports ASCII and binary COMTRADE files conforming to:
- IEEE Std C37.111-1991 (original)
- IEEE Std C37.111-1999
- IEC 60255-24:2013 / IEEE C37.111-2013 (COMTRADE 2013)

Usage
-----
::

    from protoskipper_iec61850.sv.comtrade_reader import ComtradeReader

    reader = ComtradeReader.from_cfg(Path("/path/to/capture.cfg"))
    print(reader.station_name, reader.frequency)
    for sample in reader.samples():
        print(sample.timestamp_us, sample.values)

Design notes
------------
* The CFG parser handles both ``\\r\\n`` and ``\\n`` line endings.
* Binary32 DAT uses ``<II`` + ``<i * n_channels`` per sample record.
* Binary DAT (1999) uses ``<II`` + ``<h * n_channels`` (16-bit signed).
* ASCII DAT is comma-delimited with ``n,timestamp,val0,val1,...``.
* Scaling: each analogue value is returned as ``raw * a + b`` (float).
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class AnalogueChannel:
    """Metadata for one analogue channel decoded from a CFG file."""

    index: int  # 1-based channel number
    name: str
    phase: str
    circuit_component: str
    unit: str
    scale: float  # 'a' multiplier
    offset: float  # 'b' additive offset
    min_val: int
    max_val: int
    primary: float
    secondary: float
    ps: str  # 'P' or 'S'


@dataclass
class ComtradeSample:
    """One decoded sample from a COMTRADE DAT file.

    Attributes
    ----------
    n:
        Sample number (1-based).
    timestamp_us:
        Microseconds since the trigger time.
    values:
        Scaled floating-point values per analogue channel (``raw * a + b``).
    values_raw:
        Raw integer values from the DAT file (before scaling).
    """

    n: int
    timestamp_us: int
    values: list[float]
    values_raw: list[int]


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class ComtradeReader:
    """Read a COMTRADE CFG+DAT file pair.

    Attributes
    ----------
    station_name:
        Station name from the CFG header.
    rec_dev_id:
        Recording device ID from the CFG header.
    revision:
        Year string (``"1991"``, ``"1999"``, ``"2013"``).
    frequency:
        Nominal power-system frequency (50.0 or 60.0).
    channels:
        Ordered list of :class:`AnalogueChannel`.
    dat_format:
        ``"ASCII"``, ``"BINARY"``, or ``"BINARY32"``.
    dat_path:
        Path to the associated ``.dat`` file.
    """

    def __init__(
        self,
        cfg_text: str,
        dat_path: Path,
    ) -> None:
        self.dat_path = dat_path
        self._parse_cfg(cfg_text)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_cfg(cls, cfg_path: Path) -> ComtradeReader:
        """Load a COMTRADE file pair from the ``.cfg`` path."""
        cfg_text = cfg_path.read_text(encoding="ascii", errors="replace")
        dat_path = cfg_path.with_suffix(".dat")
        if not dat_path.exists():
            dat_path = cfg_path.with_suffix(".DAT")
        return cls(cfg_text, dat_path)

    # ------------------------------------------------------------------
    # CFG parser
    # ------------------------------------------------------------------

    def _parse_cfg(self, text: str) -> None:
        lines = [ln.strip() for ln in re.split(r"\r?\n", text) if ln.strip()]
        idx = 0

        # Line 0: station_name, rec_dev_id[, rev_year]
        parts = lines[idx].split(",")
        self.station_name = parts[0].strip() if len(parts) > 0 else ""
        self.rec_dev_id = parts[1].strip() if len(parts) > 1 else ""
        self.revision = parts[2].strip() if len(parts) > 2 else "1991"
        idx += 1

        # Line 1: nA+nD, nA, nD  (e.g. "4A,4A,0D")
        chan_line = lines[idx]
        idx += 1
        m = re.match(r"(\d+)A", chan_line, re.IGNORECASE)
        n_analogue = int(m.group(1)) if m else 0

        # Analogue channel lines
        self.channels: list[AnalogueChannel] = []
        for i in range(n_analogue):
            ch = self._parse_analogue_line(lines[idx], i + 1)
            self.channels.append(ch)
            idx += 1

        # Skip digital channel lines (0D in our files)
        # If there are digital channels, skip them
        # (Not implemented: we only use analogue channels)

        # Frequency
        self.frequency = float(lines[idx].split(",")[0])
        idx += 1

        # Number of sample rates
        n_rates = int(lines[idx])
        idx += 1

        self._sample_rate_info: list[tuple[float, int]] = []
        for _ in range(n_rates):
            rate_parts = lines[idx].split(",")
            rate = float(rate_parts[0])
            endsamp = int(rate_parts[1])
            self._sample_rate_info.append((rate, endsamp))
            idx += 1

        # Start and trigger timestamps (we store but don't currently parse)
        self._start_time_str = lines[idx] if idx < len(lines) else ""
        idx += 1
        self._trigger_time_str = lines[idx] if idx < len(lines) else ""
        idx += 1

        # Data file type
        self.dat_format = lines[idx].strip().upper() if idx < len(lines) else "ASCII"
        idx += 1

        # Time multiplier (μs per timestamp unit)
        if idx < len(lines):
            try:
                self._time_multiplier = float(lines[idx])
            except ValueError:
                self._time_multiplier = 1.0
            idx += 1
        else:
            self._time_multiplier = 1.0

    @staticmethod
    def _parse_analogue_line(line: str, ch_num: int) -> AnalogueChannel:
        """Parse one CFG analogue channel line."""
        parts = [p.strip() for p in line.split(",")]

        # Fields: An, ch_id, ph, ccbm, uu, a, b, skew, min, max, primary, secondary, P/S
        def _get(i: int, default: str = "") -> str:
            return parts[i] if i < len(parts) else default

        return AnalogueChannel(
            index=ch_num,
            name=_get(1),
            phase=_get(2),
            circuit_component=_get(3),
            unit=_get(4),
            scale=float(_get(5) or "1.0"),
            offset=float(_get(6) or "0.0"),
            min_val=int(_get(8) or "-32768"),
            max_val=int(_get(9) or "32767"),
            primary=float(_get(10) or "1.0"),
            secondary=float(_get(11) or "1.0"),
            ps=_get(12, "P"),
        )

    # ------------------------------------------------------------------
    # Sample iteration
    # ------------------------------------------------------------------

    def samples(self) -> Iterator[ComtradeSample]:
        """Iterate decoded samples from the DAT file."""
        if self.dat_format == "ASCII":
            yield from self._read_ascii()
        elif self.dat_format == "BINARY32":
            yield from self._read_binary32()
        elif self.dat_format == "BINARY":
            yield from self._read_binary()
        else:
            raise ValueError(f"Unsupported COMTRADE DAT format: {self.dat_format!r}")

    def all_samples(self) -> list[ComtradeSample]:
        """Return all samples as a list (loads entire file into memory)."""
        return list(self.samples())

    # ------------------------------------------------------------------
    # Format-specific readers
    # ------------------------------------------------------------------

    def _read_ascii(self) -> Iterator[ComtradeSample]:
        text = self.dat_path.read_text(encoding="ascii", errors="replace")
        for line in re.split(r"\r?\n", text):
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 2 + len(self.channels):
                continue
            try:
                n = int(parts[0])
                ts_raw = int(parts[1])
                raw = [int(parts[2 + i]) for i in range(len(self.channels))]
                scaled = [raw[i] * ch.scale + ch.offset for i, ch in enumerate(self.channels)]
                yield ComtradeSample(
                    n=n,
                    timestamp_us=int(ts_raw * self._time_multiplier),
                    values=scaled,
                    values_raw=raw,
                )
            except (ValueError, IndexError):
                continue

    def _read_binary32(self) -> Iterator[ComtradeSample]:
        """BINARY32: each sample = uint32 n + uint32 ts + int32 x n_ch."""
        n_ch = len(self.channels)
        record_size = 8 + 4 * n_ch  # 2 x uint32 header + n_ch x int32
        data = self.dat_path.read_bytes()
        offset = 0
        while offset + record_size <= len(data):
            n, ts_raw = struct.unpack_from("<II", data, offset)
            raw = list(struct.unpack_from(f"<{n_ch}i", data, offset + 8))
            scaled = [raw[i] * ch.scale + ch.offset for i, ch in enumerate(self.channels)]
            yield ComtradeSample(
                n=n,
                timestamp_us=int(ts_raw * self._time_multiplier),
                values=scaled,
                values_raw=raw,
            )
            offset += record_size

    def _read_binary(self) -> Iterator[ComtradeSample]:
        """BINARY (1999-style): each sample = uint32 n + uint32 ts + int16 x n_ch."""
        n_ch = len(self.channels)
        record_size = 8 + 2 * n_ch  # 2 x uint32 header + n_ch x int16
        data = self.dat_path.read_bytes()
        offset = 0
        while offset + record_size <= len(data):
            n, ts_raw = struct.unpack_from("<II", data, offset)
            raw = list(struct.unpack_from(f"<{n_ch}h", data, offset + 8))
            scaled = [raw[i] * ch.scale + ch.offset for i, ch in enumerate(self.channels)]
            yield ComtradeSample(
                n=n,
                timestamp_us=int(ts_raw * self._time_multiplier),
                values=scaled,
                values_raw=raw,
            )
            offset += record_size
