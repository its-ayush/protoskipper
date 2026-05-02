# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""ProtoSkipper main window - the full assembled application.

This file is mostly wiring. Each piece is documented separately:

* :doc:`/docs/GUI_ARCHITECTURE` for the overall design.
* :class:`protoskipper.gui.services.SessionManager` for the threading model.
* :class:`protoskipper.gui.services.ApplicationState` for the state model.

The MainWindow owns exactly one of:
ApplicationState, SessionManager, GuiConfirmHandler. Panels and dialogs
receive these through constructor injection.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
)

from protoskipper import __version__
from protoskipper.core.audit import verify_log
from protoskipper.core.capture.pcapng import read_pcapng
from protoskipper.core.driver import DeviceRef, ObjectRef, SessionProfile, WriteIntent
from protoskipper.core.plugin_loader import load_protocol_drivers
from protoskipper.gui.dialogs import (
    AuditViewDialog,
    NewConnectionDialog,
    PreferencesDialog,
    ProbeNetworkDialog,
    ProbeSelection,
    SafetyConfirmDialog,
)
from protoskipper.gui.panels import (
    DeviceTreePanel,
    ObjectBrowserPanel,
    PacketViewPanel,
    SessionStatusPanel,
    WatchlistPanel,
)
from protoskipper.gui.services import (
    ApplicationState,
    GuiConfirmHandler,
    SessionManager,
)
from protoskipper.gui.services.types import CapturedFrame as GuiFrame
from protoskipper.gui.services.types import Direction, SessionId
from protoskipper.gui.services.write_flow import WriteFlowController
from protoskipper.gui.theme import apply_app_theme, apply_density

_logger = logging.getLogger(__name__)


def _default_audit_dir() -> Path:
    """Where audit logs are written by default. Per-OS user data dir."""
    return Path.home() / ".protoskipper" / "audit"


class MainWindow(QMainWindow):
    """ProtoSkipper main window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"ProtoSkipper {__version__}")
        self.resize(1500, 920)

        # ---- core services ----
        self._state = ApplicationState(parent=self)
        self._audit_dir = _default_audit_dir()
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._confirm_handler = GuiConfirmHandler(
            dialog_factory=self._safety_dialog_factory,
            parent=self,
        )
        self._session_manager = SessionManager(
            state=self._state,
            confirm_handler=self._confirm_handler,
            audit_dir=self._audit_dir,
            parent=self,
        )
        self._write_flow = WriteFlowController(
            state=self._state,
            session_manager=self._session_manager,
            parent=self,
        )

        # Capture state tracking (per-window, not per-session).
        self._capture_active: bool = False

        # ---- ui ----
        self._build_actions()
        self._build_toolbar()
        self._build_panels()
        self._build_status_bar()
        self._wire_signals()

        # Restore persisted layout (window geometry + dock state).
        self._restore_settings()

        # P3.D: Apply saved theme and density (must run after _build_actions
        # so the View menu check states exist to update).
        self._apply_theme(PreferencesDialog.saved_theme(), _save=False)
        self._apply_density(PreferencesDialog.saved_density() == "Compact", _save=False)

        # P3.E.1: Show welcome dialog on first ever launch.
        self._check_first_run()

    # ---- construction ---------------------------------------------------

    def _build_actions(self) -> None:
        self._action_probe_network = QAction("Probe Network…", self)
        self._action_probe_network.setShortcut("Ctrl+P")
        self._action_probe_network.triggered.connect(self._open_probe_dialog)

        self._action_new_connection = QAction("New Connection…", self)
        self._action_new_connection.setShortcut(QKeySequence.StandardKey.New)
        self._action_new_connection.triggered.connect(lambda: self._open_new_connection_dialog())

        self._action_disconnect = QAction("Disconnect Selected", self)
        self._action_disconnect.setEnabled(False)
        self._action_disconnect.triggered.connect(self._disconnect_current_session)

        self._action_quit = QAction("Quit", self)
        self._action_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self._action_quit.triggered.connect(self.close)

        self._action_about = QAction("About ProtoSkipper", self)
        self._action_about.triggered.connect(self._show_about)

        # Menu bar
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        file_menu.addAction(self._action_probe_network)
        file_menu.addAction(self._action_new_connection)
        file_menu.addSeparator()

        self._action_close_session = QAction(self.tr("Close Session"), self)
        self._action_close_session.setShortcut("Ctrl+W")
        self._action_close_session.setEnabled(False)
        self._action_close_session.triggered.connect(self._disconnect_current_session)

        self._action_close_all_sessions = QAction(self.tr("Close All Sessions"), self)
        self._action_close_all_sessions.setShortcut("Ctrl+Shift+W")
        self._action_close_all_sessions.setEnabled(False)
        self._action_close_all_sessions.triggered.connect(self._close_all_sessions)

        file_menu.addAction(self._action_close_session)
        file_menu.addAction(self._action_close_all_sessions)
        file_menu.addAction(self._action_disconnect)
        file_menu.addSeparator()
        file_menu.addAction(self._action_quit)

        # Capture menu (P2.A.2)
        self._action_capture_start = QAction("&Start Capture", self)
        self._action_capture_start.setShortcut("Ctrl+Shift+R")
        self._action_capture_start.triggered.connect(self._on_capture_start)

        self._action_capture_stop = QAction("S&top Capture", self)
        self._action_capture_stop.setShortcut("Ctrl+Shift+T")
        self._action_capture_stop.setEnabled(False)
        self._action_capture_stop.triggered.connect(self._on_capture_stop)

        self._action_capture_save = QAction("&Save Capture…", self)
        self._action_capture_save.setShortcut("Ctrl+Shift+S")
        self._action_capture_save.setEnabled(False)
        self._action_capture_save.triggered.connect(self._on_capture_save)

        self._action_capture_open = QAction("&Open Capture…", self)
        self._action_capture_open.setShortcut("Ctrl+Shift+O")
        self._action_capture_open.triggered.connect(self._on_capture_open)

        capture_menu = menu.addMenu("&Capture")
        capture_menu.addAction(self._action_capture_start)
        capture_menu.addAction(self._action_capture_stop)
        capture_menu.addAction(self._action_capture_save)
        capture_menu.addSeparator()
        capture_menu.addAction(self._action_capture_open)

        # Audit submenu (P2.B.1 + P2.B.2)
        self._action_audit_verify = QAction("&Verify Audit Log…", self)
        self._action_audit_verify.triggered.connect(self._on_audit_verify)

        self._action_audit_view = QAction("View Audit Log…", self)
        self._action_audit_view.triggered.connect(self._on_audit_view)

        audit_menu = menu.addMenu("&Audit")
        audit_menu.addAction(self._action_audit_verify)
        audit_menu.addAction(self._action_audit_view)

        # P3.A.2 Tools menu — Preferences…
        self._action_preferences = QAction(self.tr("Preferences\u2026"), self)
        self._action_preferences.setShortcut("Ctrl+,")
        self._action_preferences.triggered.connect(self._on_preferences)
        tools_menu = menu.addMenu(self.tr("&Tools"))
        tools_menu.addAction(self._action_preferences)

        # P3.D View menu — Theme + Density
        self._action_theme_light = QAction(self.tr("&Light"), self)
        self._action_theme_light.setCheckable(True)
        self._action_theme_light.setChecked(True)
        self._action_theme_light.triggered.connect(lambda: self._apply_theme("Light"))

        self._action_theme_dark = QAction(self.tr("&Dark"), self)
        self._action_theme_dark.setCheckable(True)
        self._action_theme_dark.triggered.connect(lambda: self._apply_theme("Dark"))

        self._theme_group = QActionGroup(self)
        self._theme_group.addAction(self._action_theme_light)
        self._theme_group.addAction(self._action_theme_dark)
        self._theme_group.setExclusive(True)

        self._action_density_comfortable = QAction(self.tr("&Comfortable"), self)
        self._action_density_comfortable.setCheckable(True)
        self._action_density_comfortable.setChecked(True)
        self._action_density_comfortable.triggered.connect(
            lambda: self._apply_density(compact=False)
        )

        self._action_density_compact = QAction(self.tr("Co&mpact"), self)
        self._action_density_compact.setCheckable(True)
        self._action_density_compact.triggered.connect(lambda: self._apply_density(compact=True))

        self._density_group = QActionGroup(self)
        self._density_group.addAction(self._action_density_comfortable)
        self._density_group.addAction(self._action_density_compact)
        self._density_group.setExclusive(True)

        view_menu = menu.addMenu(self.tr("&View"))
        theme_submenu = view_menu.addMenu(self.tr("&Theme"))
        theme_submenu.addAction(self._action_theme_light)
        theme_submenu.addAction(self._action_theme_dark)
        density_submenu = view_menu.addMenu(self.tr("&Density"))
        density_submenu.addAction(self._action_density_comfortable)
        density_submenu.addAction(self._action_density_compact)

        # P3.B.2 / P3.E.2 Help menu — shortcuts + docs + report bug
        self._action_keyboard_shortcuts = QAction(self.tr("Keyboard Shortcuts\u2026"), self)
        self._action_keyboard_shortcuts.triggered.connect(self._show_keyboard_shortcuts)

        self._action_docs = QAction(self.tr("Documentation"), self)
        self._action_docs.triggered.connect(self._open_docs)

        self._action_report_bug = QAction(self.tr("Report Bug\u2026"), self)
        self._action_report_bug.triggered.connect(self._open_report_bug)

        help_menu = menu.addMenu("&Help")
        help_menu.addAction(self._action_keyboard_shortcuts)
        help_menu.addSeparator()
        help_menu.addAction(self._action_docs)
        help_menu.addAction(self._action_report_bug)
        help_menu.addSeparator()
        help_menu.addAction(self._action_about)

        # P3.B.2 F5 / Shift+F5 — window-level shortcuts for the object browser.
        # These are plain QActions added to the window so they fire regardless
        # of which child widget has focus.
        self._action_read_selected = QAction(self.tr("Read Selected"), self)
        self._action_read_selected.setShortcut("F5")
        self._action_read_selected.triggered.connect(
            lambda: (
                self._object_browser.read_selected() if hasattr(self, "_object_browser") else None
            )
        )
        self.addAction(self._action_read_selected)

        self._action_read_all = QAction(self.tr("Read All"), self)
        self._action_read_all.setShortcut("Shift+F5")
        self._action_read_all.triggered.connect(
            lambda: self._object_browser.read_all() if hasattr(self, "_object_browser") else None
        )
        self.addAction(self._action_read_all)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main", self)
        toolbar.setObjectName("MainToolbar")
        toolbar.setMovable(False)
        toolbar.addAction(self._action_probe_network)
        toolbar.addAction(self._action_new_connection)
        toolbar.addAction(self._action_disconnect)
        toolbar.addSeparator()
        self._audit_label = QLabel(
            f"Audit dir: {self._audit_dir}",
            self,
        )
        self._audit_label.setStyleSheet("padding: 0 8px; color: #4b5563;")
        toolbar.addWidget(self._audit_label)
        toolbar.addSeparator()
        # P0.D.2: Profile chip — reflects the currently-selected session's profile.
        self._profile_chip = QLabel("No active session", self)
        self._profile_chip.setObjectName("profile_chip")
        self._profile_chip.setStyleSheet(
            "padding: 2px 10px; border-radius: 4px; color: #6b7280; font-weight: bold;"
        )
        toolbar.addWidget(self._profile_chip)
        self.addToolBar(toolbar)

    def _build_panels(self) -> None:
        # ---- device tree (left) ----
        self._device_tree = DeviceTreePanel(self._state, self._session_manager, self)
        self._device_tree.session_selected.connect(self._on_session_selected)
        self._device_tree.object_selected.connect(self._on_object_selected_in_tree)
        self._device_tree.write_requested.connect(self._open_write_dialog)
        self._device_tree.add_to_watchlist_requested.connect(self._add_to_watchlist)
        self._device_tree.disconnect_requested.connect(self._disconnect_session)
        dock_left = QDockWidget("Devices", self)
        dock_left.setObjectName("DeviceTreeDock")
        dock_left.setWidget(self._device_tree)
        dock_left.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock_left)

        # ---- center: tabs (object browser / packet view) ----
        self._tabs = QTabWidget(self)
        self._tabs.setDocumentMode(True)
        self._object_browser = ObjectBrowserPanel(self._state, self._session_manager, self)
        self._object_browser.write_requested.connect(self._open_write_dialog)
        self._object_browser.add_to_watchlist_requested.connect(self._add_to_watchlist)
        self._packet_view = PacketViewPanel(self._state, self)
        self._tabs.addTab(self._object_browser, "Object Browser")
        self._tabs.addTab(self._packet_view, "Packet View")
        self.setCentralWidget(self._tabs)

        # ---- right: watchlist + session status ----
        self._watchlist = WatchlistPanel(self._state, self._session_manager, self)
        dock_right_top = QDockWidget("Watchlist", self)
        dock_right_top.setObjectName("WatchlistDock")
        dock_right_top.setWidget(self._watchlist)
        dock_right_top.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock_right_top)

        self._session_status = SessionStatusPanel(self._state, self)
        dock_right_bottom = QDockWidget("Sessions", self)
        dock_right_bottom.setObjectName("SessionStatusDock")
        dock_right_bottom.setWidget(self._session_status)
        dock_right_bottom.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock_right_bottom)

    def _build_status_bar(self) -> None:
        bar = QStatusBar(self)
        self.setStatusBar(bar)

        n_drivers = len(load_protocol_drivers())
        self._driver_count_label = QLabel(f"{n_drivers} driver(s)")
        bar.addPermanentWidget(self._driver_count_label)

        # P0.D.1: Live audit row counter.
        self._audit_row_count = 0
        self._audit_row_label = QLabel("Audit: 0 rows")
        self._audit_row_label.setObjectName("audit_row_label")
        bar.addPermanentWidget(self._audit_row_label)

        bar.showMessage("Ready")

    def _wire_signals(self) -> None:
        s = self._state
        s.error_raised.connect(self._on_error)
        s.session_failed.connect(self._on_session_failed)
        s.session_opened.connect(lambda *_a: self.statusBar().showMessage("Session opened", 4000))
        s.session_closed.connect(lambda *_a: self.statusBar().showMessage("Session closed", 4000))
        s.write_completed.connect(lambda *_a: self.statusBar().showMessage("Write completed", 4000))
        s.write_denied.connect(
            lambda *_a: self.statusBar().showMessage(
                "Write denied by safety profile",
                6000,
            )
        )
        # P0.D.1: Increment audit row counter on every logged event.
        s.audit_row_appended.connect(self._on_audit_row_appended)
        # P0.D.2: Update profile chip when sessions open/close.
        s.session_opened.connect(self._on_session_state_changed)
        s.session_closed.connect(self._on_session_state_changed)
        # P0.D.3: Keep disconnect button in sync when sessions close.
        s.session_closed.connect(self._on_session_closed_update_disconnect)
        # P2.A.2: Update capture action states on session change.
        s.session_opened.connect(self._on_session_state_for_capture)
        s.session_closed.connect(self._on_session_state_for_capture)
        # P2.A.2: Disable write actions when replay mode is active.
        s.replay_mode_changed.connect(self._on_replay_mode_changed)

    # ---- session management dispatch ------------------------------------

    def _open_probe_dialog(self) -> None:
        """Open the Probe Network dialog. If the operator picks a discovered
        device, we hand off to a pre-filled New Connection dialog."""
        if not load_protocol_drivers():
            QMessageBox.warning(
                self,
                "No protocol drivers",
                "No protocol drivers are installed. Run `pip install "
                "protoskipper[modbus]` and restart.",
            )
            return
        dialog = ProbeNetworkDialog(self._state, self._session_manager, parent=self)
        if dialog.exec() != ProbeNetworkDialog.Accepted:
            return
        sel = dialog.selection()
        if sel is None:
            return
        self._open_new_connection_dialog(prefill=sel)

    def _open_new_connection_dialog(self, prefill: ProbeSelection | None = None) -> None:
        if not load_protocol_drivers():
            QMessageBox.warning(
                self,
                "No protocol drivers",
                "No protocol drivers are installed. Run `pip install "
                "protoskipper[modbus]` and restart.",
            )
            return
        dialog = NewConnectionDialog(
            default_operator=PreferencesDialog.default_operator(), parent=self
        )
        if prefill is not None:
            dialog.set_protocol(prefill.protocol_id)
            dialog.set_address(prefill.device.address)
            if prefill.device.label:
                dialog.set_label(prefill.device.label)
        if dialog.exec() != NewConnectionDialog.Accepted:
            return
        req = dialog.request()
        try:
            driver = self._session_manager.driver_for(req.protocol_id)
            device = driver.parse_address(req.address)
            if req.label:
                device = DeviceRef(
                    protocol=device.protocol,
                    address=device.address,
                    label=req.label,
                    metadata=device.metadata,
                )
        except Exception as exc:
            QMessageBox.critical(self, "Could not parse address", str(exc))
            return
        self._session_manager.open_session(device, req.profile, req.operator)
        self.statusBar().showMessage(
            f"Opening session to {device.address} ({req.profile.value})…",
            4000,
        )

    def _disconnect_current_session(self) -> None:
        sid = self._currently_selected_session()
        if sid is None:
            return
        self._disconnect_session(sid)

    def _disconnect_session(self, session_id: str) -> None:
        self._session_manager.close_session(SessionId(session_id))

    def _currently_selected_session(self) -> str | None:
        sid = self._object_browser.current_session_id()
        info = self._state.session(sid) if sid else None
        return info.session_id if info else None

    # ---- panel selection forwarding --------------------------------------

    def _on_session_selected(self, session_id: str) -> None:
        self._object_browser.set_session(session_id)
        self._packet_view.set_session(session_id)
        info = self._state.session(SessionId(session_id))
        is_open = info is not None and info.is_open
        self._action_disconnect.setEnabled(is_open)
        self._action_close_session.setEnabled(is_open)
        has_any = any(i.is_open for i in self._state.sessions())
        self._action_close_all_sessions.setEnabled(has_any)
        # P0.D.2: Update profile chip to reflect the newly selected session.
        self._update_profile_chip(info.profile if info else None)

    def _on_object_selected_in_tree(self, session_id: str, _ref: ObjectRef) -> None:
        # The tree panel selected a specific object; switch to the
        # object-browser tab so the operator sees it highlighted.
        self._tabs.setCurrentWidget(self._object_browser)

    # ---- P0.D.1 audit row counter ---------------------------------------

    def _on_audit_row_appended(self) -> None:
        self._audit_row_count += 1
        self._audit_row_label.setText(f"Audit: {self._audit_row_count} rows")

    # ---- P0.D.2 profile chip --------------------------------------------

    def _update_profile_chip(self, profile: SessionProfile | None) -> None:
        from protoskipper.gui.theme import active_theme

        theme = active_theme()
        if profile is None:
            self._profile_chip.setText("No active session")
            self._profile_chip.setStyleSheet(
                "padding: 2px 10px; border-radius: 4px; color: #6b7280; font-weight: bold;"
            )
        else:
            colour = theme.profile_color(profile).name()
            self._profile_chip.setText(profile.value.upper())
            self._profile_chip.setStyleSheet(
                f"padding: 2px 10px; border-radius: 4px; color: {colour}; font-weight: bold;"
            )

    def _on_session_state_changed(self, *_args: object) -> None:
        """Update profile chip when any session opens or closes."""
        sid = self._currently_selected_session()
        info = self._state.session(SessionId(sid)) if sid else None
        self._update_profile_chip(info.profile if info and info.is_open else None)

    # ---- P0.D.3 disconnect button state ---------------------------------

    def _on_session_closed_update_disconnect(self, session_id: str) -> None:
        """Keep Disconnect / Close actions in sync when the current session closes."""
        current = self._currently_selected_session()
        if current is None or current == session_id:
            self._action_disconnect.setEnabled(False)
            self._action_close_session.setEnabled(False)
        has_any = any(i.is_open for i in self._state.sessions())
        self._action_close_all_sessions.setEnabled(has_any)

    # ---- write flow -----------------------------------------------------

    def _open_write_dialog(self, session_id: str, ref: ObjectRef) -> None:
        self._write_flow.start(session_id, ref)

    def _safety_dialog_factory(self, intent: WriteIntent, profile: SessionProfile):
        """Factory called by GuiConfirmHandler on the UI thread."""
        return SafetyConfirmDialog(intent, profile, parent=self)

    # ---- watchlist -----------------------------------------------------

    def _add_to_watchlist(self, session_id: str, ref: ObjectRef) -> None:
        added = self._state.add_to_watchlist(SessionId(session_id), ref)
        if added:
            self.statusBar().showMessage(
                f"Added {ref.object_id} to watchlist",
                3000,
            )

    # ---- P2.A.2 capture ------------------------------------------------

    def _active_session_id(self) -> SessionId | None:
        """Return the currently selected open session, or None."""
        sid_str = self._currently_selected_session()
        if sid_str is None:
            return None
        sid = SessionId(sid_str)
        info = self._state.session(sid)
        return sid if info and info.is_open else None

    def _on_session_state_for_capture(self, *_args: object) -> None:
        """Keep capture actions consistent with session open/close state."""
        has_session = self._active_session_id() is not None
        self._action_capture_start.setEnabled(has_session and not self._capture_active)
        self._action_capture_stop.setEnabled(has_session and self._capture_active)
        self._action_capture_save.setEnabled(has_session and self._capture_active)

    def _on_capture_start(self) -> None:
        sid = self._active_session_id()
        if sid is None:
            return
        self._state.exit_replay_mode()
        self._packet_view.set_replay_mode(False)
        self._session_manager.clear_capture(sid)
        self._capture_active = True
        self._on_session_state_for_capture()
        self.statusBar().showMessage("Capture started", 3000)

    def _on_capture_stop(self) -> None:
        sid = self._active_session_id()
        if sid is None:
            self._capture_active = False
            self._on_session_state_for_capture()
            return
        reply = QMessageBox.question(
            self,
            "Stop Capture",
            "Save captured frames to a pcapng file?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return
        if reply == QMessageBox.StandardButton.Save:
            self._save_capture_dialog(sid)
        self._capture_active = False
        self._on_session_state_for_capture()
        self.statusBar().showMessage("Capture stopped", 3000)

    def _on_capture_save(self) -> None:
        sid = self._active_session_id()
        if sid is None:
            return
        self._save_capture_dialog(sid)

    def _save_capture_dialog(self, session_id: SessionId) -> None:
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Save Capture",
            str(Path.home()),
            "pcapng files (*.pcapng);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        if not path.suffix:
            path = path.with_suffix(".pcapng")
        self._session_manager.save_capture(session_id, path)
        self.statusBar().showMessage(f"Capture saved to {path.name}", 5000)

    def _on_capture_open(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Open Capture",
            str(Path.home()),
            "pcapng files (*.pcapng);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            core_frames = read_pcapng(path)
        except Exception as exc:
            QMessageBox.critical(self, "Cannot open capture file", str(exc))
            return

        # Convert core.capture.CapturedFrame → gui.services.types.CapturedFrame.
        replay_sid = SessionId("__replay__")
        gui_frames = [
            GuiFrame(
                session_id=replay_sid,
                timestamp=f.timestamp,
                direction=Direction(f.direction),
                payload=f.payload,
                metadata={},
            )
            for f in core_frames
        ]

        self._packet_view.set_session("__replay__")
        self._packet_view.set_replay_mode(True)
        self._packet_view.load_replay_frames(gui_frames)
        self._state.record_replay_frames(gui_frames)
        self.statusBar().showMessage(
            f"Opened {len(gui_frames)} frames from {path.name} (read-only replay)", 6000
        )

    def _on_replay_mode_changed(self, active: bool) -> None:
        """Disable or re-enable write actions when replay mode changes."""
        self._action_new_connection.setEnabled(not active)
        self._action_probe_network.setEnabled(not active)
        self._action_disconnect.setEnabled(
            not active and self._currently_selected_session() is not None
        )

    # ---- P3.A.2 Preferences --------------------------------------------

    def _on_preferences(self) -> None:
        """Tools → Preferences…"""
        dlg = PreferencesDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_theme(PreferencesDialog.saved_theme())
            self._apply_density(PreferencesDialog.saved_density() == "Compact")

    # ---- P3.D.1 Theme switcher -----------------------------------------

    def _apply_theme(self, name: str, *, _save: bool = True) -> None:
        """Apply *name* ("Light" or "Dark") to the app and persist the choice."""
        apply_app_theme(name)
        if _save:
            QSettings("DataSailors", "ProtoSkipper").setValue("theme", name)
        # Update View → Theme check states (guard: may not exist during init).
        if hasattr(self, "_action_theme_light"):
            self._action_theme_light.setChecked(name != "Dark")
            self._action_theme_dark.setChecked(name == "Dark")

    # ---- P3.D.2 Compact density mode -----------------------------------

    def _apply_density(self, compact: bool, *, _save: bool = True) -> None:
        """Apply compact or comfortable density and persist the choice."""
        apply_density(compact)
        if _save:
            QSettings("DataSailors", "ProtoSkipper").setValue(
                "density", "Compact" if compact else "Comfortable"
            )
        if hasattr(self, "_action_density_comfortable"):
            self._action_density_comfortable.setChecked(not compact)
            self._action_density_compact.setChecked(compact)

    # ---- P3.B.2 Keyboard shortcuts dialog ------------------------------

    def _show_keyboard_shortcuts(self) -> None:
        """Help → Keyboard Shortcuts…"""
        dlg = QDialog(self)
        dlg.setWindowTitle(self.tr("Keyboard Shortcuts"))
        shortcuts = [
            ("Ctrl+P", self.tr("Probe network")),
            ("Ctrl+N", self.tr("New connection")),
            ("Ctrl+W", self.tr("Close current session")),
            ("Ctrl+Shift+W", self.tr("Close all sessions")),
            ("Ctrl+,", self.tr("Preferences")),
            ("F5", self.tr("Read selected register")),
            ("Shift+F5", self.tr("Read all registers")),
            ("Ctrl+Shift+R", self.tr("Start capture")),
            ("Ctrl+Shift+T", self.tr("Stop capture")),
            ("Ctrl+Shift+S", self.tr("Save capture")),
            ("Ctrl+Shift+O", self.tr("Open capture file")),
            ("Ctrl+Q", self.tr("Quit")),
        ]
        rows = "".join(
            f"<tr><td style='padding:2px 16px 2px 4px'><b>{k}</b></td>"
            f"<td style='padding:2px 4px'>{v}</td></tr>"
            for k, v in shortcuts
        )
        label = QLabel(f"<table>{rows}</table>", dlg)
        label.setTextFormat(Qt.TextFormat.RichText)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dlg)
        buttons.rejected.connect(dlg.reject)
        layout = QVBoxLayout(dlg)
        layout.addWidget(label)
        layout.addWidget(buttons)
        dlg.exec()

    # ---- P3.E.2 In-app documentation links -----------------------------

    def _open_docs(self) -> None:
        """Help → Documentation — opens the project wiki in a browser."""
        QDesktopServices.openUrl(QUrl("https://github.com/datasailors/protoskipper/wiki"))

    def _open_report_bug(self) -> None:
        """Help → Report Bug… — opens the GitHub new-issue form."""
        QDesktopServices.openUrl(QUrl("https://github.com/datasailors/protoskipper/issues/new"))

    # ---- P3.E.1 First-run welcome dialog --------------------------------

    def _check_first_run(self) -> None:
        """Show a one-time welcome dialog on the very first launch."""
        settings = QSettings("DataSailors", "ProtoSkipper")
        if settings.value("shown_welcome", False, bool):
            return
        settings.setValue("shown_welcome", True)

        dlg = QDialog(self)
        dlg.setWindowTitle(self.tr("Welcome to ProtoSkipper"))
        dlg.setMinimumWidth(480)

        msg = QLabel(
            self.tr(
                "<h3>Welcome to ProtoSkipper!</h3>"
                "<p>ProtoSkipper is an open-source SCADA/BMS protocol testing "
                "and commissioning toolkit.</p>"
                "<p><b>Getting started:</b></p>"
                "<ul>"
                "<li>Click <b>Probe Network\u2026</b> (Ctrl+P) to discover devices on "
                "your network.</li>"
                "<li>Click <b>New Connection\u2026</b> (Ctrl+N) to connect directly.</li>"
                "<li>No hardware? Run the built-in Modbus TCP simulator on "
                "<tt>localhost:5020</tt>.</li>"
                "</ul>"
            ),
            dlg,
        )
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.TextFormat.RichText)

        sim_button = QPushButton(self.tr("Run Modbus Simulator"), dlg)
        sim_button.setToolTip(self.tr("Start a local Modbus TCP test server on localhost:5020"))
        sim_button.clicked.connect(self._run_simulator)

        probe_button = QPushButton(self.tr("Probe Network\u2026"), dlg)
        probe_button.clicked.connect(dlg.accept)
        probe_button.clicked.connect(self._open_probe_dialog)

        close_button = QPushButton(self.tr("Get Started"), dlg)
        close_button.clicked.connect(dlg.accept)
        close_button.setDefault(True)

        button_row = QHBoxLayout()
        button_row.addWidget(sim_button)
        button_row.addStretch()
        button_row.addWidget(probe_button)
        button_row.addWidget(close_button)

        layout = QVBoxLayout(dlg)
        layout.addWidget(msg)
        layout.addLayout(button_row)
        dlg.exec()

    def _run_simulator(self) -> None:
        """Start ``protoskipper sim modbus`` as a background QProcess."""
        import sys

        from PySide6.QtCore import QProcess

        proc = QProcess(self)
        proc.start(sys.executable, ["-m", "protoskipper", "sim", "modbus"])
        if proc.waitForStarted(2000):
            self.statusBar().showMessage(
                self.tr("Modbus simulator started on localhost:5020"), 5000
            )
        else:
            QMessageBox.warning(
                self,
                self.tr("Simulator"),
                self.tr(
                    "Could not start the simulator automatically.\n"
                    "Run manually:  protoskipper sim modbus"
                ),
            )

    # ---- P3.B.2 Close-all sessions helper --------------------------------

    def _close_all_sessions(self) -> None:
        """Close every open session (bound to Ctrl+Shift+W)."""
        for info in list(self._state.sessions()):
            if info.is_open:
                self._disconnect_session(info.session_id)

    # ---- P2.B.1 audit verify -------------------------------------------

    def _on_audit_verify(self) -> None:
        """File → Audit → Verify Audit Log…"""
        audit_dir = self._audit_dir
        start_dir = str(audit_dir) if audit_dir else str(Path.home())
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Verify Audit Log",
            start_dir,
            "SQLite audit logs (*.db *.sqlite *.sqlite3);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            ok, message = verify_log(path)
        except Exception as exc:
            QMessageBox.critical(self, "Verification error", str(exc))
            return
        if ok:
            QMessageBox.information(self, "Audit Log: VERIFIED", message)
        else:
            QMessageBox.warning(self, "Audit Log: FAILED", message)

    # ---- P2.B.2 audit view ---------------------------------------------

    def _on_audit_view(self) -> None:
        """Audit → View Audit Log… — opens chronological viewer dialog."""
        audit_dir = self._audit_dir
        start_dir = str(audit_dir) if audit_dir else str(Path.home())
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "View Audit Log",
            start_dir,
            "SQLite audit logs (*.db *.sqlite *.sqlite3);;All files (*)",
        )
        if not path_str:
            return
        dlg = AuditViewDialog(Path(path_str), parent=self)
        dlg.exec()

    # ---- error / failure surfaces --------------------------------------

    def _on_error(self, operation: str, message: str) -> None:
        _logger.warning("Operation %s failed: %s", operation, message)
        self.statusBar().showMessage(f"{operation}: {message}", 8000)

    def _on_session_failed(self, session_id: str, error: str) -> None:
        QMessageBox.warning(
            self,
            "Session failed",
            f"Could not open session {session_id}: {error}",
        )

    # ---- about ---------------------------------------------------------

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About ProtoSkipper",
            (
                f"<h3>ProtoSkipper {__version__}</h3>"
                "<p>Open-source SCADA &amp; BMS protocol testing toolkit.</p>"
                "<p>Created and maintained by "
                "<a href='https://datasailors.io'>DataSailors Pvt Ltd</a>.</p>"
                "<p>Released under the GNU General Public License v3.0 or later.</p>"
                f"<p><i>Audit log directory:</i> {self._audit_dir}</p>"
            ),
        )

    # ---- persistence + close -------------------------------------------

    def _restore_settings(self) -> None:
        settings = QSettings("DataSailors", "ProtoSkipper")
        geometry = settings.value("MainWindow/geometry")
        state = settings.value("MainWindow/state")
        if geometry:
            self.restoreGeometry(geometry)
        if state:
            self.restoreState(state)

    def _save_settings(self) -> None:
        settings = QSettings("DataSailors", "ProtoSkipper")
        settings.setValue("MainWindow/geometry", self.saveGeometry())
        settings.setValue("MainWindow/state", self.saveState())

    def closeEvent(self, event) -> None:
        self._save_settings()
        try:
            self._session_manager.shutdown()
        except Exception:
            _logger.exception("Error during SessionManager shutdown; closing anyway")
        super().closeEvent(event)
