# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""GUI services - the layer between the UI panels and the protocol drivers.

Panels never talk to drivers directly. They talk to :class:`SessionManager`
and listen on :class:`ApplicationState` signals. This indirection is what
keeps the UI thread responsive, makes operations cancellable, and centralises
audit logging.
"""

from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.confirm_handler import GuiConfirmHandler
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import (
    CapturedFrame,
    Direction,
    SessionId,
    new_session_id,
)
from protoskipper.gui.services.worker import DriverWorker

__all__ = [
    "ApplicationState",
    "CapturedFrame",
    "Direction",
    "DriverWorker",
    "GuiConfirmHandler",
    "SessionId",
    "SessionInfo",
    "SessionManager",
    "new_session_id",
]
