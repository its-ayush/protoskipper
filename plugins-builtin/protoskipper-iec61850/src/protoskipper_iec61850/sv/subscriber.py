# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""IEC 61850-9-2 Sampled Values subscriber service — P8.D.6 (subscriber side).

This module wraps pyiec61850's ``SVReceiver`` / ``SVSubscriber`` C API to
deliver decoded :class:`~protoskipper_iec61850.sv.decoder.SvAsdu` objects to
registered Python callbacks.

When pyiec61850 is not installed the module falls back to a pure-Python
raw-socket subscriber (Linux-only, requires CAP_NET_RAW or root) so that the
scope panel can still receive SV frames captured on the local NIC.

Lifecycle
---------
1. Construct :class:`SvSubscriberService`.
2. Register at least one callback with :meth:`add_subscriber`.
3. Call :meth:`start` with the NIC name.
4. Callbacks fire on the receiver's internal thread — they must be
   brief and thread-safe.
5. Call :meth:`stop` to release resources.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from protoskipper_iec61850.sv.decoder import ETHERTYPE_SV, SvAsdu, SvFrame, decode_frame

_log = logging.getLogger(__name__)

SvCallback = Callable[[SvFrame], None]

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class SvSubscription:
    """Describes one SV subscription (by SVID or wildcard).

    Attributes
    ----------
    sv_id:
        The SVID to match, or ``""`` to receive all SV streams on the
        interface.
    callback:
        Callable receiving an :class:`SvFrame` for each matched frame.
    """

    sv_id: str
    callback: SvCallback


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SvSubscriberService:
    """Manage SV subscriptions on a single network interface.

    Thread safety
    -------------
    :meth:`add_subscriber` / :meth:`remove_subscriber` may be called from any
    thread.  The internal lock serialises modifications to the subscription list
    before and after :meth:`start`.
    """

    def __init__(self) -> None:
        self._subscriptions: list[SvSubscription] = []
        self._lock = threading.Lock()
        self._running = False
        self._interface: str | None = None
        self._stop_event = threading.Event()
        self._receiver_thread: threading.Thread | None = None
        self._use_pyiec = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_subscriber(self, sv_id: str, callback: SvCallback) -> None:
        """Register a callback for a given SVID (or ``""`` for all)."""
        with self._lock:
            self._subscriptions.append(SvSubscription(sv_id=sv_id, callback=callback))

    def remove_subscriber(self, sv_id: str, callback: SvCallback) -> None:
        """Unregister a previously-added callback."""
        with self._lock:
            self._subscriptions = [
                s for s in self._subscriptions if not (s.sv_id == sv_id and s.callback is callback)
            ]

    def start(self, interface: str) -> None:
        """Start receiving SV frames on *interface*.

        First tries pyiec61850's ``SVReceiver``; falls back to the raw-socket
        path on Linux if pyiec61850 is unavailable or the receiver fails to
        bind.
        """
        if self._running:
            raise RuntimeError("SvSubscriberService already running")
        self._interface = interface
        self._stop_event.clear()

        if self._try_start_pyiec(interface):
            self._use_pyiec = True
            self._running = True
            _log.info("SV subscriber started on %s via pyiec61850", interface)
        else:
            _log.info(
                "pyiec61850 SVReceiver unavailable; falling back to raw socket on %s",
                interface,
            )
            self._start_raw_socket(interface)
            self._running = True

    def stop(self) -> None:
        """Stop receiving and free all resources."""
        if not self._running:
            return
        self._stop_event.set()

        if self._use_pyiec:
            self._stop_pyiec()
        else:
            if self._receiver_thread is not None:
                self._receiver_thread.join(timeout=5.0)
                self._receiver_thread = None

        self._running = False
        _log.info("SV subscriber stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # pyiec61850 path
    # ------------------------------------------------------------------

    def _try_start_pyiec(self, interface: str) -> bool:
        """Return True if pyiec61850 SVReceiver is available and starts OK."""
        try:
            import pyiec61850 as _iec
        except ImportError:
            return False

        try:
            receiver = _iec.SVReceiver_create()
            _iec.SVReceiver_setInterfaceId(receiver, interface)

            # We need one subscriber per registered SVID (or one wildcard).
            with self._lock:
                subs = list(self._subscriptions)

            if not subs:
                _log.debug("No SV subscriptions registered — using wildcard")
                subs_to_add = [("", None)]
            else:
                subs_to_add = [(s.sv_id, s) for s in subs]

            subscriber_handles = []
            for sv_id, sub in subs_to_add:
                svid_c = sv_id.encode("ascii") if sv_id else None
                iec_sub = _iec.SVSubscriber_create(svid_c, 0x4001)

                def _listener(sv_sub, values, pv, *, _sub=sub, _svc=self):
                    _svc._on_pyiec_frame(sv_sub, values, _sub)

                _iec.SVSubscriber_setListener(iec_sub, _listener, None)
                _iec.SVReceiver_addSubscriber(receiver, iec_sub)
                subscriber_handles.append(iec_sub)

            _iec.SVReceiver_start(receiver)
            self._pyiec_receiver = receiver
            self._pyiec_subscribers = subscriber_handles
            self._pyiec_iec = _iec
            return True
        except Exception:
            _log.debug("SVReceiver start failed", exc_info=True)
            return False

    def _on_pyiec_frame(self, sv_sub: object, values: object, sub: SvSubscription | None) -> None:
        """Callback from pyiec61850 SVReceiver — convert to SvFrame and dispatch."""
        try:
            _iec = self._pyiec_iec
            sv_id = _iec.SVSubscriber_getCurrentSVID(sv_sub) or ""
            smp_cnt = _iec.SVSubscriber_getCurrentSmpCnt(sv_sub)
            conf_rev = _iec.SVSubscriber_getCurrentConfRev(sv_sub)
            smp_synch = _iec.SVSubscriber_getCurrentSmpSynch(sv_sub)

            n_channels = _iec.SVSubscriber_getNumberOfDataSetEntries(sv_sub)
            channels = []
            for i in range(n_channels // 2):  # each logical channel = value + quality
                val = _iec.SVSubscriber_getINT32(values, i * 2) if values else 0
                qual = _iec.SVSubscriber_getINT32U(values, i * 2 + 1) if values else 0
                from protoskipper_iec61850.sv.decoder import SvChannel

                channels.append(SvChannel(index=i, value_raw=val, quality_raw=qual))

            asdu = SvAsdu(
                sv_id=sv_id,
                smp_cnt=smp_cnt,
                conf_rev=conf_rev,
                smp_synch=smp_synch,
                smp_rate=None,
                smp_mod=None,
                refr_tm_ms=None,
                channels=channels,
            )
            frame = SvFrame(
                src_mac=b"\x00" * 6,
                dst_mac=b"\x01\x0c\xcd\x04\x00\x00",
                vlan_id=None,
                vlan_priority=None,
                app_id=0x4001,
                no_asdu=1,
                asdus=[asdu],
                raw_apdu=b"",
            )
            self._dispatch(frame, sub)
        except Exception:
            _log.debug("Error in pyiec61850 SV frame handler", exc_info=True)

    def _stop_pyiec(self) -> None:
        try:
            self._pyiec_iec.SVReceiver_stop(self._pyiec_receiver)
            self._pyiec_iec.SVReceiver_destroy(self._pyiec_receiver)
        except Exception:
            _log.debug("Error stopping pyiec61850 SVReceiver", exc_info=True)

    # ------------------------------------------------------------------
    # Raw-socket fallback (Linux only)
    # ------------------------------------------------------------------

    def _start_raw_socket(self, interface: str) -> None:
        self._receiver_thread = threading.Thread(
            target=self._raw_socket_loop,
            args=(interface,),
            name=f"sv-raw-{interface}",
            daemon=True,
        )
        self._receiver_thread.start()

    def _raw_socket_loop(self, interface: str) -> None:
        import socket

        try:
            # AF_PACKET / SOCK_RAW — Linux only; requires CAP_NET_RAW
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE_SV_NET))  # type: ignore[attr-defined]
            sock.bind((interface, 0))
            sock.settimeout(0.5)
        except (AttributeError, OSError) as exc:
            _log.error("Cannot open raw socket on %s: %s", interface, exc)
            return

        try:
            while not self._stop_event.is_set():
                try:
                    data = sock.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    break
                frame = decode_frame(data)
                if frame is not None:
                    self._dispatch(frame, None)
        finally:
            sock.close()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, frame: SvFrame, bound_sub: SvSubscription | None) -> None:
        """Route a decoded SvFrame to all matching callbacks."""
        with self._lock:
            subs = list(self._subscriptions)

        sv_id = frame.asdus[0].sv_id if frame.asdus else ""
        for sub in subs:
            if sub.sv_id == "" or sub.sv_id == sv_id:
                try:
                    sub.callback(frame)
                except Exception:
                    _log.debug("SV callback raised", exc_info=True)


# Re-export for consumers who need only the socket-filter constant.
ETHERTYPE_SV_NET = ETHERTYPE_SV
