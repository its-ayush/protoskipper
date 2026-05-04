# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Substation bench layout — file format and live-status model — P8.E.

Provides:

* :class:`IedTileSpec` — per-IED tile layout/colour/expectation metadata.
* :class:`BenchLayout` — the full layout for one SCD; serialisable as
  a ``bench_layout`` JSON section in an ``iec61850-setup.json`` file.
* :func:`load_bench_layout` / :func:`save_bench_layout` — JSON I/O helpers.
* :class:`BenchStatusUpdater` — Qt-independent timer that polls session
  keep-alive info and emits tile health updates via a callback.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "BenchLayout",
    "BenchLayoutError",
    "BenchStatusUpdater",
    "IedHealth",
    "IedTileSpec",
    "load_bench_layout",
    "save_bench_layout",
]

_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class IedTileSpec:
    """Persisted metadata for one IED tile in the bench overview.

    Attributes
    ----------
    ied_name:
        IED name (matches ``IED/@name`` in the SCD).
    x:
        Tile column position in a grid layout (0-based).
    y:
        Tile row position (0-based).
    colour:
        CSS-style hex colour string for the tile background
        (e.g. ``"#2c6e49"``).  Empty string = use default.
    expected_goose_refs:
        List of GoCB references this IED is expected to publish.  Any
        that are not seen within ``MaxTime`` turn the GOOSE health LED amber.
    notes:
        Free-text commissioning notes attached to this tile.
    """

    ied_name: str
    x: int = 0
    y: int = 0
    colour: str = ""
    expected_goose_refs: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ied_name": self.ied_name,
            "x": self.x,
            "y": self.y,
            "colour": self.colour,
            "expected_goose_refs": self.expected_goose_refs,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> IedTileSpec:
        return cls(
            ied_name=str(d.get("ied_name", "")),
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            colour=str(d.get("colour", "")),
            expected_goose_refs=list(d.get("expected_goose_refs", [])),
            notes=str(d.get("notes", "")),
        )


@dataclass
class BenchLayout:
    """Full bench layout for one SCD file.

    Attributes
    ----------
    scd_path:
        Canonical path to the SCD that this layout belongs to.
    tiles:
        IED tiles keyed by IED name.
    """

    scd_path: str = ""
    tiles: dict[str, IedTileSpec] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _SCHEMA_VERSION,
            "scd_path": self.scd_path,
            "tiles": {name: tile.to_dict() for name, tile in self.tiles.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BenchLayout:
        tiles = {name: IedTileSpec.from_dict(tile_d) for name, tile_d in d.get("tiles", {}).items()}
        return cls(scd_path=str(d.get("scd_path", "")), tiles=tiles)


class BenchLayoutError(ValueError):
    """Raised when a bench layout JSON document is structurally invalid."""


# ---------------------------------------------------------------------------
# JSON I/O
# ---------------------------------------------------------------------------


def save_bench_layout(layout: BenchLayout, path: Path) -> None:
    """Serialise *layout* to *path* (pretty-printed JSON, UTF-8)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = layout.to_dict()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_bench_layout(path: Path) -> BenchLayout:
    """Load a :class:`BenchLayout` from *path*.

    Raises
    ------
    BenchLayoutError
        If the file is missing, invalid JSON, or schema-incompatible.
    FileNotFoundError
        If *path* does not exist.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BenchLayoutError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BenchLayoutError("Bench layout JSON must be a JSON object")
    version = data.get("version", 0)
    if version != _SCHEMA_VERSION:
        raise BenchLayoutError(
            f"Unsupported bench layout schema version {version!r} (expected {_SCHEMA_VERSION})"
        )
    return BenchLayout.from_dict(data)


# ---------------------------------------------------------------------------
# Live-status model
# ---------------------------------------------------------------------------


@dataclass
class IedHealth:
    """Snapshot of one IED's live health, computed by :class:`BenchStatusUpdater`.

    Attributes
    ----------
    ied_name:
        IED name.
    mms_online:
        True = MMS keep-alive responded within the last poll cycle.
    goose_green:
        Number of expected GOOSE streams received within MaxTime.
    goose_amber:
        Number of GOOSE streams between MaxTime and the hold-off threshold.
    goose_red:
        Number of GOOSE streams stale or never seen.
    sv_ok:
        True = at least one SV sample arrived in the last poll cycle.
    last_report_ms:
        Unix timestamp (ms) of the last MMS report received, or 0.
    last_error:
        Most recent MMS error string, or ``""`` if none.
    """

    ied_name: str
    mms_online: bool = False
    goose_green: int = 0
    goose_amber: int = 0
    goose_red: int = 0
    sv_ok: bool = False
    last_report_ms: float = 0.0
    last_error: str = ""

    @property
    def overall_colour(self) -> str:
        """CSS colour string summarising the tile health."""
        if not self.mms_online:
            return "#cc0000"  # red — offline
        if self.goose_red > 0:
            return "#cc6600"  # amber-red — GOOSE alarm
        if self.goose_amber > 0:
            return "#ffcc00"  # amber — GOOSE warning
        return "#00aa44"  # green — healthy


class BenchStatusUpdater:
    """Poll-based health updater — framework-independent.

    Callers provide a ``get_status`` callback that returns the live data
    for a given IED name.  :meth:`poll` computes an :class:`IedHealth`
    per tile and fires the ``on_health`` callback.

    This class is intentionally Qt-free so that it can be unit-tested
    without a Qt event loop.  The GUI wraps it in a ``QTimer``.

    Parameters
    ----------
    layout:
        The bench layout to monitor.
    get_mms_last_seen:
        ``get_mms_last_seen(ied_name) -> float`` — returns the Unix time (s)
        of the last successful MMS keep-alive for *ied_name*, or 0 if not
        connected.
    get_goose_last_seen:
        ``get_goose_last_seen(go_cb_ref) -> float`` — returns the Unix time (s)
        of the last GOOSE frame for *go_cb_ref*, or 0 if never seen.
    get_sv_last_seen:
        ``get_sv_last_seen(sv_id) -> float`` — returns the Unix time (s) of
        the last SV sample for *sv_id*, or 0 if never seen.
    on_health:
        Callback fired once per :meth:`poll` per tile:
        ``on_health(health: IedHealth)``.
    mms_timeout_s:
        How many seconds without a keep-alive until MMS is considered offline.
        Default: 5.0.
    goose_max_time_s:
        Seconds after which a GOOSE stream is amber.  Default: 4.0.
    goose_holdoff_s:
        Seconds after which a GOOSE stream is red (stale).  Default: 8.0.
    sv_timeout_s:
        Seconds without SV samples until sv_ok = False.  Default: 1.0.
    """

    def __init__(
        self,
        layout: BenchLayout,
        *,
        get_mms_last_seen: Any,  # Callable[[str], float]
        get_goose_last_seen: Any,  # Callable[[str], float]
        get_sv_last_seen: Any,  # Callable[[str], float]
        on_health: Any,  # Callable[[IedHealth], None]
        mms_timeout_s: float = 5.0,
        goose_max_time_s: float = 4.0,
        goose_holdoff_s: float = 8.0,
        sv_timeout_s: float = 1.0,
    ) -> None:
        self._layout = layout
        self._get_mms = get_mms_last_seen
        self._get_goose = get_goose_last_seen
        self._get_sv = get_sv_last_seen
        self._on_health = on_health
        self._mms_timeout = mms_timeout_s
        self._goose_max_time = goose_max_time_s
        self._goose_holdoff = goose_holdoff_s
        self._sv_timeout = sv_timeout_s

    def poll(self) -> list[IedHealth]:
        """Compute and return health snapshots for all tiles.

        Also fires ``on_health`` for each tile.
        """
        now = time.time()
        results: list[IedHealth] = []
        for ied_name, tile in self._layout.tiles.items():
            mms_last = self._get_mms(ied_name)
            mms_online = mms_last > 0 and (now - mms_last) < self._mms_timeout

            goose_green = goose_amber = goose_red = 0
            for go_ref in tile.expected_goose_refs:
                last_seen = self._get_goose(go_ref)
                if last_seen == 0:
                    goose_red += 1
                else:
                    age = now - last_seen
                    if age < self._goose_max_time:
                        goose_green += 1
                    elif age < self._goose_holdoff:
                        goose_amber += 1
                    else:
                        goose_red += 1

            sv_last = self._get_sv(ied_name)
            sv_ok = sv_last > 0 and (now - sv_last) < self._sv_timeout

            health = IedHealth(
                ied_name=ied_name,
                mms_online=mms_online,
                goose_green=goose_green,
                goose_amber=goose_amber,
                goose_red=goose_red,
                sv_ok=sv_ok,
            )
            self._on_health(health)
            results.append(health)
        return results
