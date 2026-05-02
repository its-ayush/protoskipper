# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""WatchlistPanel - persistent table of points the operator is monitoring.

Polling is opt-in and off by default. The operator selects an interval via
a combo box. Switching to a PRODUCTION session profile while polling is
enabled requires explicit confirmation.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import SessionProfile
from protoskipper.gui.models.watchlist_model import WatchlistModel
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId

# Polling combo items: (label, interval_ms | None)
_POLL_OPTIONS: list[tuple[str, int | None]] = [
    ("Off", None),
    ("1 s", 1000),
    ("5 s", 5000),
    ("30 s", 30000),
    ("Custom…", -1),  # -1 → open input dialog
]

_WATCHLIST_DIR = Path.home() / ".config" / "protoskipper" / "watchlists"


class WatchlistPanel(QWidget):
    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._session_manager = session_manager

        self._model = WatchlistModel(state, parent=self)
        self._view = QTableView(self)
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._view.setAlternatingRowColors(True)
        self._view.verticalHeader().setVisible(False)

        self._toolbar = QToolBar(self)
        self._refresh_button = QPushButton("Refresh", self)
        self._refresh_button.setAccessibleName("Refresh all watchlist entries")
        self._refresh_selected_button = QPushButton("Refresh selected", self)
        self._refresh_selected_button.setAccessibleName("Refresh selected watchlist entry")
        self._remove_button = QPushButton("Remove", self)
        self._remove_button.setAccessibleName("Remove selected entry from watchlist")
        self._save_watchlist_button = QPushButton("Save…", self)
        self._save_watchlist_button.setAccessibleName("Save watchlist to JSON file")
        self._save_watchlist_button.setToolTip("Save watchlist to JSON (grouped by profile)")
        self._load_watchlist_button = QPushButton("Load…", self)
        self._load_watchlist_button.setAccessibleName("Load watchlist from JSON file")
        self._load_watchlist_button.setToolTip("Load watchlist entries from a saved JSON file")
        self._toolbar.addWidget(self._refresh_button)
        self._toolbar.addWidget(self._refresh_selected_button)
        self._toolbar.addWidget(self._remove_button)
        self._toolbar.addWidget(self._save_watchlist_button)
        self._toolbar.addWidget(self._load_watchlist_button)

        self._refresh_button.clicked.connect(self._on_refresh_all)
        self._refresh_selected_button.clicked.connect(self._on_refresh_selected)
        self._remove_button.clicked.connect(self._on_remove_selected)
        self._save_watchlist_button.clicked.connect(self._on_save_watchlist)
        self._load_watchlist_button.clicked.connect(self._on_load_watchlist)

        # ---- polling timer controls --------------------------------------
        poll_bar = QWidget(self)
        poll_layout = QHBoxLayout(poll_bar)
        poll_layout.setContentsMargins(0, 0, 0, 0)
        poll_layout.addWidget(QLabel("Poll interval:", self))
        self._poll_combo = QComboBox(self)
        for label, _ in _POLL_OPTIONS:
            self._poll_combo.addItem(label)
        poll_layout.addWidget(self._poll_combo)
        poll_layout.addStretch()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._on_poll_tick)
        self._poll_interval_ms: int | None = None  # None = off

        self._poll_combo.currentIndexChanged.connect(self._on_poll_interval_changed)

        # Cancel polling when any session closes.
        self._state.session_closed.connect(self._on_session_closed)
        # P3.A.3: Auto-restore saved watchlist entries when a session opens.
        self._state.session_opened.connect(self._on_session_opened_restore)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._toolbar)
        layout.addWidget(poll_bar)
        layout.addWidget(self._view)

    # ---- handlers --------------------------------------------------------

    def _on_refresh_all(self) -> None:
        for sid, obj in self._state.watchlist():
            session_info = self._state.session(sid)
            if session_info is None or not session_info.is_open:
                continue
            self._session_manager.read(sid, obj)

    def _on_refresh_selected(self) -> None:
        index = self._view.currentIndex()
        if not index.isValid():
            return
        row = self._model.row_at(index.row())
        if row is None:
            return
        sid, obj = row
        session_info = self._state.session(sid)
        if session_info is None or not session_info.is_open:
            return
        self._session_manager.read(SessionId(sid), obj)

    def _on_remove_selected(self) -> None:
        index = self._view.currentIndex()
        if not index.isValid():
            return
        row = self._model.row_at(index.row())
        if row is None:
            return
        sid, obj = row
        self._state.remove_from_watchlist(SessionId(sid), obj)

    # ---- P3.A.3 save / load watchlist -----------------------------------

    def _on_save_watchlist(self) -> None:
        """Save current watchlist entries to per-profile JSON files."""
        entries_by_profile: dict[str, list[dict]] = {}
        for sid, obj in self._state.watchlist():
            info = self._state.session(sid)
            if info is None:
                continue
            profile = info.profile.value
            if profile not in entries_by_profile:
                entries_by_profile[profile] = []
            entries_by_profile[profile].append(
                {
                    "protocol": info.device.protocol,
                    "device_address": info.device.address,
                    "object_id": obj.object_id,
                    "object_label": obj.label or "",
                    "data_type": obj.data_type,
                    "unit": obj.unit or "",
                }
            )
        if not entries_by_profile:
            QMessageBox.information(self, "Save Watchlist", "The watchlist is empty.")
            return
        _WATCHLIST_DIR.mkdir(parents=True, exist_ok=True)
        for profile, entries in entries_by_profile.items():
            json_path = _WATCHLIST_DIR / f"{profile}.json"
            json_path.write_text(json.dumps(entries, indent=2))
        profiles = ", ".join(entries_by_profile.keys())
        QMessageBox.information(
            self, "Save Watchlist", f"Watchlist saved for profile(s): {profiles}"
        )

    def _on_load_watchlist(self) -> None:
        """Load watchlist entries from a user-selected JSON file."""
        start_dir = str(_WATCHLIST_DIR) if _WATCHLIST_DIR.exists() else str(Path.home())
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Load Watchlist",
            start_dir,
            "JSON watchlists (*.json);;All files (*)",
        )
        if not path_str:
            return
        try:
            entries: list[dict] = json.loads(Path(path_str).read_text())
        except Exception as exc:
            QMessageBox.critical(self, "Load Watchlist", f"Could not read file: {exc}")
            return
        added = self._restore_entries(entries)
        QMessageBox.information(
            self,
            "Load Watchlist",
            f"Added {added} entr{'y' if added == 1 else 'ies'} to watchlist.",
        )

    def _on_session_opened_restore(self, session_id: str, _device: object, profile: object) -> None:
        """Auto-restore watchlist entries saved for this session's profile and device."""
        info: SessionInfo | None = self._state.session(SessionId(session_id))
        if info is None:
            return
        profile_name = info.profile.value
        json_path = _WATCHLIST_DIR / f"{profile_name}.json"
        if not json_path.exists():
            return
        try:
            entries: list[dict] = json.loads(json_path.read_text())
        except Exception:
            return
        # Filter to entries matching this specific device.
        relevant = [
            e
            for e in entries
            if isinstance(e, dict)
            and e.get("protocol") == info.device.protocol
            and e.get("device_address") == info.device.address
        ]
        self._restore_entries(relevant)

    def _restore_entries(self, entries: list[dict]) -> int:
        """Match *entries* against open sessions and add found objects to watchlist.

        Returns the number of entries successfully added.
        """
        added = 0
        for info in self._state.sessions():
            if not info.is_open:
                continue
            sid = SessionId(info.session_id)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("protocol") != info.device.protocol:
                    continue
                if entry.get("device_address") != info.device.address:
                    continue
                for obj in info.objects:
                    if obj.object_id == entry.get("object_id"):
                        if self._state.add_to_watchlist(sid, obj):
                            added += 1
                        break
        return added

    # ---- polling ---------------------------------------------------------

    def _on_poll_interval_changed(self, index: int) -> None:
        _label, interval_ms = _POLL_OPTIONS[index]

        if interval_ms == -1:
            # "Custom…" — ask for a value in seconds.
            secs, ok = QInputDialog.getInt(
                self, "Custom poll interval", "Interval (seconds):", 10, 1, 3600
            )
            if not ok:
                # Restore previous selection without re-triggering this slot.
                self._poll_combo.blockSignals(True)
                prev_index = next(
                    (i for i, (_, ms) in enumerate(_POLL_OPTIONS) if ms == self._poll_interval_ms),
                    0,
                )
                self._poll_combo.setCurrentIndex(prev_index)
                self._poll_combo.blockSignals(False)
                return
            interval_ms = secs * 1000

        # PRODUCTION guard: warn if any open session is on PRODUCTION profile.
        if interval_ms is not None and self._any_open_production_session():
            answer = QMessageBox.question(
                self,
                "Polling on PRODUCTION session",
                "Polling enabled on a PRODUCTION session — confirm",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._poll_combo.blockSignals(True)
                self._poll_combo.setCurrentIndex(0)  # back to "Off"
                self._poll_combo.blockSignals(False)
                return

        self._poll_interval_ms = interval_ms
        if interval_ms is None:
            self._poll_timer.stop()
        else:
            self._poll_timer.start(interval_ms)

    def _any_open_production_session(self) -> bool:
        for sid, _ in self._state.watchlist():
            info = self._state.session(sid)
            if info is not None and info.is_open and info.profile == SessionProfile.PRODUCTION:
                return True
        return False

    def _on_session_closed(self, _session_id: str) -> None:
        """Cancel the polling timer if no open sessions remain."""
        has_open = any(
            info.is_open
            for sid, _ in self._state.watchlist()
            if (info := self._state.session(SessionId(sid))) is not None
        )
        if not has_open:
            self._poll_timer.stop()
            self._poll_combo.blockSignals(True)
            self._poll_combo.setCurrentIndex(0)
            self._poll_combo.blockSignals(False)
            self._poll_interval_ms = None

    def _on_poll_tick(self) -> None:
        """Collect watchlist refs grouped by session and dispatch read_many."""
        from collections import defaultdict

        from protoskipper.core.driver import ObjectRef

        session_refs: dict[SessionId, list[ObjectRef]] = defaultdict(list)
        for sid, obj in self._state.watchlist():
            info = self._state.session(sid)
            if info is None or not info.is_open:
                continue
            session_refs[sid].append(obj)

        for sid, refs in session_refs.items():
            self._session_manager.read_many(sid, refs)
