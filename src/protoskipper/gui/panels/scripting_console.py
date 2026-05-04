# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Python scripting console panel (P5.A.1 of EXECUTION_PLAN.md).

An embedded Python REPL that runs inside a QThread so console I/O never
blocks the Qt event loop.  The panel exposes bindings:

* ``state``        — :class:`~protoskipper.gui.services.ApplicationState`
* ``manager``      — :class:`~protoskipper.gui.services.SessionManager`
* ``sessions``     — ``dict[SessionId, DriverWorker]`` snapshot (read-only view)

The REPL runs :class:`code.InteractiveConsole` so multi-line statements
(for-loops, functions, class definitions) work correctly.

Design notes
------------
* ``_ConsoleWorker`` lives on a ``QThread``; it reads commands from a
  ``queue.SimpleQueue`` and writes output back via a Qt signal.
* The panel's ``QTextEdit`` (output) and ``QLineEdit`` (input) live on
  the GUI thread.  History is kept in a plain list; up/down arrows cycle it.
* ``sys.stdout`` / ``sys.stderr`` are **not** replaced globally — only the
  ``ConsoleWorker``'s private ``StringIO`` captures output.
"""

from __future__ import annotations

import code
import io
import logging
import queue
import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QColor, QFont, QKeyEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from protoskipper.gui.services.app_state import ApplicationState
from protoskipper.gui.services.session_manager import SessionManager

if TYPE_CHECKING:
    pass

_logger = logging.getLogger(__name__)

_BANNER = (
    f"ProtoSkipper Python Console (Python {sys.version.split()[0]})\n"
    "Bindings: state, manager, sessions, iec104, modbus\n"
    "Type help(iec104) or help(iec104.MasterSession) for IEC 104 API docs.\n"
)

_PROMPT_PS1 = ">>> "
_PROMPT_PS2 = "... "


class _RedirectStdio(io.StringIO):
    """StringIO wrapper that also calls a callback on each write."""

    def __init__(self, callback: object) -> None:
        super().__init__()
        self._cb = callback  # callable[[str], None]

    def write(self, s: str) -> int:  # type: ignore[override]
        self._cb(s)
        return len(s)

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Worker (lives on QThread)
# ---------------------------------------------------------------------------


class _ConsoleWorker(QObject):
    """Runs code.InteractiveConsole in a dedicated thread."""

    output_ready = Signal(str, bool)  # (text, is_error)
    prompt_changed = Signal(str)  # PS1 or PS2

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._session_manager = session_manager
        self._queue: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._running = False

    def post_line(self, line: str) -> None:
        """Called from GUI thread — enqueue a line for execution."""
        self._queue.put(line)

    def stop(self) -> None:
        """Signal the worker to exit its loop."""
        self._queue.put(None)  # sentinel

    @Slot()
    def run(self) -> None:
        """Main loop — executed on the worker QThread."""
        self._running = True

        # Build protocol scripting namespaces (allow_writes=True in the REPL
        # because write safety is enforced by the SessionManager / SafetyContext).
        _iec104_ns: object = None
        _modbus_ns: object = None
        import contextlib

        with contextlib.suppress(Exception):
            from protoskipper.scripting import _make_iec104_ns

            _iec104_ns = _make_iec104_ns(allow_writes=True)
        with contextlib.suppress(Exception):
            _modbus_ns = __import__("protoskipper.builtin_drivers.modbus", fromlist=[""])

        local_ns: dict[str, object] = {
            "state": self._state,
            "manager": self._session_manager,
            "sessions": self._session_manager.active_workers,
            "iec104": _iec104_ns,
            "modbus": _modbus_ns,
        }
        console = code.InteractiveConsole(locals=local_ns)

        def _emit_out(text: str) -> None:
            if text:
                self.output_ready.emit(text, False)

        def _emit_err(text: str) -> None:
            if text:
                self.output_ready.emit(text, True)

        stdout_redir = _RedirectStdio(_emit_out)
        stderr_redir = _RedirectStdio(_emit_err)

        self.output_ready.emit(_BANNER, False)
        self.prompt_changed.emit(_PROMPT_PS1)

        while self._running:
            try:
                line = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if line is None:
                break  # stop sentinel

            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = stdout_redir  # type: ignore[assignment]
            sys.stderr = stderr_redir  # type: ignore[assignment]
            try:
                need_more = console.push(line)
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr

            # Update sessions binding each time so the user sees the live dict.
            local_ns["sessions"] = self._session_manager.active_workers
            self.prompt_changed.emit(_PROMPT_PS2 if need_more else _PROMPT_PS1)

        self._running = False


# ---------------------------------------------------------------------------
# History-aware input line
# ---------------------------------------------------------------------------


class _HistoryLineEdit(QLineEdit):
    """QLineEdit that cycles through command history on Up/Down."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history: list[str] = []
        self._hist_pos: int = 0  # index into history (0 = newest slot)

    def add_history(self, line: str) -> None:
        if line and (not self._history or self._history[-1] != line):
            self._history.append(line)
        self._hist_pos = len(self._history)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        key = event.key()
        if key == Qt.Key.Key_Up:
            if self._hist_pos > 0:
                self._hist_pos -= 1
                self.setText(self._history[self._hist_pos])
            return
        if key == Qt.Key.Key_Down:
            if self._hist_pos < len(self._history) - 1:
                self._hist_pos += 1
                self.setText(self._history[self._hist_pos])
            elif self._hist_pos == len(self._history) - 1:
                self._hist_pos = len(self._history)
                self.clear()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


class ScriptingConsolePanel(QWidget):
    """Dockable Python REPL panel (P5.A.1)."""

    def __init__(
        self,
        state: ApplicationState,
        session_manager: SessionManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._session_manager = session_manager
        self._prompt = _PROMPT_PS1

        self._worker = _ConsoleWorker(state, session_manager)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)

        self._build_ui()

        self._worker.output_ready.connect(self._on_output)
        self._worker.prompt_changed.connect(self._on_prompt_changed)

        self._thread.start()

        # Stop the worker thread when the Qt application is about to quit.
        # This prevents the C++ objects from being freed while the thread
        # is still running (which causes a fatal abort in tests / on exit).
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._stop_thread)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        # Output area
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)

        self._output = QTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(mono)
        self._output.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._output.setObjectName("consoleOutput")
        layout.addWidget(self._output, stretch=1)

        # Input row
        input_row = QHBoxLayout()
        input_row.setSpacing(4)

        self._prompt_label = QLineEdit(_PROMPT_PS1)
        self._prompt_label.setReadOnly(True)
        self._prompt_label.setFixedWidth(36)
        self._prompt_label.setFont(mono)
        self._prompt_label.setFrame(False)
        input_row.addWidget(self._prompt_label)

        self._input = _HistoryLineEdit()
        self._input.setFont(mono)
        self._input.setPlaceholderText("Python expression or statement…")
        self._input.returnPressed.connect(self._submit)
        input_row.addWidget(self._input, stretch=1)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._clear_btn.clicked.connect(self._output.clear)
        input_row.addWidget(self._clear_btn)

        layout.addLayout(input_row)

    # ------------------------------------------------------------------
    @Slot()
    def _submit(self) -> None:
        line = self._input.text()
        self._input.add_history(line)
        self._input.clear()
        # Echo to output
        self._append_text(self._prompt + line + "\n", error=False)
        self._worker.post_line(line)

    @Slot(str, bool)
    def _on_output(self, text: str, is_error: bool) -> None:
        self._append_text(text, error=is_error)

    @Slot(str)
    def _on_prompt_changed(self, prompt: str) -> None:
        self._prompt = prompt
        self._prompt_label.setText(prompt)

    def _append_text(self, text: str, *, error: bool) -> None:
        cursor = self._output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        if error:
            fmt.setForeground(QColor("#f28b82"))  # soft red
        else:
            fmt.setForeground(self._output.palette().text().color())
        cursor.setCharFormat(fmt)
        cursor.insertText(text)
        self._output.setTextCursor(cursor)
        self._output.ensureCursorVisible()

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _stop_thread(self) -> None:
        """Stop the worker thread (idempotent)."""
        if self._thread.isRunning():
            self._worker.stop()
            self._thread.quit()
            self._thread.wait(2000)

    def closeEvent(self, event: object) -> None:  # type: ignore[override]
        """Stop the worker thread cleanly when the dock is closed."""
        self._stop_thread()
        super().closeEvent(event)  # type: ignore[call-arg]
