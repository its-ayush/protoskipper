# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Substation overview panel — P8.E.

Displays a tile grid of all IEDs declared in the active SCD, with live
colour-coded health indicators (MMS online, GOOSE green/amber/red, SV ok).

Architecture
------------
* :class:`SubstationOverviewPanel` is a ``QWidget`` embedded in the main
  window's central tab area.
* It loads its tile arrangement from a :class:`BenchLayout` (persisted in
  the setup JSON), or auto-populates from the SCD's ``<IED>`` elements.
* A ``QTimer`` at 1 Hz calls :class:`BenchStatusUpdater.poll`; each health
  update is routed back to the GUI thread via ``Qt.QueuedConnection``.
* Clicking a tile emits :attr:`ied_activated` so the main window can focus
  or create the MMS session for that IED.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_log = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 1000


def _make_tile_frame(ied_name: str) -> _IedTileWidget:
    return _IedTileWidget(ied_name)


# ---------------------------------------------------------------------------
# IED tile widget
# ---------------------------------------------------------------------------


class _IedTileWidget(QFrame):
    """Single IED tile displayed in the bench overview grid."""

    clicked: Signal = Signal(str)  # ied_name

    def __init__(self, ied_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ied_name = ied_name
        self.setFrameStyle(QFrame.Shape.Box | QFrame.Shadow.Raised)
        self.setLineWidth(2)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"IED: {ied_name}\nClick to open/focus session")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setMinimumSize(140, 90)
        self.setMaximumSize(200, 120)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        name_lbl = QLabel(ied_name)
        font = QFont()
        font.setBold(True)
        name_lbl.setFont(font)
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name_lbl)

        # Status row
        status_row = QHBoxLayout()
        self._mms_led = _LedLabel("MMS")
        self._goose_led = _LedLabel("GOOSE")
        self._sv_led = _LedLabel("SV")
        status_row.addWidget(self._mms_led)
        status_row.addWidget(self._goose_led)
        status_row.addWidget(self._sv_led)
        layout.addLayout(status_row)

        self._detail_lbl = QLabel("")
        self._detail_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail_lbl.setWordWrap(True)
        layout.addWidget(self._detail_lbl)

        self._set_background("#444444")

    def update_health(self, health: object) -> None:
        """Apply a :class:`~protoskipper_iec61850.bench_layout.IedHealth` snapshot."""
        self._mms_led.set_colour("#00aa44" if health.mms_online else "#cc0000")
        if health.goose_red > 0:
            goose_col = "#cc0000"
        elif health.goose_amber > 0:
            goose_col = "#ffcc00"
        elif health.goose_green > 0:
            goose_col = "#00aa44"
        else:
            goose_col = "#666666"  # no expected streams
        self._goose_led.set_colour(goose_col)
        self._sv_led.set_colour("#00aa44" if health.sv_ok else "#666666")

        self._set_background(health.overall_colour)
        g, a, r = health.goose_green, health.goose_amber, health.goose_red
        self._detail_lbl.setText(f"G:{g} A:{a} R:{r}" if (g + a + r) > 0 else "no GOOSE")

    def mousePressEvent(self, event: object) -> None:  # type: ignore[override]
        self.clicked.emit(self.ied_name)

    def _set_background(self, colour: str) -> None:
        self.setStyleSheet(f"_IedTileWidget {{ background-color: {colour}; border-radius: 6px; }}")


class _LedLabel(QLabel):
    """Small coloured LED indicator with a text label."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._colour = "#666666"
        self._apply()

    def set_colour(self, colour: str) -> None:
        self._colour = colour
        self._apply()

    def _apply(self) -> None:
        self.setStyleSheet(
            f"QLabel {{ background-color: {self._colour}; "
            f"color: white; border-radius: 4px; padding: 2px 4px; }}"
        )


# ---------------------------------------------------------------------------
# Public panel
# ---------------------------------------------------------------------------


class SubstationOverviewPanel(QWidget):
    """Substation overview — live IED tile grid (P8.E).

    Signals
    -------
    ied_activated:
        Emitted when the user clicks a tile, carrying the IED name.
    """

    ied_activated: Signal = Signal(str)
    _health_update = Signal(object)  # carries IedHealth, internal

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._tiles: dict[str, _IedTileWidget] = {}
        self._updater: object | None = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._do_poll)
        self._layout_data: object | None = None  # BenchLayout

        self._health_update.connect(self._apply_health, Qt.ConnectionType.QueuedConnection)
        self._build_ui()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_from_scl(self, scl_doc: object, scd_path: str = "") -> None:
        """Populate tiles from a parsed SCL document.

        Parameters
        ----------
        scl_doc:
            Parsed lxml Element (the root ``<SCL>`` element) or a list of
            IED name strings.  If a list is passed, it is used directly.
        scd_path:
            Optional path string for labelling.
        """
        from protoskipper_iec61850.bench_layout import BenchLayout, IedTileSpec

        if isinstance(scl_doc, list):
            ied_names = scl_doc
        else:
            # Extract IED names from lxml element
            ns = {"s": "http://www.iec.ch/61850/2003/SCL"}
            ied_names = [e.get("name", "?") for e in scl_doc.findall(".//s:IED", ns)]

        layout = BenchLayout(scd_path=scd_path)
        for i, name in enumerate(ied_names):
            layout.tiles[name] = IedTileSpec(ied_name=name, x=i % 5, y=i // 5)
        self._layout_data = layout
        self._rebuild_tiles(layout)

    def set_updater(
        self,
        get_mms_last_seen: object,
        get_goose_last_seen: object,
        get_sv_last_seen: object,
    ) -> None:
        """Attach live-status callbacks and start the poll timer.

        Parameters
        ----------
        get_mms_last_seen:
            ``(ied_name: str) -> float`` — Unix time of last MMS keep-alive.
        get_goose_last_seen:
            ``(go_cb_ref: str) -> float`` — Unix time of last GOOSE frame.
        get_sv_last_seen:
            ``(sv_id: str) -> float`` — Unix time of last SV sample.
        """
        from protoskipper_iec61850.bench_layout import BenchStatusUpdater

        if self._layout_data is None:
            return

        self._updater = BenchStatusUpdater(
            layout=self._layout_data,
            get_mms_last_seen=get_mms_last_seen,
            get_goose_last_seen=get_goose_last_seen,
            get_sv_last_seen=get_sv_last_seen,
            on_health=lambda h: self._health_update.emit(h),
        )
        self._poll_timer.start()

    def stop_updates(self) -> None:
        """Stop the poll timer and clear the updater."""
        self._poll_timer.stop()
        self._updater = None

    def save_layout(self, path: object) -> None:
        """Persist the current tile arrangement to *path* (Path)."""
        from protoskipper_iec61850.bench_layout import save_bench_layout

        if self._layout_data is not None:
            save_bench_layout(self._layout_data, path)  # type: ignore[arg-type]

    def load_layout(self, path: object) -> None:
        """Load tile arrangement from *path* and refresh the grid."""
        from protoskipper_iec61850.bench_layout import load_bench_layout

        layout = load_bench_layout(path)  # type: ignore[arg-type]
        self._layout_data = layout
        self._rebuild_tiles(layout)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(8, 8, 8, 8)

        # Toolbar
        toolbar = QHBoxLayout()
        self._title_lbl = QLabel("No SCD loaded")
        toolbar.addWidget(self._title_lbl)
        toolbar.addStretch()

        btn_save = QPushButton(self.tr("Save layout…"))
        btn_save.clicked.connect(self._on_save_layout)
        toolbar.addWidget(btn_save)

        btn_load = QPushButton(self.tr("Load layout…"))
        btn_load.clicked.connect(self._on_load_layout)
        toolbar.addWidget(btn_load)

        vbox.addLayout(toolbar)

        # Scroll area with tile grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._grid_widget = QWidget()
        self._grid = QGridLayout(self._grid_widget)
        self._grid.setSpacing(8)
        scroll.setWidget(self._grid_widget)
        vbox.addWidget(scroll)

    def _rebuild_tiles(self, layout: object) -> None:
        """Clear the grid and rebuild from *layout*."""
        # Remove old tiles
        for tile in self._tiles.values():
            tile.setParent(None)  # type: ignore[arg-type]
        self._tiles.clear()

        for col_idx in range(self._grid.columnCount()):
            self._grid.setColumnMinimumWidth(col_idx, 0)

        # Add new tiles
        for ied_name, spec in layout.tiles.items():  # type: ignore[union-attr]
            tile = _IedTileWidget(ied_name, self._grid_widget)
            tile.clicked.connect(self.ied_activated)
            self._grid.addWidget(tile, spec.y, spec.x)
            self._tiles[ied_name] = tile

        path = getattr(layout, "scd_path", "")
        self._title_lbl.setText(f"SCD: {path}" if path else "Bench Overview")

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @Slot()
    def _do_poll(self) -> None:
        if self._updater is not None:
            try:
                self._updater.poll()  # type: ignore[union-attr]
            except Exception:
                _log.debug("BenchStatusUpdater.poll raised", exc_info=True)

    @Slot(object)
    def _apply_health(self, health: object) -> None:
        tile = self._tiles.get(health.ied_name)  # type: ignore[union-attr]
        if tile is not None:
            tile.update_health(health)

    @Slot()
    def _on_save_layout(self) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Save Bench Layout"),
            "",
            "Bench Layout (*.bench-layout.json);;All files (*)",
        )
        if not path_str:
            return
        from pathlib import Path

        try:
            self.save_layout(Path(path_str))
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Save failed"), str(exc))

    @Slot()
    def _on_load_layout(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Load Bench Layout"),
            "",
            "Bench Layout (*.bench-layout.json);;JSON files (*.json);;All files (*)",
        )
        if not path_str:
            return
        from pathlib import Path

        try:
            self.load_layout(Path(path_str))
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Load failed"), str(exc))
