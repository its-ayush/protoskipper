# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet protocol fuzzer scaffold (P7.H stub — not yet implemented).

Planned: generate malformed APDUs targeting:
* APDU length field overflows
* Object identifier type/instance boundary values
* Property access with out-of-range array indices
* Oversized segmented messages
* COV process-id exhaustion
"""

from __future__ import annotations
