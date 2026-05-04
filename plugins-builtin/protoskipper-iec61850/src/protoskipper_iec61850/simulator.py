# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""IEC 61850 IED Simulator — P8.G.1.

Wraps pyiec61850's ``IedServer`` to serve an MMS data model built from
an SCL :class:`~protoskipper_iec61850.scl.model.SclDocument`.  Designed
for:

* **Field commissioning** — substitute a missing IED so the rest of the
  bay can be commissioned while waiting for hardware.
* **Lab testing** — deterministic server against which the MMS client
  can be validated.
* **Education / demos** — observable IED behaviour from a Python script.

Data model construction
-----------------------
The simulator walks every :class:`~protoskipper_iec61850.scl.model.FCDA`
in every :class:`~protoskipper_iec61850.scl.model.DataSet` of the target
IED and registers each referenced data attribute in the pyiec61850 model
hierarchy::

    SclDocument → IED → AccessPoint → LDevice → LN → DataSet → FCDA
    ↓
    IedModel → IedDomain → LogicalNode → DataObject → DataAttribute (leaf)

Intermediate structural nodes (MMS_STRUCTURE) are created automatically
when ``da_name`` contains dot-separated path components (e.g.
``"phsA.mag.f"`` produces a ``phsA`` struct → ``mag`` struct → ``f``
float leaf).

Thread safety
-------------
:class:`IedSimulator` is **not** thread-safe.  All calls to
:meth:`~IedSimulator.update_da`, :meth:`~IedSimulator.read_da`,
:meth:`~IedSimulator.start`, and :meth:`~IedSimulator.stop` must
originate from the same Python thread, or the caller must hold an
external lock.  The underlying libiec61850 ``IedServer`` is internally
threaded (client connections run in C threads); this module serialises
all data-model updates via ``IedServer_lockDataModel`` /
``IedServer_unlockDataModel``.

pyiec61850 availability
-----------------------
``pyiec61850`` is **not** pip-installable — it is built from the
libiec61850 source tree.  If it is absent, :meth:`~IedSimulator.start`
raises :class:`SimulatorError` with an installation hint.  All other
methods (including model introspection) work without it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from protoskipper.core.errors import DriverError

from protoskipper_iec61850.scl.model import FCDA, IED, SclDocument

_log = logging.getLogger(__name__)

__all__ = [
    "IedSimulator",
    "SimulatedPoint",
    "SimulatorConfig",
    "SimulatorError",
]

# ---------------------------------------------------------------------------
# MMS type integer constants (MmsType enum — mms_value.h)
# These are replicated here so this module is self-contained.
# ---------------------------------------------------------------------------
_MMS_STRUCTURE: int = 1
_MMS_BOOLEAN: int = 2
_MMS_INTEGER: int = 4
_MMS_UNSIGNED: int = 5
_MMS_FLOAT: int = 6
_MMS_VISIBLE_STRING: int = 8
_MMS_UTC_TIME: int = 14

# FC abbreviation → IEC61850_FC_* integer (ied_client_api.h FunctionalConstraint)
_FC_INT: dict[str, int] = {
    "ST": 0,
    "MX": 1,
    "SP": 2,
    "SV": 3,
    "CF": 4,
    "DC": 5,
    "SG": 6,
    "SE": 7,
    "SR": 8,
    "OR": 9,
    "BL": 10,
    "EX": 11,
    "CO": 12,
}

# FC abbreviation → default MMS leaf type (heuristic)
_FC_MMS_TYPE: dict[str, int] = {
    "ST": _MMS_BOOLEAN,
    "MX": _MMS_FLOAT,
    "SP": _MMS_FLOAT,
    "SV": _MMS_FLOAT,
    "CF": _MMS_FLOAT,
    "DC": _MMS_VISIBLE_STRING,
}

# TriggerOptions bit mask
_TRG_DCHG: int = 2
_TRG_QCHG: int = 4
_TRG_GI: int = 128
_TRG_DEFAULT: int = _TRG_DCHG | _TRG_QCHG | _TRG_GI


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SimulatorError(DriverError):
    """Raised when the IED simulator encounters a configuration or runtime error."""


@dataclass
class SimulatorConfig:
    """Configuration for :class:`IedSimulator`."""

    ied_name: str
    """Name of the IED to simulate (must exist in the SCL document)."""

    port: int = 102
    """TCP port to listen on (default 102 / MMS)."""

    host: str = "0.0.0.0"
    """Bind address (default: all interfaces)."""


@dataclass
class SimulatedPoint:
    """A data attribute registered in the simulator's data model."""

    ref: str
    """Reference string ``LD/LN.DO.DA[FC]``, e.g. ``"LD0/MMXU1.PhV.phsA.mag.f[MX]"``."""

    fc: str
    """Functional constraint abbreviation (ST, MX, SP, …)."""

    mms_type: int
    """MMS leaf type integer (``_MMS_FLOAT``, ``_MMS_BOOLEAN``, …)."""

    value: float | bool | int | str | None = None
    """Current value; ``None`` until first :meth:`~IedSimulator.update_da` call."""

    # Internal: pyiec61850 DataAttribute handle (set during start())
    _da_handle: Any = field(default=None, repr=False, compare=False)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ied_for_name(scl: SclDocument, ied_name: str) -> IED:
    for ied in scl.ieds:
        if ied.name == ied_name:
            return ied
    raise SimulatorError(
        f"IED {ied_name!r} not found in SCL document (available: {[i.name for i in scl.ieds]})"
    )


def _fcda_ref(fcda: FCDA) -> str:
    """Build a canonical reference string from an FCDA."""
    ln = f"{fcda.prefix}{fcda.ln_class}{fcda.ln_inst}"
    ref = f"{fcda.ld_inst}/{ln}.{fcda.do_name}"
    if fcda.da_name:
        ref = f"{ref}.{fcda.da_name}"
    if fcda.fc:
        ref = f"{ref}[{fcda.fc}]"
    return ref


def _collect_points(ied: IED) -> dict[str, SimulatedPoint]:
    """Walk the IED's datasets and return one SimulatedPoint per unique FCDA ref."""
    seen: set[str] = set()
    points: dict[str, SimulatedPoint] = {}

    for ld in ied.ldevices:
        all_lns = list(ld.lns)
        if ld.ln0 is not None:
            all_lns = [ld.ln0, *all_lns]
        for ln in all_lns:
            for ds in ln.datasets:
                for fcda in ds.fcdas:
                    ref = _fcda_ref(fcda)
                    if ref in seen:
                        continue
                    seen.add(ref)
                    mms_type = _FC_MMS_TYPE.get(fcda.fc, _MMS_FLOAT)
                    points[ref] = SimulatedPoint(ref=ref, fc=fcda.fc, mms_type=mms_type)

    return points


# ---------------------------------------------------------------------------
# Model node tracker (used during IedServer model construction)
# ---------------------------------------------------------------------------


@dataclass
class _ModelNode:
    """Tracks a single node in the pyiec61850 model hierarchy."""

    handle: Any  # pyiec61850 IedDomain | LogicalNode | DataObject | DataAttribute
    children: dict[str, _ModelNode] = field(default_factory=dict)


def _get_or_create_child(
    parent_node: _ModelNode,
    child_name: str,
    *,
    create_fn: Callable[..., Any],
    **create_kwargs: Any,
) -> _ModelNode:
    """Return an existing child node or create it via ``create_fn``."""
    if child_name not in parent_node.children:
        handle = create_fn(child_name, parent_node.handle, **create_kwargs)
        parent_node.children[child_name] = _ModelNode(handle=handle)
    return parent_node.children[child_name]


# ---------------------------------------------------------------------------
# IedSimulator
# ---------------------------------------------------------------------------


class IedSimulator:
    """IED simulator backed by pyiec61850 ``IedServer``.

    Lifecycle::

        cfg = SimulatorConfig(ied_name="IED1", port=10102)
        sim = IedSimulator(cfg, scl_doc)
        sim.start()
        sim.update_da("LD0/MMXU1.PhV.phsA.mag.f[MX]", 230.0)
        ...
        sim.stop()

    The :class:`IedSimulator` can also be used as a context manager::

        with IedSimulator(cfg, scl_doc) as sim:
            sim.update_da(...)
    """

    def __init__(self, config: SimulatorConfig, scl_doc: SclDocument) -> None:
        self._config = config
        self._scl = scl_doc

        ied = _ied_for_name(scl_doc, config.ied_name)
        self._points: dict[str, SimulatedPoint] = _collect_points(ied)

        self._server: Any = None
        self._iec_model: Any = None
        self._started: bool = False
        self._write_cb: Callable[[str, Any], None] | None = None

    # ------------------------------------------------------------------
    # Introspection (available without pyiec61850)
    # ------------------------------------------------------------------

    @property
    def points(self) -> dict[str, SimulatedPoint]:
        """All registered data-attribute points keyed by reference string."""
        return dict(self._points)

    @property
    def is_running(self) -> bool:
        """``True`` while the MMS server is listening."""
        return self._started

    def set_write_callback(self, cb: Callable[[str, Any], None] | None) -> None:
        """Register a callback called when a client writes a DA value.

        ``cb(ref, value)`` receives the reference string and the decoded
        Python value.  Set to ``None`` to clear.
        """
        self._write_cb = cb

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def read_da(self, ref: str) -> float | bool | int | str | None:
        """Return the current cached value for a DA reference.

        :raises SimulatorError: if *ref* is not registered in the model.
        """
        if ref not in self._points:
            raise SimulatorError(f"Unknown DA reference: {ref!r}")
        return self._points[ref].value

    def update_da(self, ref: str, value: float | bool | int | str) -> None:
        """Set the value of a data attribute.

        If the server is running, the update is pushed into the live
        ``IedServer`` model (locked/unlocked correctly) so that connected
        clients see the new value and buffered-report subscribers receive
        an entry if the DA is in a dataset linked to an RCB.

        :raises SimulatorError: if *ref* is not registered in the model.
        """
        if ref not in self._points:
            raise SimulatorError(f"Unknown DA reference: {ref!r}")
        self._points[ref].value = value
        if self._started:
            self._push_to_server(self._points[ref])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the MMS server and begin accepting client connections.

        :raises SimulatorError: if already started, or if pyiec61850 is
            not available.
        """
        if self._started:
            raise SimulatorError("IedSimulator is already running")
        try:
            import pyiec61850 as _iec
        except ImportError as exc:
            raise SimulatorError(
                "pyiec61850 is required to run the IED simulator. "
                "Build libiec61850 with SWIG bindings and install them "
                "(see docs/internal/IEC61850_MMS_LIBRARY.md)."
            ) from exc

        self._iec_model = _iec.IedModel_create(self._config.ied_name)
        self._build_server_model(_iec)

        # Initialise registered values in the model before clients connect.
        self._server = _iec.IedServer_create(self._iec_model)
        for point in self._points.values():
            if point.value is not None and point._da_handle is not None:
                self._push_to_server(point)

        _iec.IedServer_start(self._server, self._config.port)
        self._started = True
        _log.info(
            "IED simulator %r started on port %d",
            self._config.ied_name,
            self._config.port,
        )

    def stop(self) -> None:
        """Stop the MMS server and release all libiec61850 resources.

        Safe to call even if :meth:`start` was never called or already
        stopped.
        """
        if not self._started:
            return
        try:
            import pyiec61850 as _iec

            _iec.IedServer_stop(self._server)
            _iec.IedServer_destroy(self._server)
            _iec.IedModel_destroy(self._iec_model)
        except Exception:
            _log.exception("Error stopping IED simulator %r", self._config.ied_name)
        finally:
            self._server = None
            self._iec_model = None
            self._started = False
            # Clear DA handles — they are owned by the (now destroyed) model.
            for pt in self._points.values():
                pt._da_handle = None
        _log.info("IED simulator %r stopped", self._config.ied_name)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> IedSimulator:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_server_model(self, _iec: Any) -> None:
        """Construct the pyiec61850 data-model hierarchy from collected points."""
        model_node = _ModelNode(handle=self._iec_model)

        # Tracking nodes keyed by LD name and (LD, LN) pair
        domain_nodes: dict[str, _ModelNode] = {}
        ln_nodes: dict[tuple[str, str], _ModelNode] = {}

        for point in self._points.values():
            # ref is "{ld_inst}/{ln_name}.{do_name}[.{da_parts}][{FC}]"
            ld_inst, ln_name, do_name, da_parts = _parse_ref(point.ref)

            # --- Logical Device ---
            if ld_inst not in domain_nodes:
                handle = _iec.IedDomain_create(model_node.handle, ld_inst)
                domain_nodes[ld_inst] = _ModelNode(handle=handle)

            # --- Logical Node ---
            ln_key = (ld_inst, ln_name)
            if ln_key not in ln_nodes:
                handle = _iec.LogicalNode_create(ln_name, domain_nodes[ld_inst].handle)
                ln_nodes[ln_key] = _ModelNode(handle=handle)

            # --- Data Object (always one level under LN) ---
            ln_node = ln_nodes[ln_key]
            do_node = _get_or_create_child(
                ln_node,
                do_name,
                create_fn=lambda n, p, **_kw: _iec.DataObject_create(n, p, 0),
            )

            # --- Nested DataAttributes (struct intermediates + leaf) ---
            parent_node = do_node
            parts = da_parts  # list of name segments, e.g. ["phsA", "mag", "f"]
            fc_int = _FC_INT.get(point.fc, 0)

            for i, part in enumerate(parts):
                is_leaf = i == len(parts) - 1
                if is_leaf:
                    # Leaf node — use the real MMS type.
                    if part not in parent_node.children:
                        handle = _iec.DataAttribute_create(
                            part,
                            parent_node.handle,
                            point.mms_type,
                            fc_int,
                            _TRG_DEFAULT,
                            0,
                            0,
                        )
                        child_node = _ModelNode(handle=handle)
                        parent_node.children[part] = child_node
                    point._da_handle = parent_node.children[part].handle
                else:
                    # Intermediate structural node.
                    parent_node = _get_or_create_child(
                        parent_node,
                        part,
                        create_fn=lambda n, p, _fc=fc_int, **_kw: _iec.DataAttribute_create(
                            n, p, _MMS_STRUCTURE, _fc, 0, 0, 0
                        ),
                    )

    def _push_to_server(self, point: SimulatedPoint) -> None:
        """Push a single DA value into the live IedServer model."""
        if point._da_handle is None:
            return
        try:
            import pyiec61850 as _iec
        except ImportError:
            return

        mms_val = _python_to_mms(_iec, point.value, point.mms_type)
        if mms_val is None:
            _log.warning("Cannot encode value %r for %s", point.value, point.ref)
            return

        _iec.IedServer_lockDataModel(self._server)
        try:
            _iec.IedServer_updateAttributeValue(self._server, point._da_handle, mms_val)
        finally:
            _iec.IedServer_unlockDataModel(self._server)
            _iec.MmsValue_delete(mms_val)


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _parse_ref(ref: str) -> tuple[str, str, str, list[str]]:
    """Parse a SimulatedPoint reference into ``(ld_inst, ln_name, do_name, da_parts)``.

    Example::

        >>> _parse_ref("LD0/MMXU1.PhV.phsA.mag.f[MX]")
        ("LD0", "MMXU1", "PhV", ["phsA", "mag", "f"])
    """
    # Strip trailing "[FC]" suffix
    if "[" in ref:
        ref = ref[: ref.index("[")]

    slash_idx = ref.index("/")
    ld_inst = ref[:slash_idx]
    rest = ref[slash_idx + 1 :]

    parts = rest.split(".")
    ln_name = parts[0]
    do_name = parts[1] if len(parts) > 1 else ""
    da_parts = parts[2:] if len(parts) > 2 else []

    return ld_inst, ln_name, do_name, da_parts


def _python_to_mms(_iec: Any, value: Any, mms_type: int) -> Any:
    """Create an ``MmsValue`` from a Python value; returns ``None`` on failure."""
    try:
        if mms_type == _MMS_BOOLEAN:
            return _iec.MmsValue_newBoolean(bool(value))
        if mms_type == _MMS_FLOAT:
            return _iec.MmsValue_newFloat(float(value))
        if mms_type in (_MMS_INTEGER, _MMS_UNSIGNED):
            return _iec.MmsValue_newInteger(int(value))
        if mms_type == _MMS_VISIBLE_STRING:
            return _iec.MmsValue_newVisibleString(str(value))
    except Exception:
        _log.exception("MmsValue creation failed (type=%d, value=%r)", mms_type, value)
    return None
