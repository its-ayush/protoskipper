# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""GOOSE publisher service — P8.C.3.

Architecture
------------
``GoosePublisherService`` wraps pyiec61850's ``GoosePublisher`` C object and
implements the IEC 61850-8-1 retransmission burst schedule:

After every state change (``stNum`` increment):

1. Transmit immediately at ``sqNum = 0``.
2. Transmit after ``T0`` ms at ``sqNum = 1``.
3. Transmit after ``T0`` ms at ``sqNum = 2``.
4. Transmit after ``T1 = 2 * T0`` ms at ``sqNum = 3``.
5. Transmit after ``T2 = 4 * T0`` ms at ``sqNum = 4``.
6. Continue at ``max_retransmit_interval_ms`` until the next state change.

All retransmissions after step 5 use the interval defined by
``max_retransmit_interval_ms``.  Scheduling is implemented with
:class:`threading.Timer`.

MmsValue encoding
-----------------
:meth:`publish` accepts a ``list`` of Python native values:

* ``bool`` → ``MmsValue_newBoolean``
* ``int`` → ``MmsValue_newIntegerFromInt32``
* ``float`` → ``MmsValue_newFloat``
* ``bytes`` → ``MmsValue_newOctetString`` (raw bytes)

The caller is responsible for ordering the list to match the dataset
member declaration order in the IED's CID/SCD file.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class CommParameters:
    """Layer-2 communication parameters for a GOOSE publisher.

    Maps 1-to-1 to libiec61850's ``CommParameters`` struct.

    Attributes
    ----------
    vlan_priority:
        802.1Q VLAN priority (0-7).  Default 4 per IEC 61850-8-1.
    vlan_id:
        802.1Q VLAN ID (0-4094).  0 means no VLAN tag.
    app_id:
        GOOSE APPID (0x0000 - 0x3FFF).
    dst_mac:
        Destination MAC address as 6 bytes.  Default is the IEC 61850
        multicast address ``01:0C:CD:01:00:00``.
    """

    vlan_priority: int = 4
    vlan_id: int = 0
    app_id: int = 0x0000
    dst_mac: bytes = field(default_factory=lambda: bytes([0x01, 0x0C, 0xCD, 0x01, 0x00, 0x00]))


# Retransmission burst schedule (in T0 multiples)
_BURST_DELAYS_T0: tuple[int, ...] = (0, 1, 1, 2, 4)


# ---------------------------------------------------------------------------
# Helper: encode Python native value → MmsValue
# ---------------------------------------------------------------------------


def _encode_value(lib: Any, value: Any) -> Any:
    """Encode a Python native value to a new ``MmsValue``.

    Caller must ``MmsValue_delete`` the returned value when done.

    Raises
    ------
    TypeError
        If *value* is not bool, int, float, or bytes.
    """
    if isinstance(value, bool):
        return lib.MmsValue_newBoolean(value)
    if isinstance(value, int):
        return lib.MmsValue_newIntegerFromInt32(value)
    if isinstance(value, float):
        return lib.MmsValue_newFloat(value)
    if isinstance(value, bytes):
        mv = lib.MmsValue_newOctetString(0, len(value))
        lib.MmsValue_setOctetString(mv, value, len(value))
        return mv
    raise TypeError(
        f"Cannot encode {type(value).__name__!r} as MmsValue; "
        "supported types: bool, int, float, bytes"
    )


# ---------------------------------------------------------------------------
# GoosePublisherService
# ---------------------------------------------------------------------------


class GoosePublisherService:
    """Wraps pyiec61850's GoosePublisher with burst retransmission scheduling.

    This class lazy-imports pyiec61850 so that the rest of the plugin
    package remains importable without pyiec61850 installed.

    Parameters
    ----------
    params:
        Layer-2 communication parameters (VLAN, AppID, destination MAC).
    iface:
        Network interface name (e.g. ``"eth0"``).
    go_cb_ref:
        GoCB reference string, e.g.
        ``"simpleIO/LLN0$GO$gcbAnalogValues"``.
    dat_set_ref:
        Dataset reference, e.g. ``"simpleIO/LLN0$GOOSE1"``.
    conf_rev:
        Configuration revision of the GoCB (default 1).
    go_id:
        Optional GOOSE ID string.  If ``None``, the *go_cb_ref* is used.
    t0_ms:
        Base retransmission interval in milliseconds (default 50 ms).
    max_retransmit_interval_ms:
        Maximum retransmission interval in milliseconds (default 5000 ms).
    simulation:
        Set the simulation bit in every APDU (default ``False``).
    """

    def __init__(
        self,
        params: CommParameters,
        iface: str,
        go_cb_ref: str,
        dat_set_ref: str,
        conf_rev: int = 1,
        go_id: str | None = None,
        t0_ms: int = 50,
        max_retransmit_interval_ms: int = 5_000,
        simulation: bool = False,
    ) -> None:
        self._params = params
        self._iface = iface
        self._go_cb_ref = go_cb_ref
        self._dat_set_ref = dat_set_ref
        self._conf_rev = conf_rev
        self._go_id = go_id if go_id is not None else go_cb_ref
        self._t0_ms = t0_ms
        self._max_interval_ms = max_retransmit_interval_ms
        self._simulation = simulation

        self._publisher: Any = None
        self._lib: Any = None
        self._lock = threading.Lock()
        self._retransmit_timer: threading.Timer | None = None
        self._last_values: list[Any] = []
        self._burst_step: int = 0  # index into _BURST_DELAYS_T0 / steady state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Create and configure the pyiec61850 GoosePublisher.

        Raises
        ------
        ImportError
            If pyiec61850 is not installed.
        RuntimeError
            If :meth:`open` has already been called.
        """
        with self._lock:
            if self._publisher is not None:
                raise RuntimeError("GoosePublisherService is already open")

            try:
                import pyiec61850 as _lib
            except ImportError as exc:
                raise ImportError(
                    "pyiec61850 is required for GOOSE support.  "
                    "Build and install it from https://github.com/mz-automation/libiec61850"
                ) from exc

            self._lib = _lib
            comm = _lib.CommParameters()
            comm.vlanPriority = self._params.vlan_priority
            comm.vlanId = self._params.vlan_id
            comm.appId = self._params.app_id
            mac = self._params.dst_mac
            for i, b in enumerate(mac[:6]):
                comm.dstAddress[i] = b

            pub = _lib.GoosePublisher_create(comm, self._iface)
            _lib.GoosePublisher_setGoCbRef(pub, self._go_cb_ref)
            _lib.GoosePublisher_setDataSetRef(pub, self._dat_set_ref)
            _lib.GoosePublisher_setConfRev(pub, self._conf_rev)
            _lib.GoosePublisher_setGoID(pub, self._go_id)
            _lib.GoosePublisher_setSimulation(pub, self._simulation)
            _lib.GoosePublisher_setTimeAllowedToLive(pub, self._max_interval_ms * 2)
            self._publisher = pub
            _log.info("GoosePublisher opened on %r for GoCB %r", self._iface, self._go_cb_ref)

    def close(self) -> None:
        """Stop retransmissions and release the GoosePublisher."""
        with self._lock:
            self._cancel_timer_locked()
            if self._publisher is not None and self._lib is not None:
                try:
                    self._lib.GoosePublisher_destroy(self._publisher)
                except Exception:
                    _log.debug("GoosePublisher_destroy raised", exc_info=True)
                self._publisher = None
                self._lib = None
            _log.info("GoosePublisher closed for GoCB %r", self._go_cb_ref)

    # ------------------------------------------------------------------
    # Publishing API
    # ------------------------------------------------------------------

    def publish(self, values: list[Any]) -> None:
        """Publish a new state and start the retransmission burst.

        Increments ``stNum``, resets ``sqNum`` to 0, transmits immediately,
        then schedules the retransmission burst defined in IEC 61850-8-1.

        Parameters
        ----------
        values:
            Dataset values as Python native types (bool / int / float /
            bytes).  Must match the dataset member declaration order.

        Raises
        ------
        RuntimeError
            If :meth:`open` has not been called.
        TypeError
            If any element of *values* cannot be encoded as an MmsValue.
        """
        with self._lock:
            if self._publisher is None:
                raise RuntimeError("GoosePublisherService is not open — call open() first")
            self._cancel_timer_locked()
            self._last_values = list(values)
            self._burst_step = 0
            lib = self._lib
            lib.GoosePublisher_increaseStNum(self._publisher)
            self._transmit_locked(values)
            self._schedule_next_locked()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_dataset(self, values: list[Any]) -> Any:
        """Encode *values* into a new ``LinkedList`` of MmsValues.

        Returns a ``LinkedList*`` suitable for ``GoosePublisher_publish``.
        The caller must free it with ``LinkedList_destroy`` after publishing.
        """
        lib = self._lib
        ll = lib.LinkedList_create()
        for v in values:
            mv = _encode_value(lib, v)
            lib.LinkedList_add(ll, mv)
        return ll

    def _transmit_locked(self, values: list[Any]) -> None:
        """Transmit one GOOSE APDU (called with self._lock held)."""
        lib = self._lib
        ll = self._build_dataset(values)
        try:
            lib.GoosePublisher_publish(self._publisher, ll)
        finally:
            lib.LinkedList_destroyDeep(ll, lib.MmsValue_delete)

    def _schedule_next_locked(self) -> None:
        """Schedule the next retransmission (called with self._lock held)."""
        step = self._burst_step
        if step < len(_BURST_DELAYS_T0):
            delay_ms = _BURST_DELAYS_T0[step] * self._t0_ms
            self._burst_step += 1
        else:
            delay_ms = self._max_interval_ms
        # step 0 was the immediate transmit — delay for step 1+ is the gap between sends
        if delay_ms == 0:
            # Already transmitted for step 0; immediately schedule step 1
            if self._burst_step < len(_BURST_DELAYS_T0) + 1:
                step2 = self._burst_step
                delay_ms = (
                    _BURST_DELAYS_T0[step2] * self._t0_ms
                    if step2 < len(_BURST_DELAYS_T0)
                    else self._max_interval_ms
                )
                self._burst_step += 1
            else:
                delay_ms = self._max_interval_ms

        self._retransmit_timer = threading.Timer(delay_ms / 1000.0, self._retransmit)
        self._retransmit_timer.daemon = True
        self._retransmit_timer.start()

    def _retransmit(self) -> None:
        """Retransmit callback (runs on a timer thread)."""
        with self._lock:
            if self._publisher is None:
                return  # publisher was closed
            try:
                self._transmit_locked(self._last_values)
            except Exception:
                _log.exception("GOOSE retransmit failed for %r", self._go_cb_ref)
            self._schedule_next_locked()

    def _cancel_timer_locked(self) -> None:
        """Cancel any pending retransmit timer (called with self._lock held)."""
        if self._retransmit_timer is not None:
            self._retransmit_timer.cancel()
            self._retransmit_timer = None
        self._burst_step = 0

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> GoosePublisherService:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Safe literal parser (shared with the publisher GUI panel)
# ---------------------------------------------------------------------------


def parse_dataset_literal(text: str) -> bool | int | float | bytes:
    """Parse *text* as a bool, int, float, or bytes literal.

    Used by the GOOSE publisher panel to interpret user-entered dataset
    values.  Only these four types are accepted; arbitrary Python
    expressions are never evaluated.

    Raises
    ------
    ValueError
        If *text* is not a recognised literal.
    """
    import ast  # stdlib, always available

    if text in ("True", "true"):
        return True
    if text in ("False", "false"):
        return False
    try:
        val = ast.literal_eval(text)
    except (ValueError, SyntaxError) as exc:
        raise ValueError(f"Not a valid literal: {text!r}") from exc
    if not isinstance(val, (int, float, bytes)):
        raise ValueError(
            f"Unsupported type {type(val).__name__!r}; only bool, int, float, and bytes are allowed"
        )
    return val
