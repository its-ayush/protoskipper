# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet/IP built-in driver package.

Registers three protocol IDs via the ``protoskipper.protocols`` entry-point:

* ``bacnet.ip``   — BACnet/IPv4 client (BVLC/NPDU/APDU over UDP/47808).
* ``bacnet.sc``   — BACnet/SC (Secure Connect, WebSocket + TLS, Add. 135-2020).
* ``bacnet.mstp`` — BACnet MS/TP master over RS-485 serial.

All three share the same object model, points-list parser, vendor profile
library, and safety-context wiring.  Only the transport layer differs.

All imports of ``bacpypes3`` are lazy (inside method bodies or guarded by
``TYPE_CHECKING``) so the package remains importable on a minimal install that
does not have ``bacpypes3`` installed.
"""
