# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 60870-5-104 master driver (built-in plugin).

Implements the master/client side of IEC 60870-5-104 over TCP, including:

* APCI codec (I/S/U formats) with k/w sliding window and t1/t2/t3 timers.
* ASDU codec for the canonical subset of monitor/control types used in
  real substations (types 1, 3, 9, 13, 30, 31, 36, 45, 46, 70, 100, 102,
  103) plus the CP56Time2a / CP24Time2a / QDS / SIQ / DIQ field codecs.
* Slave/simulator server for testing and lab commissioning.
* Deterministic fuzzer engine for protocol compliance testing.
* Offline libpcap dissector.
* Point-list CSV loader so operators can hand a substation IOA map to the
  GUI without writing code.
* High-level scripting façade (``MasterSession``, ``SlaveServer``,
  ``PcapReader``, ``Fuzzer``) for use from the REPL and headless scripts.

Items still in progress (see ``docs/internal/IEC104_PLAN.md``):
full TLS stack, file transfer (P4.B), slave spontaneous events + TLS
(P4.C), fuzzer GUI/report (P4.D), PCAP filter language + timeline
(P4.E), scripting bindings (P4.G.3), user guide (P4.G.6).
"""

from protoskipper.builtin_drivers.iec104.driver import Iec104TcpDriver
from protoskipper.builtin_drivers.iec104.scripting import (
    Fuzzer,
    MasterSession,
    PcapReader,
    SlaveServer,
)

__all__ = [
    "Fuzzer",
    "Iec104TcpDriver",
    "MasterSession",
    "PcapReader",
    "SlaveServer",
]
