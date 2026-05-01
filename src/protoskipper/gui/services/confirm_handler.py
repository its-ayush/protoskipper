# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""GuiConfirmHandler - cross-thread bridge for write confirmation dialogs.

Drivers run on worker threads; confirmation dialogs must show on the UI
thread. The worker thread blocks waiting for a Boolean answer. We
implement this with a Qt signal that traverses the thread boundary plus a
:class:`threading.Event` the worker waits on.

This is the only place in the GUI where we deliberately block a non-UI
thread on UI input. The block is bounded by a configurable timeout.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

from protoskipper.core.driver import SessionProfile, WriteIntent

_logger = logging.getLogger(__name__)

# Maximum time the worker thread blocks waiting for an operator's answer.
# Past this, the request is treated as a denial. Field operators do not get
# distracted for five minutes mid-write without a deliberate decision; if
# they do, the safe default is to abort.
DEFAULT_CONFIRM_TIMEOUT_S = 300.0


class GuiConfirmHandler(QObject):
    """Lives on the UI thread. Owns the WriteDialog flow.

    A worker calls :meth:`request_confirm` from its thread. The signal
    fires across the thread boundary (Qt::QueuedConnection by default),
    delivering the request to the UI thread. The UI thread runs the
    dialog and posts the answer back via the supplied callable, which
    sets a threading.Event the worker is waiting on.
    """

    # The signal carries the intent, profile, and a thread-safe callback
    # that the UI handler invokes to deliver the answer.
    confirm_requested = Signal(WriteIntent, object, object)
    # Args: intent, profile, callback(bool) -> None

    def __init__(
        self,
        dialog_factory: Callable[[WriteIntent, SessionProfile], Any],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._dialog_factory = dialog_factory
        self.confirm_requested.connect(self._handle_request, Qt.QueuedConnection)

    @Slot(WriteIntent, object, object)
    def _handle_request(
        self,
        intent: WriteIntent,
        profile: SessionProfile,
        callback: Callable[[bool], None],
    ) -> None:
        """Show the WriteDialog and report the answer back to the worker."""
        try:
            dialog = self._dialog_factory(intent, profile)
            answer = bool(dialog.exec())
        except Exception:
            _logger.exception("Write confirmation dialog raised; treating as denial.")
            answer = False
        callback(answer)

    def make_callback(
        self,
        timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
    ) -> Callable[[WriteIntent, SessionProfile], bool]:
        """Return a confirm-callback suitable for :class:`SafetyContext`.

        The returned callable runs on the worker thread, emits the request
        signal, and blocks on a threading.Event until the UI thread answers
        or the timeout elapses.
        """
        # Capture self by reference so the callback survives even if the
        # caller stores it.
        emit = self.confirm_requested.emit

        def _callback(intent: WriteIntent, profile: SessionProfile) -> bool:
            event = threading.Event()
            answer_box: list[bool] = []

            def deliver(answer: bool) -> None:
                answer_box.append(answer)
                event.set()

            emit(intent, profile, deliver)
            if not event.wait(timeout=timeout_s):
                _logger.warning(
                    "Confirmation timed out after %.1fs for write to %s; treating as denial.",
                    timeout_s,
                    intent.object_ref.object_id,
                )
                return False
            return answer_box[0] if answer_box else False

        return _callback
