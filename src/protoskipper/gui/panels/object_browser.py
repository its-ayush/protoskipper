# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""ObjectBrowserPanel - flat table view of one session's addressable objects.

Bound to whichever session is selected in the device tree. Provides the
"Read" and "Write…" actions on the toolbar, plus an "Add to Watchlist"
shortcut on the selected row.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QModelIndex, QObject, QSize, Qt, Signal
from PySide6.QtGui import QAction

# QStyleOptionViewItem moved from QtGui → QtWidgets in PySide6 6.4.
try:
    from PySide6.QtWidgets import QStyleOptionViewItem
except ImportError:
    from PySide6.QtGui import QStyleOptionViewItem  # type: ignore[no-redef]

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStyledItemDelegate,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import ObjectRef
from protoskipper.gui.dialogs.add_register import AddRegisterDialog
from protoskipper.gui.models.object_browser_model import (
    COL_ADDRESS,
    POLL_LABELS,
    POLL_MS,
    ObjectBrowserModel,
)
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId


class _LeftPaddedDelegate(QStyledItemDelegate):
    """Item delegate that adds extra left padding — used to create a visual gap
    between the Unit column and the Address column."""

    _PADDING = 16  # extra pixels of left padding

    def initStyleOption(self, option: QStyleOptionViewItem, index: QModelIndex) -> None:  # type: ignore[override]
        super().initStyleOption(option, index)
        option.rect = option.rect.adjusted(self._PADDING, 0, 0, 0)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        sh = super().sizeHint(option, index)
        return QSize(sh.width() + self._PADDING, sh.height())


class _ViewportClickFilter(QObject):
    """Event filter installed on the QTableView viewport.

    Clears the selection when the user clicks on empty space below the rows,
    so the row highlight doesn't stay stuck after a single click.
    """

    def __init__(self, view: QTableView) -> None:
        super().__init__(view)
        self._view = view

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.MouseButtonPress:
            pos = event.position().toPoint()  # type: ignore[attr-defined]
            index = self._view.indexAt(pos)
            if not index.isValid():
                self._view.clearSelection()
                self._view.setCurrentIndex(QModelIndex())
        return False


class ObjectBrowserPanel(QWidget):
    """Table of objects for the currently focused session."""

    write_requested = Signal(str, object)  # SessionId, ObjectRef
    add_to_watchlist_requested = Signal(str, object)  # SessionId, ObjectRef  (kept for compat)
    add_unit_requested = Signal(str, int)  # SessionId, new_unit_id

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._session_manager = session_manager
        self._session_id: SessionId | None = None

        self._model = ObjectBrowserModel(state, parent=self)
        self._view = QTableView(self)
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._view.horizontalHeader().setStretchLastSection(True)
        self._view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._view.setAlternatingRowColors(True)
        self._view.setShowGrid(True)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_context_menu)

        # Visual gap between Unit and Address columns via a left-padded delegate.
        self._view.setItemDelegateForColumn(COL_ADDRESS, _LeftPaddedDelegate(self._view))

        # Clear selection when clicking below all rows; toggle on re-click of same row.
        self._view.viewport().installEventFilter(_ViewportClickFilter(self._view))
        self._last_clicked_row: int = -1
        self._view.clicked.connect(self._on_view_clicked)

        # ---- Empty-state placeholder (shown when session is open but has no objects) ----
        self._empty_label = QLabel(
            "<center>"
            "<b>No registers defined.</b><br><br>"
            "Use <b>Add Register…</b> to add a register manually,<br>"
            "or right-click the session in the Device Tree → "
            "<b>Import register map…</b> to load a CSV."
            "</center>",
            self,
        )
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: grey; padding: 24px;")

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._view)  # index 0: table
        self._stack.addWidget(self._empty_label)  # index 1: empty state

        self._toolbar = QToolBar(self)
        self._add_register_button = QPushButton("Add Register…", self)
        self._add_register_button.setAccessibleName("Add a register to this session")
        self._add_register_button.setToolTip(
            "Define a Modbus register address, type, and encoding to add to this session"
        )
        self._add_unit_button = QPushButton("Add Unit ID…", self)
        self._add_unit_button.setAccessibleName(
            "Open a new session with a different unit ID on the same gateway"
        )
        self._add_unit_button.setToolTip(
            "Open a new session to the same TCP/RTU gateway with a different Modbus unit ID"
        )
        self._read_button = QPushButton("Read selected", self)
        self._read_button.setAccessibleName("Read selected object")
        self._read_button.setToolTip("Read the selected register once  [F5]")
        self._read_all_button = QPushButton("Read all", self)
        self._read_all_button.setAccessibleName("Read all objects once")
        self._read_all_button.setToolTip("Read every register once  [Shift+F5]")
        self._write_button = QPushButton("Write…", self)
        self._write_button.setAccessibleName("Write to selected object")
        self._remove_button = QPushButton("Remove", self)
        self._remove_button.setAccessibleName("Remove selected register from this session")
        self._remove_button.setToolTip("Remove the selected register from this session")

        self._export_map_button = QPushButton("Export Map…", self)
        self._export_map_button.setToolTip(
            "Export the register list for this session to a JSON file "
            "for use with other similar devices"
        )
        self._import_map_button = QPushButton("Import Map…", self)
        self._import_map_button.setToolTip(
            "Import a previously saved register map JSON file into this session"
        )

        # Poll-All combobox: sets the same polling interval on every register.
        self._poll_all_combo = QComboBox(self)
        self._poll_all_combo.addItem("Poll all: Off")
        for _poll_label in POLL_LABELS[1:]:  # skip "Off"
            self._poll_all_combo.addItem(f"Poll all: {_poll_label}")
        self._poll_all_combo.setToolTip(
            "Continuously poll all registers at this interval. "
            "You can also set a per-register interval via right-click → Set Poll Interval."
        )
        self._poll_all_combo.setAccessibleName("Poll all registers at selected interval")

        for btn in (
            self._add_register_button,
            self._add_unit_button,
            self._read_button,
            self._read_all_button,
            self._write_button,
            self._remove_button,
        ):
            self._toolbar.addWidget(btn)
        self._toolbar.addSeparator()
        self._toolbar.addWidget(self._export_map_button)
        self._toolbar.addWidget(self._import_map_button)
        self._toolbar.addSeparator()
        self._toolbar.addWidget(self._poll_all_combo)

        self._add_register_button.clicked.connect(self._on_add_register_clicked)
        self._add_unit_button.clicked.connect(self._on_add_unit_clicked)
        self._read_button.clicked.connect(self._on_read_clicked)
        self._read_all_button.clicked.connect(self._on_read_all_clicked)
        self._write_button.clicked.connect(self._on_write_clicked)
        self._remove_button.clicked.connect(self._on_remove_clicked)
        self._export_map_button.clicked.connect(self._on_export_map_clicked)
        self._import_map_button.clicked.connect(self._on_import_map_clicked)
        self._poll_all_combo.currentIndexChanged.connect(self._on_poll_all_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._stack)

        # Subscribe to object-list changes to switch stack page.
        self._state.objects_enumerated.connect(self._on_objects_enumerated)
        self._state.session_opened.connect(self._update_stack)
        self._state.session_closed.connect(self._update_stack)

        # P2.A.3: initialise before _update_actions() which reads it.
        self._replay_mode: bool = False
        self._update_actions()
        self._update_stack()
        self._view.selectionModel().selectionChanged.connect(self._update_actions)
        # P2.A.3: wire signal after init so it can't fire before _replay_mode exists.
        self._state.replay_mode_changed.connect(self._on_replay_mode_changed)

    def set_session(self, session_id: str | None) -> None:
        sid = SessionId(session_id) if session_id else None
        self._session_id = sid
        self._model.set_session(sid)
        self._update_actions()
        self._update_stack()

    def current_session_id(self) -> SessionId | None:
        """Return the currently displayed session's id, or None."""
        return self._session_id

    # ---- P3.B.2 public read API (invoked by MainWindow F5/Shift+F5) -----

    def read_selected(self) -> None:
        """Read the currently selected register. Bound to F5 in MainWindow."""
        self._on_read_clicked()

    def read_all(self) -> None:
        """Read all visible registers. Bound to Shift+F5 in MainWindow."""
        self._on_read_all_clicked()

    # ---- selection helper -----------------------------------------------

    def _selected_object(self) -> ObjectRef | None:
        if self._session_id is None:
            return None
        info = self._state.session(self._session_id)
        if info is None:
            return None
        index = self._view.currentIndex()
        if not index.isValid():
            return None
        if index.row() >= len(info.objects):
            return None
        return info.objects[index.row()]

    def _update_actions(self, *_args) -> None:
        has_session = self._session_id is not None
        info = self._state.session(self._session_id) if has_session else None
        is_open = info.is_open if info else False
        has_selection = has_session and self._view.currentIndex().isValid()
        obj = self._selected_object()
        is_writable = obj is not None and obj.access.value != "ro"

        self._add_register_button.setEnabled(is_open and not self._replay_mode)
        self._add_unit_button.setEnabled(is_open and not self._replay_mode)
        self._read_all_button.setEnabled(is_open and not self._replay_mode)
        self._read_button.setEnabled(is_open and has_selection and not self._replay_mode)
        self._write_button.setEnabled(
            is_open and has_selection and is_writable and not self._replay_mode
        )
        self._remove_button.setEnabled(is_open and has_selection and not self._replay_mode)
        has_objects = info is not None and bool(info.objects)
        self._export_map_button.setEnabled(has_objects)
        self._import_map_button.setEnabled(is_open and not self._replay_mode)
        self._poll_all_combo.setEnabled(is_open and not self._replay_mode)
        _tooltip = "Replay mode — writes are disabled" if self._replay_mode else ""
        self._write_button.setToolTip(_tooltip or "Write a value to the selected register")

    def _update_stack(self, *_args) -> None:
        """Switch between the table and the empty-state placeholder."""
        has_session = self._session_id is not None
        info = self._state.session(self._session_id) if has_session else None
        is_open = info.is_open if info else False
        has_objects = info is not None and len(info.objects) > 0
        # Show empty state only when session is open but has no objects.
        if is_open and not has_objects:
            self._stack.setCurrentIndex(1)
        else:
            self._stack.setCurrentIndex(0)

    def _on_objects_enumerated(self, session_id: str, _objects: list) -> None:
        if SessionId(session_id) == self._session_id:
            self._update_stack()

    # ---- handlers --------------------------------------------------------

    def _on_replay_mode_changed(self, active: bool) -> None:
        self._replay_mode = active
        self._update_actions()

    def _on_add_register_clicked(self) -> None:
        if self._session_id is None:
            return
        info = self._state.session(self._session_id)
        if info is None or not info.is_open:
            return

        next_address_hint: int | None = None
        next_table_hint: str | None = None
        next_dtype_hint: str | None = None

        while True:
            # Re-fetch info on each iteration so the object list is current.
            info = self._state.session(self._session_id)
            if info is None or not info.is_open:
                break
            existing_ids = {o.object_id for o in info.objects}
            dialog = AddRegisterDialog(
                device=info.device,
                existing_ids=existing_ids,
                address_hint=next_address_hint,
                table_hint=next_table_hint,
                dtype_hint=next_dtype_hint,
                parent=self,
            )
            if dialog.exec() != AddRegisterDialog.Accepted:
                break
            ref = dialog.object_ref()
            if ref.object_id in existing_ids:
                QMessageBox.warning(
                    self,
                    "Duplicate register",
                    f"Register {ref.object_id!r} is already in this session.\n"
                    "Each register can only be added once.",
                )
                # Re-open the dialog so the user can correct the address.
                continue
            self._session_manager.add_object(self._session_id, ref)
            # Apply current poll-all interval to the newly added register.
            combo_idx = self._poll_all_combo.currentIndex()
            if combo_idx > 0:
                label = POLL_LABELS[combo_idx]
                interval_ms = POLL_MS[label]
                self._session_manager.set_poll_interval(self._session_id, ref, interval_ms)
                self._model.set_poll_label(ref.object_id, label)
            if not dialog.wants_next:
                break
            # Pre-populate next dialog: same table + type, address bumped by count.
            next_address_hint = dialog.next_address()
            next_table_hint = dialog.current_table()
            next_dtype_hint = dialog.current_dtype()

    def _on_add_unit_clicked(self) -> None:
        if self._session_id is None:
            return
        info = self._state.session(self._session_id)
        if info is None or not info.is_open:
            return
        unit_id, ok = QInputDialog.getInt(
            self,
            "Add Unit ID",
            "Enter the Modbus unit ID (slave address) to open on the same gateway (1-247):",
            value=1,
            min=1,
            max=247,
        )
        if ok:
            self.add_unit_requested.emit(self._session_id, unit_id)

    def _on_remove_clicked(self) -> None:
        obj = self._selected_object()
        if obj is None or self._session_id is None:
            return
        self._session_manager.remove_object(self._session_id, obj)

    def _on_view_clicked(self, index: QModelIndex) -> None:
        """Toggle selection off when the same row is clicked again."""
        if index.row() == self._last_clicked_row:
            self._view.clearSelection()
            self._view.setCurrentIndex(QModelIndex())
            self._last_clicked_row = -1
        else:
            self._last_clicked_row = index.row()
        self._update_actions()

    def _on_context_menu(self, point) -> None:
        obj = self._selected_object()
        if obj is None or self._session_id is None:
            return
        info = self._state.session(self._session_id)
        is_open = info.is_open if info else False
        menu = QMenu(self._view)

        if is_open and not self._replay_mode:
            read_once_action = QAction("Read once  (F5)", self)
            read_once_action.triggered.connect(self._on_read_clicked)
            menu.addAction(read_once_action)

            poll_menu = menu.addMenu("Set poll interval")
            current_ms = self._session_manager.poll_interval(self._session_id, obj)
            for label in POLL_LABELS:
                ms = POLL_MS[label]
                action = QAction(label, self)
                action.setCheckable(True)
                action.setChecked(current_ms == ms)
                action.triggered.connect(
                    lambda _checked=False, _obj=obj, _label=label, _ms=ms: self._set_poll(
                        _obj, _label, _ms
                    )
                )
                poll_menu.addAction(action)

            menu.addSeparator()

            if obj.access.value != "ro":
                write_action = QAction("Write…", self)
                write_action.triggered.connect(self._on_write_clicked)
                menu.addAction(write_action)

            menu.addSeparator()
            remove_action = QAction("Remove register", self)
            remove_action.triggered.connect(self._on_remove_clicked)
            menu.addAction(remove_action)

            menu.addSeparator()
            add_next_action = QAction("Add next register (same table+type)…", self)
            add_next_action.triggered.connect(lambda: self._on_add_next_from_row(obj))
            menu.addAction(add_next_action)

        if menu.actions():
            menu.exec(self._view.viewport().mapToGlobal(point))

    def _on_add_next_from_row(self, ref: ObjectRef) -> None:
        """Open AddRegisterDialog pre-populated with the address after *ref*."""
        if self._session_id is None:
            return
        info = self._state.session(self._session_id)
        if info is None or not info.is_open:
            return
        # Parse object_id "table:addr" or "table:addr:count"
        parts = ref.object_id.split(":")
        table = parts[0]
        try:
            addr = int(parts[1])
        except (IndexError, ValueError):
            addr = 0
        try:
            count = int(parts[2])
        except (IndexError, ValueError):
            count = 1
        existing_ids = {o.object_id for o in info.objects}
        dialog = AddRegisterDialog(
            device=info.device,
            existing_ids=existing_ids,
            address_hint=addr + count,
            table_hint=table,
            dtype_hint=ref.data_type,
            parent=self,
        )
        if dialog.exec() != AddRegisterDialog.Accepted:
            return
        new_ref = dialog.object_ref()
        if new_ref.object_id in existing_ids:
            QMessageBox.warning(
                self,
                "Duplicate register",
                f"Register {new_ref.object_id!r} is already in this session.",
            )
            return
        self._session_manager.add_object(self._session_id, new_ref)
        combo_idx = self._poll_all_combo.currentIndex()
        if combo_idx > 0:
            label = POLL_LABELS[combo_idx]
            interval_ms = POLL_MS[label]
            self._session_manager.set_poll_interval(self._session_id, new_ref, interval_ms)
            self._model.set_poll_label(new_ref.object_id, label)

    def _on_export_map_clicked(self) -> None:
        if self._session_id is None:
            return
        info = self._state.session(self._session_id)
        if info is None or not info.objects:
            QMessageBox.information(
                self, "Nothing to export", "No registers defined in this session."
            )
            return
        from protoskipper.gui.services.setup_io import RegisterMap, save_register_map

        default_dir = str(Path.home() / ".protoskipper" / "maps")
        Path(default_dir).mkdir(parents=True, exist_ok=True)
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export Register Map",
            default_dir,
            "Register maps (*.json);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        if not path.suffix:
            path = path.with_suffix(".json")
        try:
            save_register_map(RegisterMap(info.device.protocol, list(info.objects)), path)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def _on_import_map_clicked(self) -> None:
        if self._session_id is None:
            return
        info = self._state.session(self._session_id)
        if info is None or not info.is_open:
            return
        from protoskipper.gui.services.setup_io import load_register_map

        default_dir = str(Path.home() / ".protoskipper" / "maps")
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import Register Map",
            default_dir,
            "Register maps (*.json);;All files (*)",
        )
        if not path_str:
            return
        rmap = load_register_map(Path(path_str))
        if not rmap.objects:
            QMessageBox.warning(self, "Empty map", "No valid registers found in the selected file.")
            return
        # Patch device reference and merge (skip duplicates).
        import dataclasses

        existing_ids = {o.object_id for o in info.objects}
        added = 0
        for obj in rmap.objects:
            if obj.object_id in existing_ids:
                continue
            patched = dataclasses.replace(obj, device=info.device)
            self._session_manager.add_object(self._session_id, patched)
            existing_ids.add(obj.object_id)
            added += 1
        skipped = len(rmap.objects) - added
        msg = f"Imported {added} register(s)."
        if skipped:
            msg += f" Skipped {skipped} duplicate(s)."
        QMessageBox.information(self, "Import complete", msg)

    def _set_poll(self, obj: ObjectRef, label: str, interval_ms: int) -> None:
        if self._session_id is None:
            return
        self._session_manager.set_poll_interval(self._session_id, obj, interval_ms)
        self._model.set_poll_label(obj.object_id, label)

    def _on_poll_all_changed(self, combo_index: int) -> None:
        if self._session_id is None:
            return
        info = self._state.session(self._session_id)
        if info is None or not info.is_open:
            return
        # combo_index 0 = "Poll all: Off" → POLL_LABELS[0] = "Off"
        label = POLL_LABELS[combo_index]
        interval_ms = POLL_MS[label]
        for obj in info.objects:
            self._session_manager.set_poll_interval(self._session_id, obj, interval_ms)
            self._model.set_poll_label(obj.object_id, label)

    def _on_read_clicked(self) -> None:
        obj = self._selected_object()
        if obj is None or self._session_id is None:
            return
        self._session_manager.read(self._session_id, obj)

    def _on_read_all_clicked(self) -> None:
        if self._session_id is None:
            return
        info = self._state.session(self._session_id)
        if info is None:
            return
        for obj in info.objects:
            self._session_manager.read(self._session_id, obj)

    def _on_write_clicked(self) -> None:
        obj = self._selected_object()
        if obj is None or self._session_id is None:
            return
        self.write_requested.emit(self._session_id, obj)
