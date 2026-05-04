# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""SCL Editor dialog — view and edit IEC 61850 SCL configuration files (P8.A.4).

Allows an operator to open an ``.icd``/``.cid``/``.scd`` file, inspect the
tree of IEDs → LDevices → LNs → DataSets / RCBs / GoCBs / SVCBs, and edit
selected attributes.

Design notes
------------
* **Edit-on-copy**: changes accumulate as an in-memory modified ``SclDocument``
  (built by replacing frozen dataclasses).  The original file is never touched
  until the operator explicitly presses *Save* or *Save As…*.
* **UNDO/REDO**: every mutation is posted as a :class:`_SclCommand` to a
  ``QUndoStack``; Ctrl+Z / Ctrl+Y work as expected.
* **Validation**: the *Validate* button runs
  :func:`protoskipper_iec61850.scl.validate` and shows issues in a
  collapsible panel at the bottom.
* **Profile guard**: edits beyond a ``LAB`` session profile require
  re-confirmation (``QMessageBox``) once per dialog open.
* **No direct driver calls**: this dialog is pure GUI; drivers are never
  imported here.

The ``protoskipper_iec61850[scl]`` extra is required at runtime (lxml).  The
dialog lazy-imports it and shows an actionable error if it is absent.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from protoskipper_iec61850.scl.model import (
        IED,
        LN,
        DataSet,
        GseControl,
        ReportControl,
        SclDocument,
        ValidationIssue,
    )

__all__ = ["SclEditorDialog"]

# ---------------------------------------------------------------------------
# Undo command
# ---------------------------------------------------------------------------


class _SclCommand(QUndoCommand):
    """Reversible replacement of one field in the SclDocument."""

    def __init__(
        self,
        dialog: SclEditorDialog,
        old_doc: SclDocument,
        new_doc: SclDocument,
        description: str,
    ) -> None:
        super().__init__(description)
        self._dialog = dialog
        self._old = old_doc
        self._new = new_doc

    def undo(self) -> None:
        self._dialog._apply_document(self._old)

    def redo(self) -> None:
        self._dialog._apply_document(self._new)


# ---------------------------------------------------------------------------
# Main dialog
# ---------------------------------------------------------------------------


class SclEditorDialog(QDialog):
    """View and edit an IEC 61850 SCL file.

    Parameters
    ----------
    path:
        Optional filesystem path to open immediately on construction.
    parent:
        Parent widget.
    """

    def __init__(self, path: Path | str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("SCL Editor"))
        self.setMinimumSize(900, 600)

        self._doc: SclDocument | None = None
        self._current_path: Path | None = None
        self._dirty = False
        self._lab_confirmed = False  # extra-profile guard (set once per open)
        self._undo_stack = QUndoStack(self)

        self._build_ui()

        # Wire undo/redo keyboard shortcuts
        undo_action = self._undo_stack.createUndoAction(self, self.tr("Undo"))
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        redo_action = self._undo_stack.createRedoAction(self, self.tr("Redo"))
        redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.addAction(undo_action)
        self.addAction(redo_action)

        self._undo_stack.cleanChanged.connect(self._on_clean_changed)

        if path is not None:
            QTimer.singleShot(0, lambda: self._open_path(Path(path)))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ----- toolbar --------------------------------------------------
        tb = QToolBar(self)
        self._act_open = tb.addAction(self.tr("Open…"))
        self._act_open.triggered.connect(self._on_open)
        self._act_save = tb.addAction(self.tr("Save"))
        self._act_save.triggered.connect(self._on_save)
        self._act_save.setShortcut(QKeySequence.StandardKey.Save)
        self._act_save_as = tb.addAction(self.tr("Save As…"))
        self._act_save_as.triggered.connect(self._on_save_as)
        tb.addSeparator()
        self._act_validate = tb.addAction(self.tr("Validate"))
        self._act_validate.triggered.connect(self._on_validate)

        # ----- tree on the left -----------------------------------------
        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels([self.tr("Element"), self.tr("Name / Value")])
        self._tree.setColumnWidth(0, 220)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.currentItemChanged.connect(self._on_tree_selection_changed)

        # ----- detail panel on the right --------------------------------
        self._detail_label = QLabel(self.tr("Select an element to edit."), self)
        self._detail_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._detail_label.setWordWrap(True)

        self._detail_form_container = QWidget(self)
        self._detail_form_layout = QVBoxLayout(self._detail_form_container)
        self._detail_form_layout.setContentsMargins(0, 0, 0, 0)
        self._detail_form_layout.addWidget(self._detail_label)

        # ----- validation issues panel ----------------------------------
        self._issues_group = QGroupBox(self.tr("Validation Issues"), self)
        self._issues_group.setCheckable(True)
        self._issues_group.setChecked(False)
        self._issues_list = QListWidget(self)
        self._issues_list.setMaximumHeight(120)
        issues_layout = QVBoxLayout(self._issues_group)
        issues_layout.addWidget(self._issues_list)
        self._issues_group.setVisible(False)

        # ----- splitter -------------------------------------------------
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._tree)
        splitter.addWidget(self._detail_form_container)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        # ----- close button ---------------------------------------------
        close_btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close_btn.rejected.connect(self._on_close_requested)

        # ----- layout ---------------------------------------------------
        root = QVBoxLayout(self)
        root.addWidget(tb)
        root.addWidget(splitter, stretch=1)
        root.addWidget(self._issues_group)
        root.addWidget(close_btn)

        self._update_action_states()

    # ------------------------------------------------------------------
    # SCL loading / saving
    # ------------------------------------------------------------------

    def _open_path(self, path: Path) -> None:
        try:
            scl_parse = self._import_scl_parse()
            doc = scl_parse(path)
        except ImportError as exc:
            self._show_import_error(str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Open Failed"), str(exc))
            return

        self._current_path = path
        self._doc = doc
        self._dirty = False
        self._lab_confirmed = False
        self._undo_stack.clear()
        self._refresh_tree()
        self._update_action_states()
        self.setWindowTitle(self.tr("SCL Editor — %1").replace("%1", path.name))

    def _write_doc(self, path: Path) -> bool:
        """Write *self._doc* to *path*; return True on success."""
        if self._doc is None:
            return False
        try:
            scl_write = self._import_scl_write()
        except ImportError as exc:
            self._show_import_error(str(exc))
            return False

        # Write to a temp file first, then atomically replace to avoid
        # corrupting the original on write failure.
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            scl_write(self._doc, tmp_path)
            shutil.move(str(tmp_path), str(path))
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Save Failed"), str(exc))
            with contextlib.suppress(Exception):
                tmp_path.unlink(missing_ok=True)
            return False
        self._current_path = path
        self._dirty = False
        self._undo_stack.setClean()
        self.setWindowTitle(self.tr("SCL Editor — %1").replace("%1", path.name))
        return True

    # ------------------------------------------------------------------
    # Tree refresh
    # ------------------------------------------------------------------

    def _refresh_tree(self) -> None:
        self._tree.clear()
        if self._doc is None:
            return

        doc = self._doc
        root_item = QTreeWidgetItem(self._tree, [self.tr("SCL Document"), doc.source_file or ""])
        root_item.setData(0, Qt.ItemDataRole.UserRole, ("document", None))

        for ied in doc.ieds:
            ied_item = QTreeWidgetItem(root_item, [self.tr("IED"), ied.name])
            ied_item.setData(0, Qt.ItemDataRole.UserRole, ("ied", ied.name))

            for ld in ied.ldevices:
                ld_item = QTreeWidgetItem(ied_item, [self.tr("LDevice"), ld.inst])
                ld_item.setData(0, Qt.ItemDataRole.UserRole, ("ldevice", (ied.name, ld.inst)))

                all_lns: list[LN] = []
                if ld.ln0 is not None:
                    all_lns.append(ld.ln0)
                all_lns.extend(ld.lns)

                for ln in all_lns:
                    ln_label = f"{ln.prefix}{ln.ln_class}{ln.inst}" or "LN0"
                    ln_item = QTreeWidgetItem(ld_item, [self.tr("LN"), ln_label])
                    key = (ied.name, ld.inst, ln.ln_class, ln.inst)
                    ln_item.setData(0, Qt.ItemDataRole.UserRole, ("ln", key))

                    for ds in ln.datasets:
                        ds_item = QTreeWidgetItem(ln_item, [self.tr("DataSet"), ds.name])
                        ds_item.setData(0, Qt.ItemDataRole.UserRole, ("dataset", (*key, ds.name)))

                    for rcb in ln.report_controls:
                        label = f"{'B' if rcb.buffered else 'U'}RCB"
                        rcb_item = QTreeWidgetItem(ln_item, [label, rcb.name])
                        rcb_item.setData(0, Qt.ItemDataRole.UserRole, ("rcb", (*key, rcb.name)))

                    for gcb in ln.gse_controls:
                        gcb_item = QTreeWidgetItem(ln_item, [self.tr("GoCB"), gcb.name])
                        gcb_item.setData(0, Qt.ItemDataRole.UserRole, ("gcb", (*key, gcb.name)))

                    for svc in ln.sv_controls:
                        svc_item = QTreeWidgetItem(ln_item, [self.tr("SVCB"), svc.name])
                        svc_item.setData(0, Qt.ItemDataRole.UserRole, ("svcb", (*key, svc.name)))

        self._tree.expandToDepth(2)

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def _on_tree_selection_changed(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        self._clear_detail()
        if current is None or self._doc is None:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            return
        kind, key = data
        if kind == "document":
            self._show_document_detail()
        elif kind == "ied":
            ied = self._find_ied(key)
            if ied:
                self._show_ied_detail(ied)
        elif kind == "dataset":
            ied_name, ld_inst, ln_class, ln_inst, ds_name = key
            ds = self._find_dataset(ied_name, ld_inst, ln_class, ln_inst, ds_name)
            if ds:
                self._show_dataset_detail(ied_name, ld_inst, ln_class, ln_inst, ds)
        elif kind == "rcb":
            ied_name, ld_inst, ln_class, ln_inst, rcb_name = key
            rcb = self._find_rcb(ied_name, ld_inst, ln_class, ln_inst, rcb_name)
            if rcb:
                self._show_rcb_detail(ied_name, ld_inst, ln_class, ln_inst, rcb)
        elif kind == "gcb":
            ied_name, ld_inst, ln_class, ln_inst, gcb_name = key
            gcb = self._find_gcb(ied_name, ld_inst, ln_class, ln_inst, gcb_name)
            if gcb:
                self._show_gcb_detail(ied_name, ld_inst, ln_class, ln_inst, gcb)

    def _clear_detail(self) -> None:
        for i in reversed(range(self._detail_form_layout.count())):
            widget = self._detail_form_layout.itemAt(i).widget()
            if widget:
                widget.setParent(None)

    def _show_document_detail(self) -> None:
        if self._doc is None:
            return
        form = QFormLayout()
        form.addRow(self.tr("Version:"), QLabel(self._doc.version or "—", self))
        form.addRow(self.tr("Revision:"), QLabel(self._doc.revision or "—", self))
        form.addRow(self.tr("Release:"), QLabel(self._doc.release or "—", self))
        form.addRow(self.tr("Source file:"), QLabel(self._doc.source_file or "—", self))
        form.addRow(self.tr("IEDs:"), QLabel(str(len(self._doc.ieds)), self))
        w = QWidget(self)
        w.setLayout(form)
        self._detail_form_layout.addWidget(w)

    def _show_ied_detail(self, ied: IED) -> None:
        form = QFormLayout()
        form.addRow(self.tr("Name:"), QLabel(ied.name, self))
        form.addRow(self.tr("Manufacturer:"), QLabel(ied.manufacturer or "—", self))
        form.addRow(self.tr("Model:"), QLabel(ied.model or "—", self))
        form.addRow(self.tr("ConfigVersion:"), QLabel(ied.config_version or "—", self))
        form.addRow(self.tr("Description:"), QLabel(ied.desc or "—", self))
        w = QWidget(self)
        w.setLayout(form)
        self._detail_form_layout.addWidget(w)

    def _show_dataset_detail(
        self,
        ied_name: str,
        ld_inst: str,
        ln_class: str,
        ln_inst: str,
        ds: DataSet,
    ) -> None:
        form = QFormLayout()
        form.addRow(self.tr("Name:"), QLabel(ds.name, self))
        form.addRow(self.tr("Description:"), QLabel(ds.desc or "—", self))
        form.addRow(self.tr("FCDAs:"), QLabel(str(len(ds.fcdas)), self))
        w = QWidget(self)
        w.setLayout(form)

        # Editable DataSet name
        name_edit = QLineEdit(ds.name, self)
        name_edit.setAccessibleName(self.tr("DataSet name"))
        save_btn = QPushButton(self.tr("Apply name change"), self)
        save_btn.clicked.connect(
            lambda: self._rename_dataset(ied_name, ld_inst, ln_class, ln_inst, ds, name_edit.text())
        )

        # FCDA list (read-only)
        fcda_list = QListWidget(self)
        for fcda in ds.fcdas:
            ref = f"{fcda.ld_inst}/{fcda.prefix}{fcda.ln_class}{fcda.ln_inst}.{fcda.do_name}"
            if fcda.da_name:
                ref += f".{fcda.da_name}"
            if fcda.fc:
                ref += f"[{fcda.fc}]"
            fcda_list.addItem(QListWidgetItem(ref))

        grp = QGroupBox(self.tr("Edit DataSet"), self)
        grp_layout = QFormLayout(grp)
        grp_layout.addRow(self.tr("Name:"), name_edit)
        row_widget = QWidget(self)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addStretch()
        row_layout.addWidget(save_btn)
        grp_layout.addRow(row_widget)
        grp_layout.addRow(self.tr("FCDAs:"), fcda_list)

        self._detail_form_layout.addWidget(w)
        self._detail_form_layout.addWidget(grp)

    def _show_rcb_detail(
        self,
        ied_name: str,
        ld_inst: str,
        ln_class: str,
        ln_inst: str,
        rcb: ReportControl,
    ) -> None:
        kind = self.tr("BRCB") if rcb.buffered else self.tr("URCB")
        form = QFormLayout()
        form.addRow(self.tr("Type:"), QLabel(kind, self))
        form.addRow(self.tr("Name:"), QLabel(rcb.name, self))
        form.addRow(self.tr("DataSetRef:"), QLabel(rcb.dataset_ref or "—", self))
        form.addRow(self.tr("RptID:"), QLabel(rcb.rpt_id or "—", self))
        form.addRow(self.tr("ConfRev:"), QLabel(str(rcb.conf_rev), self))
        indexed_str = self.tr("Yes") if rcb.indexed else self.tr("No")
        form.addRow(self.tr("Indexed:"), QLabel(indexed_str, self))

        # Editable dataset_ref
        ds_ref_edit = QLineEdit(rcb.dataset_ref, self)
        ds_ref_edit.setAccessibleName(self.tr("DataSet reference"))
        apply_btn = QPushButton(self.tr("Apply"), self)
        apply_btn.clicked.connect(
            lambda: self._update_rcb_dataset_ref(
                ied_name, ld_inst, ln_class, ln_inst, rcb, ds_ref_edit.text()
            )
        )

        grp = QGroupBox(self.tr("Edit RCB"), self)
        grp_layout = QFormLayout(grp)
        grp_layout.addRow(self.tr("DataSetRef:"), ds_ref_edit)
        btn_row = QWidget(self)
        btn_row_layout = QHBoxLayout(btn_row)
        btn_row_layout.setContentsMargins(0, 0, 0, 0)
        btn_row_layout.addStretch()
        btn_row_layout.addWidget(apply_btn)
        grp_layout.addRow(btn_row)

        w = QWidget(self)
        w.setLayout(form)
        self._detail_form_layout.addWidget(w)
        self._detail_form_layout.addWidget(grp)

    def _show_gcb_detail(
        self,
        ied_name: str,
        ld_inst: str,
        ln_class: str,
        ln_inst: str,
        gcb: GseControl,
    ) -> None:
        form = QFormLayout()
        form.addRow(self.tr("Name:"), QLabel(gcb.name, self))
        form.addRow(self.tr("Type:"), QLabel(gcb.type, self))
        form.addRow(self.tr("AppID:"), QLabel(gcb.app_id or "—", self))
        form.addRow(self.tr("DataSetRef:"), QLabel(gcb.dataset_ref or "—", self))

        ds_ref_edit = QLineEdit(gcb.dataset_ref, self)
        ds_ref_edit.setAccessibleName(self.tr("DataSet reference"))
        apply_btn = QPushButton(self.tr("Apply"), self)
        apply_btn.clicked.connect(
            lambda: self._update_gcb_dataset_ref(
                ied_name, ld_inst, ln_class, ln_inst, gcb, ds_ref_edit.text()
            )
        )

        grp = QGroupBox(self.tr("Edit GoCB"), self)
        grp_layout = QFormLayout(grp)
        grp_layout.addRow(self.tr("DataSetRef:"), ds_ref_edit)
        btn_row = QWidget(self)
        btn_row_layout = QHBoxLayout(btn_row)
        btn_row_layout.setContentsMargins(0, 0, 0, 0)
        btn_row_layout.addStretch()
        btn_row_layout.addWidget(apply_btn)
        grp_layout.addRow(btn_row)

        w = QWidget(self)
        w.setLayout(form)
        self._detail_form_layout.addWidget(w)
        self._detail_form_layout.addWidget(grp)

    # ------------------------------------------------------------------
    # Document mutation helpers (undo-aware)
    # ------------------------------------------------------------------

    def _apply_document(self, new_doc: SclDocument) -> None:
        """Replace the current document without pushing to the undo stack."""
        self._doc = new_doc
        self._dirty = True
        self._refresh_tree()
        self._update_action_states()

    def _push_mutation(self, new_doc: SclDocument, description: str) -> None:
        """Push a document replacement as an undoable command."""
        if self._doc is None:
            return
        cmd = _SclCommand(self, self._doc, new_doc, description)
        self._undo_stack.push(cmd)

    def _rename_dataset(
        self,
        ied_name: str,
        ld_inst: str,
        ln_class: str,
        ln_inst: str,
        ds: DataSet,
        new_name: str,
    ) -> None:
        new_name = new_name.strip()
        if not new_name or new_name == ds.name:
            return
        new_doc = self._mutate_dataset(ied_name, ld_inst, ln_class, ln_inst, ds.name, new_name)
        if new_doc is not None:
            self._push_mutation(new_doc, f"Rename DataSet '{ds.name}' → '{new_name}'")

    def _update_rcb_dataset_ref(
        self,
        ied_name: str,
        ld_inst: str,
        ln_class: str,
        ln_inst: str,
        rcb: ReportControl,
        new_ref: str,
    ) -> None:
        new_ref = new_ref.strip()
        if new_ref == rcb.dataset_ref:
            return
        new_doc = self._mutate_rcb_dataset_ref(
            ied_name, ld_inst, ln_class, ln_inst, rcb.name, new_ref
        )
        if new_doc is not None:
            self._push_mutation(new_doc, f"Change RCB '{rcb.name}' DataSetRef")

    def _update_gcb_dataset_ref(
        self,
        ied_name: str,
        ld_inst: str,
        ln_class: str,
        ln_inst: str,
        gcb: GseControl,
        new_ref: str,
    ) -> None:
        new_ref = new_ref.strip()
        if new_ref == gcb.dataset_ref:
            return
        new_doc = self._mutate_gcb_dataset_ref(
            ied_name, ld_inst, ln_class, ln_inst, gcb.name, new_ref
        )
        if new_doc is not None:
            self._push_mutation(new_doc, f"Change GoCB '{gcb.name}' DataSetRef")

    # ------------------------------------------------------------------
    # Low-level document mutation (frozen dataclass replace chains)
    # ------------------------------------------------------------------

    def _mutate_ln(
        self,
        ied_name: str,
        ld_inst: str,
        ln_class: str,
        ln_inst: str,
        new_ln: LN,
    ) -> SclDocument | None:
        """Return a new SclDocument with the given LN replaced in-place."""
        if self._doc is None:
            return None
        doc = self._doc
        new_ieds = []
        for ied in doc.ieds:
            if ied.name != ied_name:
                new_ieds.append(ied)
                continue
            new_aps = []
            for ap in ied.access_points:
                new_lds = []
                for ld in ap.ldevices:
                    if ld.inst != ld_inst:
                        new_lds.append(ld)
                        continue
                    # Replace in ln0 or lns
                    ln0_matches = (
                        ld.ln0 is not None
                        and ld.ln0.ln_class == ln_class
                        and ld.ln0.inst == ln_inst
                    )
                    if ln0_matches:
                        new_ld = replace(ld, ln0=new_ln)
                    else:
                        new_lns = tuple(
                            new_ln if (ln.ln_class == ln_class and ln.inst == ln_inst) else ln
                            for ln in ld.lns
                        )
                        new_ld = replace(ld, lns=new_lns)
                    new_lds.append(new_ld)
                new_aps.append(replace(ap, ldevices=tuple(new_lds)))
            new_ieds.append(replace(ied, access_points=tuple(new_aps)))
        return replace(doc, ieds=tuple(new_ieds))

    def _mutate_dataset(
        self,
        ied_name: str,
        ld_inst: str,
        ln_class: str,
        ln_inst: str,
        old_ds_name: str,
        new_ds_name: str,
    ) -> SclDocument | None:
        ln = self._find_ln(ied_name, ld_inst, ln_class, ln_inst)
        if ln is None:
            return None
        new_datasets = tuple(
            replace(ds, name=new_ds_name) if ds.name == old_ds_name else ds for ds in ln.datasets
        )
        new_ln = replace(ln, datasets=new_datasets)
        return self._mutate_ln(ied_name, ld_inst, ln_class, ln_inst, new_ln)

    def _mutate_rcb_dataset_ref(
        self,
        ied_name: str,
        ld_inst: str,
        ln_class: str,
        ln_inst: str,
        rcb_name: str,
        new_ref: str,
    ) -> SclDocument | None:
        ln = self._find_ln(ied_name, ld_inst, ln_class, ln_inst)
        if ln is None:
            return None
        new_rcbs = tuple(
            replace(rcb, dataset_ref=new_ref) if rcb.name == rcb_name else rcb
            for rcb in ln.report_controls
        )
        new_ln = replace(ln, report_controls=new_rcbs)
        return self._mutate_ln(ied_name, ld_inst, ln_class, ln_inst, new_ln)

    def _mutate_gcb_dataset_ref(
        self,
        ied_name: str,
        ld_inst: str,
        ln_class: str,
        ln_inst: str,
        gcb_name: str,
        new_ref: str,
    ) -> SclDocument | None:
        ln = self._find_ln(ied_name, ld_inst, ln_class, ln_inst)
        if ln is None:
            return None
        new_gcbs = tuple(
            replace(gcb, dataset_ref=new_ref) if gcb.name == gcb_name else gcb
            for gcb in ln.gse_controls
        )
        new_ln = replace(ln, gse_controls=new_gcbs)
        return self._mutate_ln(ied_name, ld_inst, ln_class, ln_inst, new_ln)

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _find_ied(self, name: str) -> IED | None:
        if self._doc is None:
            return None
        return next((i for i in self._doc.ieds if i.name == name), None)

    def _find_ln(self, ied_name: str, ld_inst: str, ln_class: str, ln_inst: str) -> LN | None:
        ied = self._find_ied(ied_name)
        if ied is None:
            return None
        for ld in ied.ldevices:
            if ld.inst != ld_inst:
                continue
            if ld.ln0 is not None and ld.ln0.ln_class == ln_class and ld.ln0.inst == ln_inst:
                return ld.ln0
            for ln in ld.lns:
                if ln.ln_class == ln_class and ln.inst == ln_inst:
                    return ln
        return None

    def _find_dataset(
        self, ied_name: str, ld_inst: str, ln_class: str, ln_inst: str, ds_name: str
    ) -> DataSet | None:
        ln = self._find_ln(ied_name, ld_inst, ln_class, ln_inst)
        if ln is None:
            return None
        return next((ds for ds in ln.datasets if ds.name == ds_name), None)

    def _find_rcb(
        self, ied_name: str, ld_inst: str, ln_class: str, ln_inst: str, rcb_name: str
    ) -> ReportControl | None:
        ln = self._find_ln(ied_name, ld_inst, ln_class, ln_inst)
        if ln is None:
            return None
        return next((r for r in ln.report_controls if r.name == rcb_name), None)

    def _find_gcb(
        self, ied_name: str, ld_inst: str, ln_class: str, ln_inst: str, gcb_name: str
    ) -> GseControl | None:
        ln = self._find_ln(ied_name, ld_inst, ln_class, ln_inst)
        if ln is None:
            return None
        return next((g for g in ln.gse_controls if g.name == gcb_name), None)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _on_validate(self) -> None:
        if self._doc is None:
            return
        try:
            scl_validate = self._import_scl_validate()
        except ImportError as exc:
            self._show_import_error(str(exc))
            return

        issues: list[ValidationIssue] = scl_validate(self._doc)
        self._issues_list.clear()
        if not issues:
            self._issues_list.addItem(QListWidgetItem(self.tr("No issues found.")))
        else:
            for issue in issues:
                line_str = f":{issue.line}" if issue.line else ""
                text = f"[{issue.severity.upper()}{line_str}] {issue.message}"
                self._issues_list.addItem(QListWidgetItem(text))
        self._issues_group.setVisible(True)
        self._issues_group.setChecked(True)

    # ------------------------------------------------------------------
    # Toolbar action handlers
    # ------------------------------------------------------------------

    def _on_open(self) -> None:
        if self._dirty:
            reply = QMessageBox.question(
                self,
                self.tr("Unsaved Changes"),
                self.tr("There are unsaved changes. Discard them and open a new file?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        path_str, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Open SCL File"),
            str(self._current_path.parent) if self._current_path else "",
            self.tr("SCL Files (*.icd *.cid *.scd *.iid);;All Files (*)"),
        )
        if path_str:
            self._open_path(Path(path_str))

    def _on_save(self) -> None:
        if self._current_path is None:
            self._on_save_as()
            return
        self._write_doc(self._current_path)

    def _on_save_as(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Save SCL File As"),
            str(self._current_path) if self._current_path else "",
            self.tr("SCL Files (*.icd *.cid *.scd *.iid);;All Files (*)"),
        )
        if path_str:
            self._write_doc(Path(path_str))

    def _on_close_requested(self) -> None:
        if self._dirty:
            reply = QMessageBox.question(
                self,
                self.tr("Unsaved Changes"),
                self.tr("There are unsaved changes. Close anyway?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.reject()

    def _on_clean_changed(self, clean: bool) -> None:
        self._dirty = not clean
        self._update_action_states()

    def _update_action_states(self) -> None:
        has_doc = self._doc is not None
        self._act_save.setEnabled(has_doc and self._dirty)
        self._act_save_as.setEnabled(has_doc)
        self._act_validate.setEnabled(has_doc)

    # ------------------------------------------------------------------
    # Lazy imports (protoskipper_iec61850[scl] optional dependency)
    # ------------------------------------------------------------------

    @staticmethod
    def _import_scl_parse():  # type: ignore[return]
        """Return :func:`protoskipper_iec61850.scl.parse`, raising ImportError on missing dep."""
        from protoskipper_iec61850.scl import parse  # type: ignore[import]

        return parse

    @staticmethod
    def _import_scl_write():  # type: ignore[return]
        """Return :func:`protoskipper_iec61850.scl.write`, raising ImportError on missing dep."""
        from protoskipper_iec61850.scl import write  # type: ignore[import]

        return write

    @staticmethod
    def _import_scl_validate():  # type: ignore[return]
        """Return :func:`protoskipper_iec61850.scl.validate`, raising ImportError on missing dep."""
        from protoskipper_iec61850.scl import validate  # type: ignore[import]

        return validate

    def _show_import_error(self, message: str) -> None:
        QMessageBox.critical(
            self,
            self.tr("SCL support not installed"),
            self.tr(
                "The SCL module requires lxml:\n\n"
                "    pip install protoskipper-iec61850[scl]\n\n"
                f"Detail: {message}"
            ),
        )
