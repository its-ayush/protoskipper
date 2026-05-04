# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Bench layout file format (P7.H.1 — §5.12).

A *bench layout* is a YAML (or JSON) file that describes a set of BACnet
devices organised on a test bench: their session parameters, visual tile
positions, pinned points, and display colours.

File extension convention:  ``*.bench.yaml`` or ``*.bench.json``.

Schema version: ``1``.

Example YAML::

    schema_version: 1
    name: "HVAC Lab Bench A"
    created: "2026-05-04T09:00:00"
    devices:
      - label: "AHU-1"
        protocol: "bacnet.ip"
        address: "192.168.1.10"
        device_id: 100
        tile:
          col: 0
          row: 0
          color: "#4CAF50"
        pinned_points:
          - "analog-input:1"
          - "analog-output:2"
      - label: "FCU-101"
        protocol: "bacnet.ip"
        address: "192.168.1.20"
        device_id: 200
        tile:
          col: 1
          row: 0
          color: "#2196F3"

Usage::

    from protoskipper.builtin_drivers.bacnet.bench import BenchLayout

    layout = BenchLayout.load("lab.bench.yaml")
    layout.devices.append(BenchDevice(label="New", protocol="bacnet.ip", address="10.0.0.1"))
    layout.save("lab.bench.yaml")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["BenchDevice", "BenchLayout", "TilePosition", "load_bench", "save_bench"]

_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TilePosition:
    """Grid position and visual style for a device tile on the bench canvas."""

    col: int = 0
    row: int = 0
    color: str = "#607D8B"  # material blue-grey default
    width: int = 1  # grid columns spanned
    height: int = 1  # grid rows spanned

    def to_dict(self) -> dict[str, Any]:
        return {
            "col": self.col,
            "row": self.row,
            "color": self.color,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TilePosition:
        return cls(
            col=int(d.get("col", 0)),
            row=int(d.get("row", 0)),
            color=str(d.get("color", "#607D8B")),
            width=int(d.get("width", 1)),
            height=int(d.get("height", 1)),
        )


@dataclass
class BenchDevice:
    """One device entry in a bench layout.

    Attributes
    ----------
    label:
        Human-readable device label shown on the tile.
    protocol:
        ProtoSkipper protocol identifier, e.g. ``"bacnet.ip"``.
    address:
        Transport address string, e.g. ``"192.168.1.10"`` or
        ``"192.168.1.10:47808"``.
    device_id:
        BACnet Device Object Identifier (numeric); ``-1`` if unknown.
    tile:
        Visual tile position and colour on the bench canvas.
    pinned_points:
        Up to 3 BACnet object-identifier strings whose present-value is
        displayed live on the tile (e.g. ``"analog-input:1"``).
    notes:
        Freeform operator notes stored alongside the device.
    """

    label: str = ""
    protocol: str = "bacnet.ip"
    address: str = ""
    device_id: int = -1
    tile: TilePosition = field(default_factory=TilePosition)
    pinned_points: list[str] = field(default_factory=list)
    notes: str = ""

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        if len(self.pinned_points) > 3:
            raise ValueError(
                f"BenchDevice '{self.label}': pinned_points must have at most 3 entries, "
                f"got {len(self.pinned_points)}"
            )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "label": self.label,
            "protocol": self.protocol,
            "address": self.address,
        }
        if self.device_id >= 0:
            d["device_id"] = self.device_id
        d["tile"] = self.tile.to_dict()
        if self.pinned_points:
            d["pinned_points"] = list(self.pinned_points)
        if self.notes:
            d["notes"] = self.notes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BenchDevice:
        tile_raw = d.get("tile", {})
        return cls(
            label=str(d.get("label", "")),
            protocol=str(d.get("protocol", "bacnet.ip")),
            address=str(d.get("address", "")),
            device_id=int(d.get("device_id", -1)),
            tile=TilePosition.from_dict(tile_raw) if isinstance(tile_raw, dict) else TilePosition(),
            pinned_points=list(d.get("pinned_points", [])),
            notes=str(d.get("notes", "")),
        )


@dataclass
class BenchLayout:
    """Top-level bench layout.

    Attributes
    ----------
    name:
        Human-readable bench name.
    devices:
        Ordered list of devices on this bench.
    created:
        ISO-8601 creation timestamp (set automatically on first save).
    modified:
        ISO-8601 last-modified timestamp (updated on every save).
    schema_version:
        File format version — always ``1`` for this implementation.
    """

    name: str = "Untitled Bench"
    devices: list[BenchDevice] = field(default_factory=list)
    created: str = ""
    modified: str = ""
    schema_version: int = _SCHEMA_VERSION

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        if not self.created:
            self.created = now
        self.modified = now
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "created": self.created,
            "modified": self.modified,
            "devices": [d.to_dict() for d in self.devices],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BenchLayout:
        version = int(d.get("schema_version", 1))
        if version > _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported bench layout schema version {version} "
                f"(this build supports up to {_SCHEMA_VERSION})"
            )
        devices = [BenchDevice.from_dict(dev) for dev in d.get("devices", [])]
        return cls(
            name=str(d.get("name", "Untitled Bench")),
            devices=devices,
            created=str(d.get("created", "")),
            modified=str(d.get("modified", "")),
            schema_version=version,
        )

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> BenchLayout:
        """Load a bench layout from a ``.bench.yaml`` or ``.bench.json`` file.

        Parameters
        ----------
        path:
            Path to the bench file.  Suffix must be ``.yaml``, ``.yml``, or
            ``.json``.

        Raises
        ------
        ValueError
            If the file extension is unrecognised or the schema version is
            too new.
        FileNotFoundError
            If the file does not exist.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Bench file not found: {p}")
        suffix = p.suffix.lower()
        raw = p.read_text(encoding="utf-8")
        if suffix in {".yaml", ".yml"}:
            data = _yaml_load(raw)
        elif suffix == ".json":
            data = json.loads(raw)
        else:
            raise ValueError(
                f"Unrecognised bench file extension {suffix!r}; expected .yaml, .yml, or .json"
            )
        if not isinstance(data, dict):
            raise ValueError("Bench file must be a YAML/JSON mapping at the top level")
        return cls.from_dict(data)

    def save(self, path: str | Path, *, fmt: str = "yaml") -> None:
        """Save the bench layout.

        Parameters
        ----------
        path:
            Destination path.
        fmt:
            ``"yaml"`` (default) or ``"json"``.
        """
        p = Path(path)
        data = self.to_dict()
        if fmt == "json":
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            p.write_text(_yaml_dump(data), encoding="utf-8")

    def add_device(self, device: BenchDevice) -> None:
        """Append a device; auto-assign next grid column if tile is at (0,0)."""
        if device.tile.col == 0 and device.tile.row == 0 and self.devices:
            device.tile.col = max(d.tile.col for d in self.devices) + 1
        self.devices.append(device)

    def remove_device(self, label: str) -> bool:
        """Remove the first device with the given label.  Returns ``True`` if found."""
        for i, d in enumerate(self.devices):
            if d.label == label:
                del self.devices[i]
                return True
        return False

    def get_device(self, label: str) -> BenchDevice | None:
        """Return the first device with the given label, or ``None``."""
        for d in self.devices:
            if d.label == label:
                return d
        return None


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------


def load_bench(path: str | Path) -> BenchLayout:
    """Convenience wrapper around :meth:`BenchLayout.load`."""
    return BenchLayout.load(path)


def save_bench(layout: BenchLayout, path: str | Path, *, fmt: str = "yaml") -> None:
    """Convenience wrapper around :meth:`BenchLayout.save`."""
    layout.save(path, fmt=fmt)


# ---------------------------------------------------------------------------
# Minimal YAML helpers (stdlib-only, no PyYAML required at import time)
# ---------------------------------------------------------------------------


def _yaml_load(text: str) -> Any:
    """Load YAML, preferring PyYAML if available; fall back to JSON-safe parser."""
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(text)
    except ImportError:
        pass
    # Minimal fallback: strip YAML comments and try JSON
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return json.loads("\n".join(lines))


def _yaml_dump(data: Any) -> str:
    """Dump data to YAML, using PyYAML if available; otherwise JSON."""
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.dump(data, default_flow_style=False, allow_unicode=True)
    except ImportError:
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
