# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""LogPanel — real-time application log viewer.

Displays Python :mod:`logging` records emitted anywhere in the
ProtoSkipper process.  A :class:`_QtLogHandler` is installed on the root
logger at panel construction time and removed when the panel is destroyed.

Design constraints
------------------
* No protocol-specific imports; depends only on stdlib and PySide6.
* The logging handler posts a signal-safe call to the GUI thread via
  ``QMetaObject.invokeMethod`` so it is safe to use from worker threads.
* Maximum line count is capped at 5 000 to avoid unbounded memory growth.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_MAX_LINES = 5_000

_LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG: "#6b7280",  # grey
    logging.INFO: "#111827",  # near-black
    logging.WARNING: "#b45309",  # amber
    logging.ERROR: "#b91c1c",  # red
    logging.CRITICAL: "#7c3aed",  # purple
}

_logger = logging.getLogger(__name__)


class _QtLogHandler(logging.Handler):
    """Logging handler that appends records to a :class:`LogPanel`."""

    def __init__(self, panel: LogPanel) -> None:
        super().__init__()
        self._panel = panel
        fmt = logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )
        self.setFormatter(fmt)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            level = record.levelno
        except Exception:
            return
        # Emitting a Signal from any thread to a QueuedConnection slot is
        # thread-safe in Qt.  This is the correct PySide6 cross-thread pattern.
        self._panel._log_received.emit(msg, level)


class LogPanel(QWidget):
    """Bottom-dock panel that streams live application log records."""

    # Thread-safe bridge: any thread can emit this; _append always runs on
    # the GUI thread because it is connected with QueuedConnection.
    _log_received: Signal = Signal(str, int)  # msg, levelno

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._min_level: int = logging.DEBUG

        self._build_ui()

        # Connect bridge signal → _append with QueuedConnection so worker-thread
        # emits are marshalled to the GUI event loop automatically.
        self._log_received.connect(self._append, Qt.ConnectionType.QueuedConnection)

        self._handler = _QtLogHandler(self)
        self._handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self._handler)

    # ---- construction ---------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # toolbar
        bar_widget = QWidget(self)
        bar_layout = QHBoxLayout(bar_widget)
        bar_layout.setContentsMargins(4, 2, 4, 2)
        bar_layout.setSpacing(6)

        bar_layout.addWidget(QLabel("Level:", bar_widget))

        self._level_combo = QComboBox(bar_widget)
        for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            self._level_combo.addItem(name)
        self._level_combo.setCurrentText("DEBUG")
        self._level_combo.currentTextChanged.connect(self._on_level_changed)
        bar_layout.addWidget(self._level_combo)

        bar_layout.addStretch()

        clear_btn = QPushButton("Clear", bar_widget)
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._on_clear)
        bar_layout.addWidget(clear_btn)

        root.addWidget(bar_widget)

        # log text area
        self._text = QPlainTextEdit(self)
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(_MAX_LINES)
        font = QFont("Menlo, Monaco, Consolas, monospace")
        font.setPointSize(11)
        self._text.setFont(font)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        root.addWidget(self._text, 1)

    # ---- private slots --------------------------------------------------

    @Slot(str, int)
    def _append(self, msg: str, level: int) -> None:
        if level < self._min_level:
            return
        color = _LEVEL_COLORS.get(level, "#111827")
        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        cursor.insertText(msg + "\n")
        self._text.setTextCursor(cursor)
        self._text.ensureCursorVisible()

    def _on_level_changed(self, name: str) -> None:
        self._min_level = getattr(logging, name, logging.DEBUG)

    def _on_clear(self) -> None:
        self._text.clear()

    # ---- cleanup --------------------------------------------------------

    def closeEvent(self, event: Any) -> None:
        logging.getLogger().removeHandler(self._handler)
        super().closeEvent(event)
