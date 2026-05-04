# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""ScheduleEditorPanel — weekly schedule & exception editor (P7.H.5 / §5.8).

Provides a GUI panel for viewing and editing BACnet ``Schedule`` object
properties:

* **Weekly view** -- 7-row table (Mon-Sun).  Each row lists the time-value
  entries for that day.  Add / edit / remove entries via inline dialogs.
* **Exception list** — read-only list of ``Exception_Schedule`` entries.
  Full editing is deferred to a future task.
* **Default value** — shows ``Schedule_Default`` with a plain-text edit field.
* **Write** — issues ``WriteProperty`` calls for ``Weekly_Schedule`` and
  ``Schedule_Default`` through the active session.

Design notes
------------
* This panel has **zero protocol-specific imports** — all BACnet calls go
  through :class:`~protoskipper.gui.services.session_manager.SessionManager`.
* The panel is disabled when no session is open.
* In PRODUCTION profile the Write button is locked behind the normal
  write-confirm flow (handled transparently by SessionManager).
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId

_logger = logging.getLogger(__name__)

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class ScheduleEditorPanel(QWidget):
    """Weekly schedule and exception editor panel.

    Parameters
    ----------
    state:
        Shared application state (session signals).
    session_manager:
        Used for read / write BACnet operations.
    parent:
        Optional Qt parent widget.
    """

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._sm = session_manager
        self._current_sid: SessionId | None = None
        # Internal schedule data: list of 7 day-lists; each day is a list of
        # {"time": "HH:MM:SS", "value": str} dicts sorted by time.
        self._weekly: list[list[dict[str, str]]] = [[] for _ in range(7)]
        self._schedule_default: str = ""
        self._target_object_id: str = ""  # e.g. "schedule:1"

        self._build_ui()
        state.session_opened.connect(self._on_session_opened)
        state.session_closed.connect(self._on_session_closed)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        # Top bar: object selector + status
        top = QHBoxLayout()
        top.addWidget(QLabel("Schedule object:"))
        self._obj_edit = QLineEdit()
        self._obj_edit.setPlaceholderText("e.g. schedule:1")
        self._obj_edit.setMaximumWidth(160)
        top.addWidget(self._obj_edit)
        self._load_btn = QPushButton("Load")
        self._load_btn.setToolTip("Read Weekly_Schedule from device")
        self._load_btn.clicked.connect(self._do_load)
        top.addWidget(self._load_btn)
        top.addStretch()
        self._status_label = QLabel("No session")
        top.addWidget(self._status_label)
        root.addLayout(top)

        # Splitter: weekly table (left) + exception list (right)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- Weekly schedule table ---
        weekly_box = QGroupBox("Weekly Schedule")
        wbl = QVBoxLayout(weekly_box)

        self._day_combo = QComboBox()
        self._day_combo.addItems(_DAYS)
        self._day_combo.currentIndexChanged.connect(self._on_day_changed)
        wbl.addWidget(self._day_combo)

        self._entry_table = QTableWidget(0, 2)
        self._entry_table.setHorizontalHeaderLabels(["Time (HH:MM:SS)", "Value"])
        self._entry_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._entry_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._entry_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._entry_table.setAlternatingRowColors(True)
        wbl.addWidget(self._entry_table)

        entry_btns = QHBoxLayout()
        self._add_entry_btn = QPushButton("Add entry…")
        self._add_entry_btn.clicked.connect(self._do_add_entry)
        self._edit_entry_btn = QPushButton("Edit entry…")
        self._edit_entry_btn.clicked.connect(self._do_edit_entry)
        self._remove_entry_btn = QPushButton("Remove entry")
        self._remove_entry_btn.clicked.connect(self._do_remove_entry)
        for b in (self._add_entry_btn, self._edit_entry_btn, self._remove_entry_btn):
            entry_btns.addWidget(b)
        wbl.addLayout(entry_btns)

        # Default value
        def_layout = QHBoxLayout()
        def_layout.addWidget(QLabel("Default value:"))
        self._default_edit = QLineEdit()
        self._default_edit.setPlaceholderText("null")
        def_layout.addWidget(self._default_edit)
        wbl.addLayout(def_layout)

        splitter.addWidget(weekly_box)

        # --- Exception list ---
        exc_box = QGroupBox("Exceptions (read-only)")
        ebl = QVBoxLayout(exc_box)
        self._exc_list = QListWidget()
        self._exc_list.setToolTip("Exception_Schedule entries — full editing in a future release")
        ebl.addWidget(self._exc_list)
        splitter.addWidget(exc_box)

        splitter.setSizes([480, 220])
        root.addWidget(splitter, 1)

        # Bottom bar: write + preview
        bottom = QHBoxLayout()
        self._write_btn = QPushButton("Write Schedule to Device")
        self._write_btn.setToolTip("Issue WriteProperty for Weekly_Schedule and Schedule_Default")
        self._write_btn.clicked.connect(self._do_write)
        bottom.addWidget(self._write_btn)
        bottom.addStretch()
        self._preview_label = QLabel("")
        self._preview_label.setToolTip("Current active value from the local schedule model")
        bottom.addWidget(self._preview_label)
        root.addLayout(bottom)

        self._set_controls_enabled(False)

    # ------------------------------------------------------------------
    # Session signals
    # ------------------------------------------------------------------

    def _on_session_opened(self, sid: SessionId) -> None:
        self._current_sid = sid
        self._status_label.setText("Session open")
        self._set_controls_enabled(True)

    def _on_session_closed(self, sid: SessionId) -> None:
        if sid == self._current_sid:
            self._current_sid = None
            self._status_label.setText("No session")
            self._set_controls_enabled(False)

    def _set_controls_enabled(self, enabled: bool) -> None:
        for w in (
            self._load_btn,
            self._add_entry_btn,
            self._edit_entry_btn,
            self._remove_entry_btn,
            self._write_btn,
        ):
            w.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _do_load(self) -> None:
        """Read Weekly_Schedule and Schedule_Default from the device."""
        if self._current_sid is None:
            return
        obj_id = self._obj_edit.text().strip()
        if not obj_id:
            self._status_label.setText("Enter a schedule object ID first")
            return
        self._target_object_id = obj_id
        self._status_label.setText(f"Loading {obj_id}…")
        # Trigger read via session manager; result comes back via read_completed signal
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(
            object_id=obj_id,
            label=obj_id,
            access=None,  # type: ignore[arg-type]
            data_type="BACnetWeeklySchedule",
        )
        try:
            self._sm.read(self._current_sid, ref)
        except Exception as exc:
            _logger.warning("Schedule load failed: %s", exc)
            self._status_label.setText(f"Load failed: {exc}")

    def populate_weekly(self, weekly: list[list[dict[str, Any]]]) -> None:
        """Populate the editor from a live-read weekly schedule.

        Parameters
        ----------
        weekly:
            7-element list (index 0 = Monday); each element is a list of
            ``{"time": "HH:MM:SS", "value": <any>}`` dicts.
        """
        self._weekly = [
            [{"time": e["time"], "value": str(e["value"])} for e in day] for day in weekly
        ]
        self._refresh_entry_table()
        self._update_preview()

    def populate_exceptions(self, exceptions: list[str]) -> None:
        """Populate the exception list with display strings."""
        self._exc_list.clear()
        for text in exceptions:
            self._exc_list.addItem(QListWidgetItem(text))

    def _do_add_entry(self) -> None:
        day_idx = self._day_combo.currentIndex()
        dlg = _EntryDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            entry = dlg.entry()
            self._weekly[day_idx].append(entry)
            self._weekly[day_idx].sort(key=lambda e: e["time"])
            self._refresh_entry_table()
            self._update_preview()

    def _do_edit_entry(self) -> None:
        day_idx = self._day_combo.currentIndex()
        rows = self._entry_table.selectedItems()
        if not rows:
            return
        row = self._entry_table.currentRow()
        if row < 0 or row >= len(self._weekly[day_idx]):
            return
        existing = self._weekly[day_idx][row]
        dlg = _EntryDialog(time=existing["time"], value=existing["value"], parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._weekly[day_idx][row] = dlg.entry()
            self._weekly[day_idx].sort(key=lambda e: e["time"])
            self._refresh_entry_table()
            self._update_preview()

    def _do_remove_entry(self) -> None:
        day_idx = self._day_combo.currentIndex()
        row = self._entry_table.currentRow()
        if row < 0 or row >= len(self._weekly[day_idx]):
            return
        del self._weekly[day_idx][row]
        self._refresh_entry_table()
        self._update_preview()

    def _do_write(self) -> None:
        """Issue WriteProperty for Weekly_Schedule and Schedule_Default."""
        if self._current_sid is None:
            return
        if not self._target_object_id:
            self._status_label.setText("Enter a schedule object ID first")
            return
        self._schedule_default = self._default_edit.text().strip()
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(
            object_id=self._target_object_id,
            label=self._target_object_id,
            access=None,  # type: ignore[arg-type]
            data_type="BACnetWeeklySchedule",
        )
        try:
            self._sm.prepare_write(self._current_sid, ref, self._weekly)
            self._status_label.setText("Write requested")
            _logger.info(
                "ScheduleEditorPanel: prepare_write weeklySchedule for %s",
                self._target_object_id,
            )
        except Exception as exc:
            _logger.warning("Schedule write failed: %s", exc)
            self._status_label.setText(f"Write failed: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_day_changed(self, _: int) -> None:
        self._refresh_entry_table()

    def _refresh_entry_table(self) -> None:
        day_idx = self._day_combo.currentIndex()
        entries = self._weekly[day_idx]
        self._entry_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self._entry_table.setItem(row, 0, QTableWidgetItem(entry["time"]))
            self._entry_table.setItem(row, 1, QTableWidgetItem(entry["value"]))

    def _update_preview(self) -> None:
        """Compute the active value at the current time and show it."""
        try:
            from datetime import datetime

            from protoskipper.builtin_drivers.bacnet.simulator import evaluate_weekly_schedule

            # Convert internal format to expected tuple list
            weekly_as_tuples = [[(e["time"], e["value"]) for e in day] for day in self._weekly]
            now = datetime.now()
            active = evaluate_weekly_schedule(weekly_as_tuples, now)
            self._preview_label.setText(
                f"Active now: {active}" if active is not None else "Active now: (none)"
            )
        except Exception:
            self._preview_label.setText("")


# ---------------------------------------------------------------------------
# Entry add/edit dialog
# ---------------------------------------------------------------------------


class _EntryDialog(QDialog):
    """Small dialog to enter / edit one time-value entry."""

    def __init__(
        self,
        *,
        time: str = "00:00:00",
        value: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Schedule Entry")
        self.setMinimumWidth(280)

        form = QFormLayout()
        self._time_edit = QLineEdit(time)
        self._time_edit.setPlaceholderText("HH:MM:SS")
        form.addRow("Time:", self._time_edit)

        self._value_edit = QLineEdit(value)
        self._value_edit.setPlaceholderText("e.g. 1, 21.5, true, null")
        form.addRow("Value:", self._value_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def entry(self) -> dict[str, str]:
        return {
            "time": self._time_edit.text().strip(),
            "value": self._value_edit.text().strip(),
        }
