# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""UpdateCheckerDialog — P6.D.1 of docs/internal/EXECUTION_PLAN.md.

Fetches the latest GitHub release for ProtoSkipper, compares it against
the currently-installed version, and shows either "Up to date" or a
"Version X.Y.Z is available — Download" prompt with a clickable link.

Rules
-----
* Network request runs on a ``QThread`` worker so the UI never blocks.
* The dialog **never auto-installs** or auto-downloads anything.
* Only a plain HTTPS GET to the GitHub releases API is performed.
* No credentials are stored or transmitted.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

import protoskipper

_RELEASES_URL = "https://api.github.com/repos/DataSailors/protoskipper/releases/latest"
_DOWNLOAD_URL = "https://github.com/DataSailors/protoskipper/releases/latest"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_semver(version: str) -> tuple[int, int, int]:
    """Parse 'X.Y.Z' or 'vX.Y.Z' into a comparable tuple.

    Returns ``(0, 0, 0)`` for any unrecognised format so callers always
    get a valid tuple.
    """
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+).*", version.strip())
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _fetch_latest_version(timeout: float = 10.0) -> str:
    """Return the tag_name of the latest GitHub release.

    Raises :class:`OSError` on network failure and :class:`ValueError` on
    unexpected API response.  Both are caught by the worker.
    """
    req = urllib.request.Request(
        _RELEASES_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "protoskipper"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data: dict[str, Any] = json.loads(resp.read())
    tag = data.get("tag_name", "")
    if not tag:
        raise ValueError(f"GitHub API response missing 'tag_name': {data!r}")
    return tag


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class _UpdateWorker(QObject):
    """Fetches the latest release tag on a worker thread."""

    finished = Signal(str)  # latest version tag (or "error:<msg>")

    @Slot()
    def run(self) -> None:
        try:
            tag = _fetch_latest_version()
            self.finished.emit(tag)
        except Exception as exc:
            self.finished.emit(f"error:{exc}")


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class UpdateCheckerDialog(QDialog):
    """Check for ProtoSkipper updates against the GitHub releases feed.

    Shows a spinner while the request is in flight, then displays one of:

    * "You are running the latest version (X.Y.Z)."
    * "Version X.Y.Z is available — Download" (with a link button).
    * "Could not check for updates: <reason>." (on network error).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Check for Updates")
        self.setMinimumWidth(400)
        self.setModal(True)

        self._current = protoskipper.__version__

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self._status_label = QLabel("Checking for updates…")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        layout.addWidget(self._progress)

        self._download_label = QLabel()
        self._download_label.setOpenExternalLinks(False)
        self._download_label.setVisible(False)
        layout.addWidget(self._download_label)

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._buttons.rejected.connect(self.reject)
        # "Download" button added dynamically when update is available.
        layout.addWidget(self._buttons)

        # Start worker
        self._thread = QThread(self)
        self._worker = _UpdateWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_result)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    # ------------------------------------------------------------------
    # Private slots
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_result(self, tag: str) -> None:
        self._progress.setVisible(False)

        if tag.startswith("error:"):
            reason = tag[len("error:") :]
            self._status_label.setText(
                f"Could not check for updates:\n{reason}\n\n"
                "Please visit "
                f'<a href="{_DOWNLOAD_URL}">the releases page</a> manually.'
            )
            self._status_label.setOpenExternalLinks(True)
            return

        current_tuple = _parse_semver(self._current)
        latest_tuple = _parse_semver(tag)

        if latest_tuple <= current_tuple:
            self._status_label.setText(f"You are running the latest version ({self._current}).")
        else:
            self._status_label.setText(
                f"Version <b>{tag}</b> is available (you have {self._current})."
            )
            download_btn = self._buttons.addButton(
                "Download…", QDialogButtonBox.ButtonRole.ActionRole
            )
            download_btn.clicked.connect(self._open_download_page)

    @Slot()
    def _open_download_page(self) -> None:
        from PySide6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl(_DOWNLOAD_URL))

    def closeEvent(self, event: Any) -> None:  # type: ignore[override]
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        super().closeEvent(event)
