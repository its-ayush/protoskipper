# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 60870-5-104 master driver (built-in plugin).

Implements the master/client side of IEC 60870-5-104 over TCP, including:

* APCI codec (I/S/U formats) with k/w sliding window and t1/t2/t3 timers.
* ASDU codec for the canonical subset of monitor/control types used in
  real substations (types 1, 3, 9, 13, 30, 31, 36, 45, 46, 70, 100, 102,
  103) plus the CP56Time2a / CP24Time2a / QDS / SIQ / DIQ field codecs.
* Point-list CSV loader so operators can hand a substation IOA map to the
  GUI without writing code.
* High-level operations: General Interrogation, Read, single command
  (direct execute), clock synchronisation, automatic reconnect.

Items intentionally deferred (not in this MVP — see ``docs/IEC104_PLAN.md``):
slave/server side, fuzzer, PCAP analyzer dissector, dedicated GUI panels,
TLS, file transfer, counter interrogation, SBO commands, vendor profile
presets, conformance runner, ASDU types outside the canonical subset.
"""

from protoskipper.builtin_drivers.iec104.driver import Iec104TcpDriver

__all__ = ["Iec104TcpDriver"]
