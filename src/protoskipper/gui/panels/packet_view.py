# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""PacketViewPanel - chronological list of captured frames.

Subscribes to :attr:`ApplicationState.frame_captured` via
:class:`PacketLogModel`. Filtering by the selected session keeps the
focus tight when many sessions are open simultaneously.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from protoskipper.gui.models.packet_log_model import PacketLogModel
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.types import CapturedFrame, SessionId


class PacketViewPanel(QWidget):
    """Chronological table of captured TX/RX frames."""

    def __init__(self, state: ApplicationState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._session_id: SessionId | None = None

        self._model = PacketLogModel(state, parent=self)
        self._view = QTableView(self)
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._view.horizontalHeader().setStretchLastSection(True)
        self._view.setAlternatingRowColors(True)
        self._view.verticalHeader().setVisible(False)

        self._toolbar = QToolBar(self)
        self._clear_button = QPushButton("Clear", self)
        self._filter_check = QCheckBox("Filter to selected session", self)
        self._filter_check.setChecked(True)
        # P2.A.2: Replay mode banner shown when a pcapng file is loaded.
        self._replay_label = QLabel("⏪ Replay mode — read only", self)
        self._replay_label.setStyleSheet("color: #b45309; font-weight: bold; padding: 0 8px;")
        self._replay_label.setVisible(False)
        self._toolbar.addWidget(self._clear_button)
        self._toolbar.addWidget(self._filter_check)
        self._toolbar.addWidget(self._replay_label)

        self._clear_button.clicked.connect(self._model.clear)
        self._filter_check.toggled.connect(self._on_filter_toggled)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._view)

    def set_session(self, session_id: str | None) -> None:
        sid = SessionId(session_id) if session_id else None
        self._session_id = sid
        self._update_filter()

    def set_replay_mode(self, active: bool) -> None:
        """Show or hide the replay-mode banner in the panel toolbar."""
        self._replay_label.setVisible(active)
        self._clear_button.setEnabled(not active)

    def load_replay_frames(self, frames: list[CapturedFrame]) -> None:
        """Replace the current model contents with *frames* from a pcapng file."""
        self._model.clear()
        self._model.load_frames(frames)

    def _on_filter_toggled(self, _checked: bool) -> None:
        self._update_filter()

    def _update_filter(self) -> None:
        if self._filter_check.isChecked():
            self._model.set_session_filter(self._session_id)
        else:
            self._model.set_session_filter(None)
