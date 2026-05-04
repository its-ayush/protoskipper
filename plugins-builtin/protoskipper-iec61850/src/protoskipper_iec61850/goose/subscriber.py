# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""GOOSE subscriber service — P8.C.1 / P8.C.2.

Architecture
------------
``GooseSubscriberService`` wraps pyiec61850's ``GooseReceiver`` +
``GooseSubscriber`` C objects.  Each subscription is created with
``GooseSubscriber_create(go_cb_ref, None)`` and receives frames via a
Python listener closure registered with ``GooseSubscriber_setListener``.

The ``GooseReceiver`` runs its own background OS thread internally after
``GooseReceiver_start`` is called — no additional threading is required
here.

Lifecycle
---------
1. Construct :class:`GooseSubscriberService`.
2. Call :meth:`add_subscriber` for each GOOSE control block reference.
3. Call :meth:`start` with the network interface name (e.g. ``"eth0"``).
4. Frames arrive via the registered callbacks on the receiver's internal
   thread.  The callback must be thread-safe.
5. Call :meth:`stop` to halt reception and free all resources.

Design note — MmsValue decoding
--------------------------------
``GooseSubscriber_getDataSetValues`` returns the most recent dataset as a
pyiec61850 ``MmsValue*`` (type ``MMS_ARRAY``).  We iterate its elements
with ``MmsValue_getElement`` and decode each via the shared
``_mms_value_to_python`` helper from :mod:`protoskipper_iec61850._mms_client`.
The returned ``MmsValue`` is owned by the subscriber; **do not delete it**.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

GooseCallback = Callable[["GooseFrame"], None]


@dataclass
class GooseFrame:
    """A single decoded GOOSE message received from the network.

    Attributes
    ----------
    go_cb_ref:
        GoCB reference string that the message was published under,
        e.g. ``"simpleIO/LLN0$GO$gcbAnalogValues"``.
    go_id:
        Human-readable GOOSE ID string.
    dat_set:
        Dataset reference, e.g. ``"simpleIO/LLN0$GOOSE1"``.
    t_ms:
        GOOSE timestamp in milliseconds since the Unix epoch.
    st_num:
        State number — incremented on a state change.
    sq_num:
        Sequence number — incremented on every retransmission within a burst.
    conf_rev:
        Configuration revision of the GoCB.
    simulation:
        ``True`` when the simulation bit is set in the APDU.
    nds_comm:
        ``True`` when the *needsCommissioning* bit is set.
    all_data:
        Decoded dataset elements as Python native values (bool / int / float /
        bytes / list).  Maps 1-to-1 to the dataset members in declaration
        order.
    """

    go_cb_ref: str
    go_id: str
    dat_set: str
    t_ms: int
    st_num: int
    sq_num: int
    conf_rev: int
    simulation: bool
    nds_comm: bool
    all_data: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# GooseSubscriberService
# ---------------------------------------------------------------------------


class GooseSubscriberService:
    """Manages GOOSE subscriptions using pyiec61850's native receiver.

    This class lazy-imports pyiec61850 so that the rest of the plugin
    package remains importable without pyiec61850 installed.  The first
    call to :meth:`start` triggers the import.

    Thread safety
    -------------
    :meth:`add_subscriber` must be called **before** :meth:`start`.
    Modifying subscriptions while the receiver is running is not supported.
    """

    def __init__(self) -> None:
        # Pending subscriptions: go_cb_ref → callback (registered before start)
        self._pending: list[tuple[str, GooseCallback]] = []
        # Active C objects (valid between start and stop)
        self._receiver: Any = None
        self._subscriber_handles: list[Any] = []
        self._running = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Configuration API (call before start)
    # ------------------------------------------------------------------

    def add_subscriber(self, go_cb_ref: str, callback: GooseCallback) -> None:
        """Register a GOOSE subscription for *go_cb_ref*.

        Parameters
        ----------
        go_cb_ref:
            GoCB reference in the dotted IEC 61850 format with ``$`` path
            separators as used in GOOSE headers, e.g.
            ``"simpleIO/LLN0$GO$gcbAnalogValues"``.
        callback:
            Called on the receiver's internal thread for each frame whose
            ``GoCBRef`` field matches *go_cb_ref*.  Must be thread-safe.

        Raises
        ------
        RuntimeError
            If :meth:`start` has already been called.
        """
        with self._lock:
            if self._running:
                raise RuntimeError("Cannot add subscribers while the GOOSE receiver is running")
            self._pending.append((go_cb_ref, callback))

    # ------------------------------------------------------------------
    # Lifecycle API
    # ------------------------------------------------------------------

    def start(self, iface: str) -> None:
        """Start receiving GOOSE frames on *iface*.

        Parameters
        ----------
        iface:
            Network interface name (e.g. ``"eth0"``).  Must be a Layer-2
            capable interface (not a loopback).

        Raises
        ------
        ImportError
            If pyiec61850 is not installed.
        RuntimeError
            If the service is already running.
        """
        with self._lock:
            if self._running:
                raise RuntimeError("GooseSubscriberService is already running")

            try:
                import pyiec61850 as _lib
            except ImportError as exc:
                raise ImportError(
                    "pyiec61850 is required for GOOSE support.  "
                    "Build and install it from https://github.com/mz-automation/libiec61850"
                ) from exc

            from protoskipper_iec61850._mms_client import _mms_value_to_python

            receiver = _lib.GooseReceiver_create()
            _lib.GooseReceiver_setInterfaceId(receiver, iface)

            handles: list[Any] = []
            for go_cb_ref, callback in self._pending:
                sub = _lib.GooseSubscriber_create(go_cb_ref, None)

                # Capture variables for the listener closure.
                _go_cb_ref = go_cb_ref
                _callback = callback
                _sub = sub

                def _listener(
                    subscriber: Any,
                    _param: Any,
                    *,
                    _lib: Any = _lib,
                    _go_cb_ref: str = _go_cb_ref,
                    _callback: GooseCallback = _callback,
                    _decode: Any = _mms_value_to_python,
                ) -> None:
                    try:
                        ds = _lib.GooseSubscriber_getDataSetValues(subscriber)
                        all_data: list[Any] = []
                        if ds is not None:
                            size = _lib.MmsValue_getArraySize(ds)
                            for i in range(size):
                                elem = _lib.MmsValue_getElement(ds, i)
                                all_data.append(_decode(_lib, elem))
                        frame = GooseFrame(
                            go_cb_ref=_go_cb_ref,
                            go_id=_lib.GooseSubscriber_getGoId(subscriber) or "",
                            dat_set=_lib.GooseSubscriber_getDataSetRef(subscriber) or "",
                            t_ms=_lib.GooseSubscriber_getTimestamp(subscriber),
                            st_num=_lib.GooseSubscriber_getStNum(subscriber),
                            sq_num=_lib.GooseSubscriber_getSqNum(subscriber),
                            conf_rev=_lib.GooseSubscriber_getConfRev(subscriber),
                            simulation=bool(_lib.GooseSubscriber_isTest(subscriber)),
                            nds_comm=bool(_lib.GooseSubscriber_needsCommission(subscriber)),
                            all_data=all_data,
                        )
                        _callback(frame)
                    except Exception:
                        _log.exception("Error in GOOSE listener for %r", _go_cb_ref)

                _lib.GooseSubscriber_setListener(sub, _listener, None)
                _lib.GooseReceiver_addSubscriber(receiver, sub)
                handles.append(sub)

            _lib.GooseReceiver_start(receiver)

            self._receiver = receiver
            self._subscriber_handles = handles
            self._running = True
            _log.info(
                "GooseReceiver started on interface %r (%d subscriptions)",
                iface,
                len(handles),
            )

    def stop(self) -> None:
        """Stop the receiver and release all C resources."""
        with self._lock:
            if not self._running:
                return
            try:
                import pyiec61850 as _lib
            except ImportError:
                return

            try:
                _lib.GooseReceiver_stop(self._receiver)
            except Exception:
                _log.debug("GooseReceiver_stop raised", exc_info=True)
            try:
                _lib.GooseReceiver_destroy(self._receiver)
            except Exception:
                _log.debug("GooseReceiver_destroy raised", exc_info=True)
            for sub in self._subscriber_handles:
                try:
                    _lib.GooseSubscriber_destroy(sub)
                except Exception:
                    _log.debug("GooseSubscriber_destroy raised", exc_info=True)

            self._receiver = None
            self._subscriber_handles = []
            self._running = False
            _log.info("GooseReceiver stopped")

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> GooseSubscriberService:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
