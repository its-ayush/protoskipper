# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet scripting bindings scaffold (P7.H stub — not yet implemented).

Planned: expose a simplified Python API for BACnet automation scripts:
    from protoskipper.builtin_drivers.bacnet.scripting import BACnetScript
    s = BACnetScript("192.168.1.100/dev=1234")
    s.read("analog-value:1")
    s.write("analog-output:2", 22.5, priority=8)
"""

from __future__ import annotations
