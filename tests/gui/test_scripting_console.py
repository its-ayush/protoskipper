# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""GUI smoke tests for the ScriptingConsolePanel (P5.A.1)."""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import Qt

pytest.importorskip("PySide6")


@pytest.fixture()
def state(qapp):  # type: ignore[no-untyped-def]
    from protoskipper.gui.services.app_state import ApplicationState

    return ApplicationState()


@pytest.fixture()
def session_manager(state, tmp_path, qapp):  # type: ignore[no-untyped-def]
    from protoskipper.gui.services.confirm_handler import GuiConfirmHandler
    from protoskipper.gui.services.session_manager import SessionManager

    confirm = GuiConfirmHandler(dialog_factory=lambda intent, profile: None)
    return SessionManager(state=state, confirm_handler=confirm, audit_dir=tmp_path)


@pytest.fixture()
def panel(state, session_manager, qtbot):  # type: ignore[no-untyped-def]
    from protoskipper.gui.panels.scripting_console import ScriptingConsolePanel

    w = ScriptingConsolePanel(state, session_manager)
    qtbot.addWidget(w)
    w.show()
    return w


@pytest.mark.gui
class TestScriptingConsolePanelSmoke:
    def test_widget_shows(self, panel) -> None:  # type: ignore[no-untyped-def]
        assert panel.isVisible()

    def test_output_widget_present(self, panel) -> None:  # type: ignore[no-untyped-def]
        from PySide6.QtWidgets import QTextEdit

        output = panel.findChild(QTextEdit, "consoleOutput")
        assert output is not None

    def test_banner_appears(self, panel, qtbot) -> None:  # type: ignore[no-untyped-def]
        from PySide6.QtWidgets import QTextEdit

        output = panel.findChild(QTextEdit, "consoleOutput")
        assert output is not None
        # Wait for the worker thread to emit the banner (up to 2 s)
        qtbot.waitUntil(lambda: "ProtoSkipper" in output.toPlainText(), timeout=2000)

    def test_submit_simple_expression(self, panel, qtbot) -> None:  # type: ignore[no-untyped-def]
        from PySide6.QtWidgets import QTextEdit

        output = panel.findChild(QTextEdit, "consoleOutput")
        assert output is not None
        # Wait for banner
        qtbot.waitUntil(lambda: "ProtoSkipper" in output.toPlainText(), timeout=2000)
        # Submit a simple expression
        panel._input.setText("1 + 1")
        panel._submit()
        qtbot.waitUntil(lambda: "2" in output.toPlainText(), timeout=2000)

    def test_submit_print(self, panel, qtbot) -> None:  # type: ignore[no-untyped-def]
        from PySide6.QtWidgets import QTextEdit

        output = panel.findChild(QTextEdit, "consoleOutput")
        assert output is not None
        qtbot.waitUntil(lambda: "ProtoSkipper" in output.toPlainText(), timeout=2000)
        panel._input.setText('print("hello_protoskipper")')
        panel._submit()
        qtbot.waitUntil(lambda: "hello_protoskipper" in output.toPlainText(), timeout=2000)

    def test_state_binding_accessible(self, panel, qtbot) -> None:  # type: ignore[no-untyped-def]
        from PySide6.QtWidgets import QTextEdit

        output = panel.findChild(QTextEdit, "consoleOutput")
        assert output is not None
        qtbot.waitUntil(lambda: "ProtoSkipper" in output.toPlainText(), timeout=2000)
        panel._input.setText("type(state).__name__")
        panel._submit()
        qtbot.waitUntil(lambda: "ApplicationState" in output.toPlainText(), timeout=2000)

    def test_clear_button(self, panel, qtbot) -> None:  # type: ignore[no-untyped-def]
        from PySide6.QtWidgets import QPushButton, QTextEdit

        output = panel.findChild(QTextEdit, "consoleOutput")
        assert output is not None
        qtbot.waitUntil(lambda: "ProtoSkipper" in output.toPlainText(), timeout=2000)
        clear_btn = panel.findChild(QPushButton)
        assert clear_btn is not None
        qtbot.mouseClick(clear_btn, Qt.MouseButton.LeftButton)
        assert output.toPlainText() == ""

    def test_history_navigation(self, panel, qtbot) -> None:  # type: ignore[no-untyped-def]
        qtbot.waitUntil(lambda: "ProtoSkipper" in panel._output.toPlainText(), timeout=2000)
        panel._input.setText("x = 1")
        panel._submit()
        panel._input.setText("y = 2")
        panel._submit()
        # Press Up to go back
        qtbot.keyClick(panel._input, Qt.Key.Key_Up)
        assert panel._input.text() == "y = 2"
        qtbot.keyClick(panel._input, Qt.Key.Key_Up)
        assert panel._input.text() == "x = 1"
        # Press Down to go forward
        qtbot.keyClick(panel._input, Qt.Key.Key_Down)
        assert panel._input.text() == "y = 2"

    def test_active_workers_binding(self, panel, qtbot) -> None:  # type: ignore[no-untyped-def]
        from PySide6.QtWidgets import QTextEdit

        output = panel.findChild(QTextEdit, "consoleOutput")
        assert output is not None
        qtbot.waitUntil(lambda: "ProtoSkipper" in output.toPlainText(), timeout=2000)
        panel._input.setText("type(sessions).__name__")
        panel._submit()
        qtbot.waitUntil(lambda: "dict" in output.toPlainText(), timeout=2000)

    def test_close_stops_thread(self, panel, qtbot) -> None:  # type: ignore[no-untyped-def]
        qtbot.waitUntil(lambda: "ProtoSkipper" in panel._output.toPlainText(), timeout=2000)
        panel.close()
        # Give the thread 1 s to stop
        deadline = time.monotonic() + 1.0
        while panel._thread.isRunning() and time.monotonic() < deadline:
            qtbot.wait(100)
        assert not panel._thread.isRunning()
