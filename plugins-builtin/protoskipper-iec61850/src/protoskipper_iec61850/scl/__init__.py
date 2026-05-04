# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""SCL sub-package for protoskipper-iec61850.

Provides IEC 61850-6 SCL file parsing, structural validation, and
round-trip serialisation.

Phase 8:
* P8.A.2 — schema-validating parser (``parser.py``, ``model.py``)  ✅
* P8.A.3 — semantic diff engine (``diff.py``)
"""

from __future__ import annotations

from .model import (
    FCDA,
    IED,
    LN,
    AccessPoint,
    ConnectedAP,
    DataSet,
    GseControl,
    LDevice,
    ReportControl,
    SampledValueControl,
    SclDocument,
    SubNetwork,
    Substation,
    ValidationIssue,
)
from .parser import parse, validate, write

__all__ = [
    "FCDA",
    "IED",
    "LN",
    "AccessPoint",
    "ConnectedAP",
    "DataSet",
    "GseControl",
    "LDevice",
    "ReportControl",
    "SampledValueControl",
    "SclDocument",
    "SubNetwork",
    "Substation",
    "ValidationIssue",
    "parse",
    "validate",
    "write",
]
