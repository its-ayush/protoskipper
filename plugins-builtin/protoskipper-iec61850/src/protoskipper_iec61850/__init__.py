# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""protoskipper-iec61850 — IEC 61850 plugin for ProtoSkipper.

Phase 8 implementation.  Registers ``iec61850.mms`` in the
``protoskipper.protocols`` entry-point group.
"""

from __future__ import annotations

from protoskipper_iec61850.driver import Iec61850MmsDriver

__all__ = ["Iec61850MmsDriver"]
__version__ = "0.1.0"
