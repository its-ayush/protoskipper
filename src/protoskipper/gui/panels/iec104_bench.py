# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 104 Bench Overview Panel (§5.12 of IEC104_PLAN.md).

Shows a scrollable grid of RTU tiles — one per open (or recently
failed) IEC 104 session. Each tile displays:
  - Session label and IP:port
  - Profile colour badge (LAB=blue, COMMISSIONING=yellow, PRODUCTION=red)
  - Online LED (green=open, orange=connecting, red=failed/closed)
  - Last RTT estimate
  - Most recent measured value
  - Events (read_completed hits) in the last 60 seconds
  - Last error snippet

Clicking a tile emits ``session_focused(session_id)`` so the main
window can switch the object browser to that session.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import SessionProfile
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.types import SessionId

_logger = logging.getLogger(__name__)

_PROFILE_COLOUR = {
    SessionProfile.LAB: "#2979FF",
    SessionProfile.COMMISSIONING: "#FFA000",
    SessionProfile.PRODUCTION: "#D32F2F",
}
_LED_OPEN = "#43A047"  # green
_LED_FAILED = "#E53935"  # red
_LED_UNKNOWN = "#78909C"  # grey

# Width of the event ring buffer window (seconds)
_WINDOW_S = 60


class _RtuTile(QFrame):
    """A single RTU status tile."""

    clicked = Signal(str)  # session_id

    def __init__(self, session_id: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._session_id = session_id
        self._event_times: deque[float] = deque()
        self._last_rtt_ms: float | None = None
        self._last_read: float | None = None
        self._build_ui()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(220)
        self.setMaximumWidth(280)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # Header row: LED + label
        header_layout = QVBoxLayout()
        self._led_lbl = QLabel("●")
        self._led_lbl.setStyleSheet(f"color: {_LED_UNKNOWN}; font-size: 14pt;")
        self._session_lbl = QLabel(self._session_id[:32])
        self._session_lbl.setWordWrap(True)
        header_layout.addWidget(self._led_lbl)
        header_layout.addWidget(self._session_lbl)
        layout.addLayout(header_layout)

        self._addr_lbl = QLabel("—")
        self._addr_lbl.setStyleSheet("color: #666; font-size: 9pt;")
        layout.addWidget(self._addr_lbl)

        self._profile_lbl = QLabel("—")
        self._profile_lbl.setStyleSheet(
            "background: #78909C; color: white; padding: 2px 6px; border-radius: 4px;"
        )
        self._profile_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._profile_lbl)

        self._rtt_lbl = QLabel("RTT: —")
        self._rtt_lbl.setStyleSheet("font-size: 9pt;")
        layout.addWidget(self._rtt_lbl)

        self._value_lbl = QLabel("Value: —")
        self._value_lbl.setStyleSheet("font-size: 9pt;")
        self._value_lbl.setWordWrap(True)
        layout.addWidget(self._value_lbl)

        self._events_lbl = QLabel("Events/min: 0")
        self._events_lbl.setStyleSheet("font-size: 9pt;")
        layout.addWidget(self._events_lbl)

        self._error_lbl = QLabel("")
        self._error_lbl.setStyleSheet("color: #E53935; font-size: 8pt;")
        self._error_lbl.setWordWrap(True)
        layout.addWidget(self._error_lbl)

    def mousePressEvent(self, event: Any) -> None:
        self.clicked.emit(self._session_id)
        super().mousePressEvent(event)

    # ------------------------------------------------------------------
    def update_from_info(self, info: Any) -> None:
        """Update from a SessionInfo object."""
        device = info.device if info else None
        if device:
            addr = str(device.address)
            self._addr_lbl.setText(addr[:48])
            self._session_lbl.setText(str(device.label or self._session_id)[:32])
        profile = getattr(info, "profile", SessionProfile.LAB)
        colour = _PROFILE_COLOUR.get(profile, "#78909C")
        self._profile_lbl.setText(profile.value.upper() if profile else "—")
        self._profile_lbl.setStyleSheet(
            f"background: {colour}; color: white; padding: 2px 6px; border-radius: 4px;"
        )
        is_open = getattr(info, "is_open", False)
        led_colour = _LED_OPEN if is_open else _LED_FAILED
        self._led_lbl.setStyleSheet(f"color: {led_colour}; font-size: 14pt;")

    def set_last_value(self, value_str: str) -> None:
        self._value_lbl.setText(f"Value: {value_str[:40]}")

    def set_error(self, msg: str) -> None:
        self._error_lbl.setText(msg[:80])

    def record_event(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        self._event_times.append(now)
        # Prune old events outside the window
        cutoff = now - _WINDOW_S
        while self._event_times and self._event_times[0] < cutoff:
            self._event_times.popleft()
        self._events_lbl.setText(f"Events/min: {len(self._event_times)}")

    def record_read(self, rtt_ms: float | None = None) -> None:
        if rtt_ms is not None:
            self._last_rtt_ms = rtt_ms
            self._rtt_lbl.setText(f"RTT: {rtt_ms:.0f} ms")


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


class Iec104BenchPanel(QWidget):
    """Bench overview panel showing all active IEC 104 sessions (§5.12)."""

    session_focused = Signal(str)  # session_id

    def __init__(
        self,
        state: ApplicationState,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._tiles: dict[str, _RtuTile] = {}
        self._build_ui()
        state.session_opened.connect(self._on_session_opened)
        state.session_closed.connect(self._on_session_closed)
        state.session_failed.connect(self._on_session_failed)
        state.read_completed.connect(self._on_read_completed)
        state.error_raised.connect(self._on_error)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._grid_widget = QWidget()
        self._grid_layout = QGridLayout(self._grid_widget)
        self._grid_layout.setSpacing(8)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        scroll.setWidget(self._grid_widget)
        outer.addWidget(scroll)

    def _add_tile(self, session_id: str) -> _RtuTile:
        tile = _RtuTile(session_id, self._grid_widget)
        tile.clicked.connect(self.session_focused)
        self._tiles[session_id] = tile
        # Arrange in rows of 4
        idx = len(self._tiles) - 1
        row, col = divmod(idx, 4)
        self._grid_layout.addWidget(tile, row, col)
        return tile

    def _get_or_add(self, session_id: str) -> _RtuTile:
        if session_id in self._tiles:
            return self._tiles[session_id]
        return self._add_tile(session_id)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_session_opened(self, session_id: str, device: Any, profile: Any) -> None:
        tile = self._get_or_add(session_id)
        info = self._state.session(SessionId(session_id))
        if info:
            tile.update_from_info(info)

    def _on_session_closed(self, session_id: str) -> None:
        tile = self._get_or_add(session_id)
        info = self._state.session(SessionId(session_id))
        if info:
            tile.update_from_info(info)
        else:
            tile._led_lbl.setStyleSheet(f"color: {_LED_FAILED}; font-size: 14pt;")

    def _on_session_failed(self, session_id: str, error_msg: str) -> None:
        tile = self._get_or_add(session_id)
        tile.set_error(error_msg)
        tile._led_lbl.setStyleSheet(f"color: {_LED_FAILED}; font-size: 14pt;")

    def _on_read_completed(self, session_id: str, result: Any) -> None:
        tile = self._tiles.get(session_id)
        if tile is None:
            return
        value_str = str(getattr(result, "value", "?"))
        tile.set_last_value(value_str)
        tile.record_event()

    def _on_error(self, operation: str, message: str) -> None:
        # Can't easily target a specific tile from a generic error_raised signal,
        # so we surface it if there's only one session.
        if len(self._tiles) == 1:
            tile = next(iter(self._tiles.values()))
            tile.set_error(f"{operation}: {message[:60]}")
