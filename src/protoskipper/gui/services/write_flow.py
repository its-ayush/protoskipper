# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""WriteFlowController — the two-phase prepare / commit write flow.

This module encapsulates all signal wiring required to drive a write from
the GUI's perspective: open :class:`WriteDialog`, dispatch
``prepare_write``, receive the :class:`WriteIntent` back from the worker,
allow the operator to review it, and then dispatch ``commit_write``.

The controller is instantiated once per :class:`MainWindow` and its
:meth:`start` method is called whenever any panel emits ``write_requested``.
This replaces the ~50-line ``_open_write_dialog`` method that used to live
inside ``MainWindow``.

Design notes
------------
* The two ``connect``/``disconnect`` flips exist because Qt signals
  are delivered to *all* connected slots regardless of session identity, so
  we need to filter in the lambda.  We disconnect after delivery to avoid
  spurious callbacks from a later write-flow invocation.
* ``contextlib.suppress`` silences the runtime ``TypeError`` Qt raises when
  you attempt to disconnect a slot that was never connected (guard for the
  error path where the dialog was rejected before prepare completed).
* The safety confirmation dialog is NOT opened here — that runs inside
  ``commit_write → GuiConfirmHandler`` on the worker thread, which then
  posts a queued signal back to the UI thread.  Only one thread ever blocks
  on a dialog at a time.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from PySide6.QtWidgets import QDialog, QWidget

from protoskipper.core.driver import ObjectRef, WriteIntent
from protoskipper.gui.dialogs.write_dialog import WriteDialog
from protoskipper.gui.services.types import SessionId

if TYPE_CHECKING:
    from protoskipper.gui.services.app_state import ApplicationState
    from protoskipper.gui.services.session_manager import SessionManager

_logger = logging.getLogger(__name__)


class WriteFlowController:
    """Orchestrates the GUI-side two-phase Prepare → Commit write flow.

    One instance lives on ``MainWindow``; it is NOT a ``QObject``.
    """

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget,
    ) -> None:
        self._state = state
        self._session_manager = session_manager
        self._parent = parent

    def start(self, session_id: str, ref: ObjectRef) -> None:
        """Open the write dialog for *ref* in *session_id* and drive the flow."""
        sid = SessionId(session_id)
        info = self._state.session(sid)
        if info is None or not info.is_open:
            _logger.debug("WriteFlowController.start: session %s not open; ignored", session_id)
            return

        last = info.last_values.get(ref.object_id)
        dialog = WriteDialog(ref, info.profile, last_known=last, parent=self._parent)

        # ---- slot 1: receive the prepared intent from the worker --------
        def on_intent_prepared(returned_sid: str, intent: WriteIntent) -> None:
            if returned_sid != session_id:
                return
            if intent.object_ref.object_id != ref.object_id:
                return
            dialog.set_intent(intent)
            with contextlib.suppress(TypeError, RuntimeError):
                self._state.write_intent_prepared.disconnect(on_intent_prepared)

        # ---- slot 2: surface prepare errors in the dialog ---------------
        def on_error(operation: str, message: str) -> None:
            if operation != "prepare_write":
                return
            dialog.report_prepare_failed(message)

        self._state.write_intent_prepared.connect(on_intent_prepared)
        self._state.error_raised.connect(on_error)

        # ---- hook the dialog's Prepare/Next button -----------------------
        original_prepare: Any = dialog._on_prepare_clicked

        def prepare_dispatched() -> None:
            original_prepare()
            value = dialog.typed_value()
            if value is None:
                # Validation failed; the dialog already showed the error.
                return
            self._session_manager.prepare_write(sid, ref, value)

        dialog._next.clicked.disconnect(original_prepare)
        dialog._next.clicked.connect(prepare_dispatched)

        # ---- run the dialog modally -------------------------------------
        try:
            accepted = dialog.exec() == QDialog.Accepted
        finally:
            with contextlib.suppress(TypeError, RuntimeError):
                self._state.write_intent_prepared.disconnect(on_intent_prepared)
            with contextlib.suppress(TypeError, RuntimeError):
                self._state.error_raised.disconnect(on_error)

        # ---- dispatch commit if the operator approved -------------------
        if accepted and dialog.intent() is not None:
            # Safety confirmation runs inside commit_write via
            # GuiConfirmHandler — no further dialog is opened here.
            self._session_manager.commit_write(sid, dialog.intent())
