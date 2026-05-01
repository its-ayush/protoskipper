# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Allow ``python -m protoskipper`` to launch the CLI."""

from protoskipper.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
