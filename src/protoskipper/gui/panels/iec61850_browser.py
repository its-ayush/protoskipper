# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Iec61850BrowserPanel — hierarchical IEC 61850 data model browser.

This panel provides three views of the IED data model discovered via SCL
(IEC 61850-6 configuration file downloaded from the IED):

* **Tree** (left) — LD → LN → DO hierarchy.  Selecting a node filters the
  table on the right to show only the data attributes in that subtree.
* **Table** (right) — flat list of leaf data attributes with FC, basic type,
  CDC, last-read value, and a poll-interval selector.
* **Toolbar** — "Fetch SCL Tags" re-downloads and re-parses the SCL; "Read
  selected" / "Read all visible" trigger live reads; "Write…" opens the
  standard write dialog for settable attributes.

Design constraints
------------------
* No protocol-specific imports — the panel only uses
  :class:`~protoskipper.core.driver.ObjectRef` (from *core*) and plain
  Python ``dict`` objects carried in the ``metadata`` field.
* All network operations are delegated to
  :class:`~protoskipper.gui.services.SessionManager` (non-blocking queued
  calls); results arrive via
  :attr:`~protoskipper.gui.services.ApplicationState.iec_tags_loaded` and
  :attr:`~protoskipper.gui.services.ApplicationState.read_completed`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QSplitter,
    QTableView,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from protoskipper.core.driver import ObjectRef, ReadResult
from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager
from protoskipper.gui.services.types import SessionId

_logger = logging.getLogger(__name__)

# ---- table columns -------------------------------------------------------
_COL_LABEL = 0
_COL_FC = 1
_COL_TYPE = 2
_COL_CDC = 3
_COL_VALUE = 4
_COL_QUALITY = 5
_COL_POLL = 6
_COL_ACCESS = 7
_HEADERS = ["MMS Path", "FC", "Type", "CDC", "Value", "Quality", "Poll", "Access"]

_POLL_OPTIONS = [("Off", 0), ("500 ms", 500), ("1 s", 1000), ("5 s", 5000), ("10 s", 10_000)]
_POLL_LABELS = [lbl for lbl, _ in _POLL_OPTIONS]
_POLL_MS = [ms for _, ms in _POLL_OPTIONS]


# ---- model ---------------------------------------------------------------


class _TagTableModel(QAbstractTableModel):
    """Table model backed by a flat list of :class:`ObjectRef` instances."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._refs: list[ObjectRef] = []
        self._values: dict[str, ReadResult] = {}  # object_id -> ReadResult

    # ---- QAbstractTableModel interface ----------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return len(self._refs)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return len(_HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if row >= len(self._refs):
            return None
        ref = self._refs[row]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(ref, col)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(ref, col)
        if role == Qt.ItemDataRole.FontRole and col == _COL_VALUE:
            f = QFont()
            f.setFamily("Menlo, Monaco, Consolas, monospace")
            return f
        if role == Qt.ItemDataRole.UserRole:
            return ref
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        return base

    # ---- helpers --------------------------------------------------------

    def _display(self, ref: ObjectRef, col: int) -> str:
        m = ref.metadata
        if col == _COL_LABEL:
            return ref.label
        if col == _COL_FC:
            return str(m.get("fc", ""))
        if col == _COL_TYPE:
            return ref.data_type.upper() if ref.data_type else ""
        if col == _COL_CDC:
            return str(m.get("cdc", ""))
        if col == _COL_VALUE:
            result = self._values.get(ref.object_id)
            if result is None:
                return ""
            if result.error:
                return f"ERR: {result.error}"
            return str(result.value) if result.value is not None else ""
        if col == _COL_QUALITY:
            result = self._values.get(ref.object_id)
            if result is None:
                return ""
            return result.quality.name if result.quality is not None else ""
        if col == _COL_POLL:
            return ""  # handled by delegate
        if col == _COL_ACCESS:
            return ref.access.value if ref.access else ""
        return ""

    def _foreground(self, ref: ObjectRef, col: int) -> QColor | None:
        if col == _COL_VALUE:
            result = self._values.get(ref.object_id)
            if result is not None and result.error:
                return QColor("#c00")
        return None

    # ---- public mutators ------------------------------------------------

    def set_refs(self, refs: list[ObjectRef]) -> None:
        self.beginResetModel()
        self._refs = list(refs)
        self.endResetModel()

    def record_read(self, result: ReadResult) -> None:
        oid = result.object_ref.object_id
        self._values[oid] = result
        for row, ref in enumerate(self._refs):
            if ref.object_id == oid:
                self.dataChanged.emit(
                    self.index(row, _COL_VALUE),
                    self.index(row, _COL_QUALITY),
                    [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole],
                )
                break

    def ref_at(self, row: int) -> ObjectRef | None:
        if 0 <= row < len(self._refs):
            return self._refs[row]
        return None

    def all_refs(self) -> list[ObjectRef]:
        return list(self._refs)


# ---- main panel ----------------------------------------------------------


class Iec61850BrowserPanel(QWidget):
    """IEC 61850 data model browser panel.

    Emits
    -----
    write_requested(ObjectRef)
        User triggered "Write…" for a settable data attribute.
    add_to_watchlist_requested(SessionId, ObjectRef)
        User triggered "Add to Watchlist" for a tag.
    """

    write_requested = Signal(object)  # ObjectRef
    add_to_watchlist_requested = Signal(str, object)  # session_id, ObjectRef

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._sm = session_manager
        self._session_id: SessionId | None = None

        # All tags for the current session, grouped for tree building.
        self._all_refs: list[ObjectRef] = []

        self._build_ui()
        self._wire_signals()

    # ---- construction ---------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- toolbar --
        toolbar = QToolBar(self)
        toolbar.setMovable(False)

        self._action_fetch = QAction("Fetch SCL Tags", self)
        self._action_fetch.setToolTip(
            "Download the IED's SCL configuration and expand DataTypeTemplates into tag list"
        )
        self._action_fetch.triggered.connect(self._on_fetch_clicked)
        toolbar.addAction(self._action_fetch)
        toolbar.addSeparator()

        self._action_read_sel = QAction("Read Selected", self)
        self._action_read_sel.setEnabled(False)
        self._action_read_sel.triggered.connect(self._on_read_selected)
        toolbar.addAction(self._action_read_sel)

        self._action_read_all = QAction("Read All Visible", self)
        self._action_read_all.setEnabled(False)
        self._action_read_all.triggered.connect(self._on_read_all)
        toolbar.addAction(self._action_read_all)

        self._action_write = QAction("Write…", self)
        self._action_write.setEnabled(False)
        self._action_write.triggered.connect(self._on_write_clicked)
        toolbar.addAction(self._action_write)

        self._action_watchlist = QAction("Add to Watchlist", self)
        self._action_watchlist.setEnabled(False)
        self._action_watchlist.triggered.connect(self._on_add_watchlist)
        toolbar.addAction(self._action_watchlist)

        toolbar.addSeparator()

        # Poll combo for selected items.
        toolbar.addWidget(QLabel("Poll: "))
        self._poll_combo = QComboBox(self)
        self._poll_combo.addItems(_POLL_LABELS)
        self._poll_combo.currentIndexChanged.connect(self._on_poll_changed)
        toolbar.addWidget(self._poll_combo)

        root.addWidget(toolbar)

        # -- status label --
        self._status_label = QLabel('Select an IEC 61850 session and click "Fetch SCL Tags".')
        self._status_label.setContentsMargins(6, 2, 6, 2)
        root.addWidget(self._status_label)

        # -- splitter: tree (left) + table (right) --
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # Left: tree
        self._tree = QTreeWidget(splitter)
        self._tree.setHeaderLabel("Data Model Hierarchy")
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.currentItemChanged.connect(self._on_tree_selection_changed)
        splitter.addWidget(self._tree)

        # Right: table
        self._table_model = _TagTableModel(self)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._table_model)
        self._proxy.setFilterKeyColumn(-1)  # filter across all columns

        self._table_view = QTableView(splitter)
        self._table_view.setModel(self._proxy)
        self._table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table_view.setAlternatingRowColors(True)
        self._table_view.setSortingEnabled(True)
        self._table_view.horizontalHeader().setSectionResizeMode(
            _COL_LABEL, QHeaderView.ResizeMode.Stretch
        )
        self._table_view.horizontalHeader().setStretchLastSection(False)
        self._table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self._table_view.addAction(self._action_read_sel)
        self._table_view.addAction(self._action_write)
        self._table_view.addAction(self._action_watchlist)
        self._table_view.selectionModel().selectionChanged.connect(self._on_table_selection)

        splitter.addWidget(self._table_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        root.addWidget(splitter, 1)

    def _wire_signals(self) -> None:
        self._state.session_closed.connect(self._on_session_closed)
        self._state.iec_tags_loaded.connect(self._on_tags_loaded)
        self._state.read_completed.connect(self._on_read_completed)

    # ---- public API -----------------------------------------------------

    def set_session(self, session_id: SessionId | None) -> None:
        """Switch the panel to track *session_id*."""
        self._session_id = session_id
        if session_id is None:
            self._clear()
            self._status_label.setText("No session selected.")
            return

        # If tags are already available (e.g. eagerly fetched on open),
        # populate from existing state.
        info = self._state.session(session_id)
        if info and info.objects:
            self._populate(list(info.objects))
        else:
            self._status_label.setText('Click "Fetch SCL Tags" to download the IED configuration.')

    # ---- slots ----------------------------------------------------------

    def _on_session_closed(self, session_id: str) -> None:
        if session_id == self._session_id:
            self._clear()
            self._status_label.setText("Session closed.")
            self._session_id = None

    def _on_tags_loaded(self, session_id: str, tags: list) -> None:
        if session_id != self._session_id:
            return
        refs: list[ObjectRef] = [t for t in tags if isinstance(t, ObjectRef)]
        self._populate(refs)

    def _on_read_completed(self, session_id: str, result: ReadResult) -> None:
        if session_id != self._session_id:
            return
        self._table_model.record_read(result)

    def _on_fetch_clicked(self) -> None:
        if self._session_id is None:
            QMessageBox.information(self, "No Session", "Please select an active session first.")
            return
        try:
            self._sm.fetch_scl_tags(self._session_id)
            self._status_label.setText("Fetching SCL tags from IED…")
        except KeyError:
            QMessageBox.warning(self, "Session Error", "Session is no longer active.")

    def _on_read_selected(self) -> None:
        if self._session_id is None:
            return
        for ref in self._selected_refs():
            self._sm.read(self._session_id, ref)

    def _on_read_all(self) -> None:
        if self._session_id is None:
            return
        for ref in self._visible_refs():
            self._sm.read(self._session_id, ref)

    def _on_write_clicked(self) -> None:
        refs = self._selected_refs()
        if not refs:
            return
        ref = refs[0]
        self.write_requested.emit(ref)

    def _on_add_watchlist(self) -> None:
        if self._session_id is None:
            return
        for ref in self._selected_refs():
            self.add_to_watchlist_requested.emit(self._session_id, ref)

    def _on_poll_changed(self, idx: int) -> None:
        if self._session_id is None:
            return
        ms = _POLL_MS[idx]
        for ref in self._selected_refs():
            self._sm.set_poll_interval(self._session_id, ref, ms)

    def _on_tree_selection_changed(
        self,
        current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if current is None:
            # Show all
            self._filter_table(None)
            return
        depth = 0
        item: QTreeWidgetItem | None = current
        while item is not None:
            depth += 1
            item = item.parent()

        # depth 1 = LD, depth 2 = LN, depth 3 = DO
        node_data: dict[str, str] | None = current.data(0, Qt.ItemDataRole.UserRole)
        self._filter_table(node_data)

    def _on_table_selection(self) -> None:
        has_sel = bool(self._selected_refs())
        self._action_read_sel.setEnabled(has_sel)
        self._action_watchlist.setEnabled(has_sel)
        if has_sel:
            writable = any(ref.access.value == "rw" for ref in self._selected_refs())
            self._action_write.setEnabled(writable)
        else:
            self._action_write.setEnabled(False)

    # ---- internal helpers -----------------------------------------------

    def _clear(self) -> None:
        self._all_refs = []
        self._table_model.set_refs([])
        self._tree.clear()
        self._action_read_all.setEnabled(False)
        self._action_read_sel.setEnabled(False)
        self._action_write.setEnabled(False)
        self._action_watchlist.setEnabled(False)

    def _populate(self, refs: list[ObjectRef]) -> None:
        """Rebuild tree + table from a new list of ObjectRefs."""
        # Filter to only refs that have IEC 61850 metadata.
        iec_refs = [r for r in refs if "ld_inst" in r.metadata]

        self._all_refs = iec_refs
        self._table_model.set_refs(iec_refs)
        self._build_tree(iec_refs)

        n = len(iec_refs)
        self._status_label.setText(f"{n} data attribute{'s' if n != 1 else ''} loaded from SCL.")
        self._action_read_all.setEnabled(n > 0)

    def _build_tree(self, refs: list[ObjectRef]) -> None:
        """Rebuild the tree widget from the tag list."""
        self._tree.clear()
        if not refs:
            return

        # Group: LD → LN → DO
        ld_map: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for ref in refs:
            m = ref.metadata
            ld = str(m.get("ld_inst", ""))
            ln = str(m.get("ln_ref", ""))
            do = str(m.get("do_name", ""))
            ld_map[ld][ln].add(do)

        for ld_name in sorted(ld_map):
            ld_item = QTreeWidgetItem(self._tree, [ld_name])
            ld_item.setData(0, Qt.ItemDataRole.UserRole, {"ld_inst": ld_name})
            ld_item.setExpanded(True)
            ln_map = ld_map[ld_name]
            for ln_name in sorted(ln_map):
                ln_item = QTreeWidgetItem(ld_item, [ln_name])
                ln_item.setData(
                    0,
                    Qt.ItemDataRole.UserRole,
                    {"ld_inst": ld_name, "ln_ref": ln_name},
                )
                ln_item.setExpanded(True)
                for do_name in sorted(ln_map[ln_name]):
                    do_item = QTreeWidgetItem(ln_item, [do_name])
                    do_item.setData(
                        0,
                        Qt.ItemDataRole.UserRole,
                        {"ld_inst": ld_name, "ln_ref": ln_name, "do_name": do_name},
                    )

    def _filter_table(self, node_data: dict[str, str] | None) -> None:
        """Filter the table to refs matching the tree node selection."""
        if node_data is None:
            self._table_model.set_refs(self._all_refs)
            self._action_read_all.setEnabled(bool(self._all_refs))
            return

        ld = node_data.get("ld_inst")
        ln = node_data.get("ln_ref")
        do = node_data.get("do_name")

        filtered = []
        for ref in self._all_refs:
            m = ref.metadata
            if ld and m.get("ld_inst") != ld:
                continue
            if ln and m.get("ln_ref") != ln:
                continue
            if do and m.get("do_name") != do:
                continue
            filtered.append(ref)

        self._table_model.set_refs(filtered)
        self._action_read_all.setEnabled(bool(filtered))

    def _selected_refs(self) -> list[ObjectRef]:
        """Return ObjectRefs corresponding to selected table rows."""
        indexes = self._table_view.selectionModel().selectedRows()
        refs = []
        for proxy_idx in indexes:
            src_idx = self._proxy.mapToSource(proxy_idx)
            ref = self._table_model.ref_at(src_idx.row())
            if ref is not None:
                refs.append(ref)
        return refs

    def _visible_refs(self) -> list[ObjectRef]:
        """Return all refs currently shown in the table (after tree filter)."""
        return self._table_model.all_refs()
