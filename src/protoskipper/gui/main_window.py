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

import contextlib
import logging
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QToolBar,
)

from protoskipper import __version__
from protoskipper.core.driver import DeviceRef, ObjectRef, SessionProfile, WriteIntent
from protoskipper.core.plugin_loader import load_protocol_drivers
from protoskipper.gui.dialogs import (
    NewConnectionDialog,
    ProbeNetworkDialog,
    ProbeSelection,
    SafetyConfirmDialog,
    WriteDialog,
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
from protoskipper.gui.services.types import SessionId

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

        # ---- ui ----
        self._build_actions()
        self._build_toolbar()
        self._build_panels()
        self._build_status_bar()
        self._wire_signals()

        # Restore persisted layout (window geometry + dock state).
        self._restore_settings()

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
        file_menu.addAction(self._action_disconnect)
        file_menu.addSeparator()
        file_menu.addAction(self._action_quit)

        help_menu = menu.addMenu("&Help")
        help_menu.addAction(self._action_about)

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
        dialog = NewConnectionDialog(parent=self)
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
        info = (
            self._state.session(SessionId(self._object_browser._session_id))
            if self._object_browser._session_id
            else None
        )
        return info.session_id if info else None

    # ---- panel selection forwarding --------------------------------------

    def _on_session_selected(self, session_id: str) -> None:
        self._object_browser.set_session(session_id)
        self._packet_view.set_session(session_id)
        info = self._state.session(SessionId(session_id))
        self._action_disconnect.setEnabled(info is not None and info.is_open)

    def _on_object_selected_in_tree(self, session_id: str, _ref: ObjectRef) -> None:
        # The tree panel selected a specific object; switch to the
        # object-browser tab so the operator sees it highlighted.
        self._tabs.setCurrentWidget(self._object_browser)

    # ---- write flow -----------------------------------------------------

    def _open_write_dialog(self, session_id: str, ref: ObjectRef) -> None:
        info = self._state.session(SessionId(session_id))
        if info is None or not info.is_open:
            return
        last = info.last_values.get(ref.object_id)

        dialog = WriteDialog(ref, info.profile, last_known=last, parent=self)

        # Wire: when the operator clicks Prepare, dispatch prepare_write
        # and wait for the worker's write_intent_prepared signal.
        def on_intent_prepared(sid: str, intent: WriteIntent) -> None:
            if sid != session_id:
                return
            if intent.object_ref.object_id != ref.object_id:
                return
            dialog.set_intent(intent)
            self._state.write_intent_prepared.disconnect(on_intent_prepared)

        def on_error(operation: str, message: str) -> None:
            if operation != "prepare_write":
                return
            dialog.report_prepare_failed(message)

        self._state.write_intent_prepared.connect(on_intent_prepared)
        self._state.error_raised.connect(on_error)

        # Dispatch prepare on Next click.
        original_prepare = dialog._on_prepare_clicked

        def prepare_dispatched():
            original_prepare()
            value = dialog.typed_value()
            if value is None:
                # Validation failed; original_prepare already showed the error.
                return
            self._session_manager.prepare_write(SessionId(session_id), ref, value)

        dialog._next.clicked.disconnect(original_prepare)
        dialog._next.clicked.connect(prepare_dispatched)

        try:
            accepted = dialog.exec() == WriteDialog.Accepted
        finally:
            with contextlib.suppress(TypeError, RuntimeError):
                self._state.write_intent_prepared.disconnect(on_intent_prepared)
            with contextlib.suppress(TypeError, RuntimeError):
                self._state.error_raised.disconnect(on_error)

        if accepted and dialog.intent() is not None:
            # The actual safety confirm dialog will run inside
            # commit_write via GuiConfirmHandler. The two-prompt design is
            # intentional - this dispatch starts the chain.
            self._session_manager.commit_write(SessionId(session_id), dialog.intent())

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
