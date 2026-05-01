# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Built-in protocol drivers shipped with the core package.

These drivers register themselves through the same entry-point mechanism
third-party plugins use, so they hold no privileged status. Removing the
``[modbus]`` extra at install time is enough to keep the dependencies out of
a minimal install.
"""
