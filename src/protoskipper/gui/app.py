# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""GUI entry point: ``protoskipper-gui``.

Launches the Qt application, configures logging, instantiates the main
window, and runs the event loop. Kept deliberately small so the heavy
lifting (driver loading, panel construction) stays in :mod:`main_window`
where it can be tested without driving a Qt loop.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ProtoSkipper desktop application."""
    _configure_logging()

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        sys.stderr.write(
            "PySide6 is not installed. Run `pip install protoskipper[gui]` "
            "or install PySide6 directly.\n"
        )
        return 1

    from protoskipper import __version__
    from protoskipper.gui.main_window import MainWindow

    args = list(argv) if argv is not None else sys.argv
    app = QApplication(args)
    app.setApplicationName("ProtoSkipper")
    app.setApplicationDisplayName("ProtoSkipper")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("DataSailors Pvt Ltd")
    app.setOrganizationDomain("datasailors.io")

    window = MainWindow()
    window.show()
    window.raise_()
    window.activateWindow()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
