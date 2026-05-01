# ProtoSkipper

> Open-source SCADA & BMS protocol testing, probing, and commissioning toolkit.
> Created and maintained by **[DataSailors](https://datasailors.io)** — the team behind the DataSkipper RTU.

ProtoSkipper is a cross-platform desktop tool for industrial control engineers
who need to identify, probe, test, and commission devices speaking SCADA and
BMS protocols. It is designed as the *single tool every substation and BMS
engineer keeps on their laptop* — and is built on a plugin architecture so the
community can extend it with new protocols without touching the core.

## Status

Pre-alpha. Architecture and plugin contract are being shaped; the first
shippable milestone targets Modbus TCP and Modbus RTU end-to-end.

## What it does (target feature set)

- **Discover** devices on a network: Modbus TCP unit scans, BACnet Who-Is
  broadcasts, IEC 61850 GOOSE/MMS announcements, IEC 104 ASDU enumeration.
- **Probe** discovered devices: read registers, browse logical nodes,
  enumerate objects, fetch identification.
- **Interact as a master** with full read/write capability, gated by a
  per-session safety profile (Lab / Commissioning / Production) so
  destructive operations cannot happen by accident.
- **Capture and replay** protocol traffic in a common pcapng-based format,
  cross-protocol, on a single timeline.
- **Simulate** the slave side of any supported protocol for testing masters
  during development.
- **Audit** every byte sent and received in an append-only signed log,
  per session — so when something goes sideways at 3am you have proof of
  exactly what was on the wire.
- **Script** ad-hoc tests through an embedded Python REPL with first-class
  bindings to every loaded protocol driver.
- **Extend** with third-party protocol plugins distributed as ordinary Python
  packages — `pip install protoskipper-dnp3` and the new protocol shows up
  in the GUI on next launch.

## Supported protocols (planned)

| Protocol | Status |
|----------|--------|
| Modbus TCP | Phase 1 — in development |
| Modbus RTU | Phase 1 — in development |
| IEC 60870-5-104 | Phase 2 |
| BACnet/IP | Phase 3 |
| IEC 61850 (MMS, GOOSE) | Phase 4 |
| DNP3, OPC UA, Profibus, others | Community plugins |

## Quick start (developer)

```bash
git clone https://github.com/datasailors/protoskipper.git
cd protoskipper
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,gui]"
protoskipper --help
```

## Architecture

ProtoSkipper is a Python application built on PySide6 (Qt 6) for the GUI, with
a strict separation between the **core** (plugin loader, session lifecycle,
safety model, audit log) and **protocol drivers** (independent packages
implementing the `ProtocolDriver` interface).

Drivers register themselves via standard `setuptools` entry points
(`[project.entry-points."protoskipper.protocols"]`), so new protocols are
discovered automatically at startup with no core changes required.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the deeper design notes.

## Safety model

ProtoSkipper supports full master-side write and control operations. Because
an incorrect setpoint on a real substation can trip a feeder, the tool ships
with three baked-in safeguards:

1. **Session profiles** chosen at connection time:
   - **Lab** — writes allowed with single-click confirmation.
   - **Commissioning** — writes require an explicit confirm dialog.
   - **Production** — writes require typing back the target tag name to
     confirm, like `kubectl delete --confirm=<name>`.
2. **Dry-run / shadow mode** showing the exact protocol bytes that *would*
   be sent, with a diff against the device's current state, before any
   write commits.
3. **Append-only signed audit log** per session — every read, every write
   intent, every committed write, every protocol error, with timestamps
   and operator identity, written to a per-session SQLite WAL file.

These safeguards apply to every protocol uniformly through the
`SafetyContext` abstraction in `protoskipper.core.session`.

## License

ProtoSkipper is released under the **GNU General Public License v3.0 or
later** (GPL-3.0-or-later). See [`LICENSE`](LICENSE) for the full text.

The GPL was chosen deliberately: this tool is built for the industrial
engineering community to use freely, modify, redistribute, and improve. We
do not want vendors silently absorbing it into closed-source products. If
you ship ProtoSkipper or a derivative work, the GPL ensures your users
receive the same freedoms.

The **ProtoSkipper** name and logo are trademarks of DataSailors and are not
covered by the source-code license. You may fork the code under the GPL; you
may not ship a fork called "ProtoSkipper" without permission. This is the
same arrangement Mozilla, Wireshark, and the Linux Foundation use to keep
the canonical project distinguishable from forks.

## Contributing

Contributions are welcome under the project's
[Developer Certificate of Origin](CONTRIBUTING.md). We do not require a
Contributor License Agreement — DCO sign-off in your commits is sufficient,
and ensures no single party (including DataSailors) can unilaterally
relicense the project.

## Commercial support

DataSailors offers paid commissioning support, custom plugin development,
and on-site training for ProtoSkipper. Reach out at
[hello@datasailors.io](mailto:hello@datasailors.io).

The community version is, and will remain, free.
