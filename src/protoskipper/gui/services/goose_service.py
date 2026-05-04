# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Qt-aware wrapper services for GOOSE publish/subscribe (P8.C.4 / P8.C.5).

These classes are the bridge between the UI thread and the background GOOSE
receiver / publisher threads.  They lazy-import
``protoskipper_iec61850.goose`` so that the rest of the GUI remains
importable without pyiec61850 installed.

Architecture
------------
``GooseSubscriberQt`` starts a ``GooseSubscriberService`` and delivers
frames to the UI thread by emitting ``frame_received`` (a queued-connection
safe ``Signal(object)``).

``GoosePublisherQt`` owns a ``GoosePublisherService`` and provides
slot-style methods that the publisher panel can call from the UI thread.

Thread safety
-------------
All calls into ``GooseSubscriberService`` and ``GoosePublisherService`` from
these wrappers are thread-safe — the underlying services use their own
internal locks.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, Signal

_log = logging.getLogger(__name__)


class GooseSubscriberQt(QObject):
    """Qt wrapper around :class:`~protoskipper_iec61850.goose.GooseSubscriberService`.

    Emits :attr:`frame_received` for every decoded GOOSE frame, suitable for
    connecting to a UI table model from the main thread using a
    ``QueuedConnection``.

    Emits :attr:`error_occurred` if the service fails to start.

    Parameters
    ----------
    parent:
        Optional parent QObject.
    """

    frame_received = Signal(object)  # GooseFrame
    error_occurred = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._service: Any = None

    def add_subscription(self, go_cb_ref: str) -> None:
        """Register a GOOSE subscription before calling :meth:`start`.

        Parameters
        ----------
        go_cb_ref:
            GoCB reference, e.g. ``"simpleIO/LLN0$GO$gcbAnalogValues"``.
        """
        try:
            from protoskipper_iec61850.goose import GooseSubscriberService
        except ImportError as exc:
            self.error_occurred.emit(str(exc))
            return

        if self._service is None:
            self._service = GooseSubscriberService()

        def _callback(frame: Any) -> None:
            self.frame_received.emit(frame)

        self._service.add_subscriber(go_cb_ref, _callback)

    def start(self, iface: str) -> None:
        """Start receiving GOOSE frames on *iface*.

        Parameters
        ----------
        iface:
            Network interface name (e.g. ``"eth0"``).
        """
        if self._service is None:
            self.error_occurred.emit("No subscriptions registered before start()")
            return
        try:
            self._service.start(iface)
        except Exception as exc:
            _log.exception("Failed to start GooseReceiver on %r", iface)
            self.error_occurred.emit(str(exc))

    def stop(self) -> None:
        """Stop receiving and release all C resources."""
        if self._service is not None:
            try:
                self._service.stop()
            except Exception:
                _log.debug("GooseSubscriberService.stop raised", exc_info=True)


class GoosePublisherQt(QObject):
    """Qt wrapper around :class:`~protoskipper_iec61850.goose.GoosePublisherService`.

    Parameters
    ----------
    parent:
        Optional parent QObject.
    """

    error_occurred = Signal(str)
    published = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._service: Any = None

    def configure(
        self,
        iface: str,
        go_cb_ref: str,
        dat_set_ref: str,
        *,
        app_id: int = 0x0000,
        dst_mac: bytes = bytes([0x01, 0x0C, 0xCD, 0x01, 0x00, 0x00]),
        vlan_id: int = 0,
        vlan_priority: int = 4,
        conf_rev: int = 1,
        go_id: str | None = None,
        t0_ms: int = 50,
        max_retransmit_interval_ms: int = 5_000,
        simulation: bool = False,
    ) -> None:
        """Create and open a GoosePublisherService with the given parameters.

        Closes any existing publisher before creating the new one.
        """
        try:
            from protoskipper_iec61850.goose import (
                CommParameters,
                GoosePublisherService,
            )
        except ImportError as exc:
            self.error_occurred.emit(str(exc))
            return

        if self._service is not None:
            try:
                self._service.close()
            except Exception:
                _log.debug("Closing previous GoosePublisherService raised", exc_info=True)
            self._service = None

        params = CommParameters(
            vlan_priority=vlan_priority,
            vlan_id=vlan_id,
            app_id=app_id,
            dst_mac=dst_mac,
        )
        svc = GoosePublisherService(
            params=params,
            iface=iface,
            go_cb_ref=go_cb_ref,
            dat_set_ref=dat_set_ref,
            conf_rev=conf_rev,
            go_id=go_id,
            t0_ms=t0_ms,
            max_retransmit_interval_ms=max_retransmit_interval_ms,
            simulation=simulation,
        )
        try:
            svc.open()
        except Exception as exc:
            _log.exception("Failed to open GoosePublisher on %r", iface)
            self.error_occurred.emit(str(exc))
            return
        self._service = svc

    def publish(self, values: list[Any]) -> None:
        """Publish *values* and start the retransmission burst.

        Parameters
        ----------
        values:
            Dataset values as Python native types (bool / int / float / bytes).
        """
        if self._service is None:
            self.error_occurred.emit("Publisher not configured — call configure() first")
            return
        try:
            self._service.publish(values)
            self.published.emit()
        except Exception as exc:
            _log.exception("GoosePublisher publish failed")
            self.error_occurred.emit(str(exc))

    def close(self) -> None:
        """Stop retransmissions and release the publisher."""
        if self._service is not None:
            try:
                self._service.close()
            except Exception:
                _log.debug("GoosePublisherService.close raised", exc_info=True)
            self._service = None
