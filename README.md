# ProtoSkipper

> Open-source SCADA & BMS protocol testing, probing, and commissioning toolkit.
> Created and maintained by **[DataSailors](https://datasailors.io)** — the team behind the DataSkipper RTU.

[![CI](https://github.com/its-ayush/protoskipper/actions/workflows/ci.yml/badge.svg)](https://github.com/its-ayush/protoskipper/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)

ProtoSkipper is a cross-platform desktop tool for industrial control engineers who need to identify, probe, test, and commission devices speaking SCADA and BMS protocols. It is designed as the *single tool every substation and BMS engineer keeps on their laptop* — built on a plugin architecture so the community can extend it with new protocols without touching the core.

---

## Current status

**Alpha.** Modbus TCP and Modbus RTU are functional end-to-end. The GUI is usable for real hardware commissioning work. Remaining protocols (IEC 104, BACnet, IEC 61850) are planned but not yet implemented.

### What works today

| Feature | State |
|---------|-------|
| Modbus TCP — connect, probe, browse registers | ✅ |
| Modbus RTU (serial) — connect, probe, browse registers | ✅ |
| Network scan (TCP port sweep + unit-ID scan) | ✅ |
| Manual register map — add, poll, read, write | ✅ |
| CSV register-map import (ABB, Schneider, Siemens examples included) | ✅ |
| Register map export / import (per-device `.json` files) | ✅ |
| Per-register polling with configurable intervals | ✅ |
| Watchlist panel for live-monitoring selected tags | ✅ |
| Safety profiles — Lab / Commissioning / Production | ✅ |
| Write confirmation dialogs (dry-run diff before commit) | ✅ |
| Append-only HMAC-chained audit log per session | ✅ |
| Audit log verification | ✅ |
| pcapng capture (record & replay) | ✅ |
| Packet timeline view | ✅ |
| Dark / Light theme with compact density mode | ✅ |
| Session setup save & restore (connection + registers) | ✅ |
| Plugin architecture — third-party protocols via `pip install` | ✅ |
| IEC 60870-5-104 | ⏳ Phase 2 |
| BACnet/IP | ⏳ Phase 3 |
| IEC 61850 (MMS, GOOSE) | ⏳ Phase 4 |
| Scripting REPL | ⏳ Phase 5 |

---

## Quick start

### Install (end user)

```bash
pip install "protoskipper[gui,modbus]"
protoskipper-gui          # launch the GUI
protoskipper --help       # CLI reference
```

### Developer setup

```bash
git clone https://github.com/datasailors/protoskipper.git
cd protoskipper
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
pre-commit install          # install git hooks (ruff, mypy, pytest)
protoskipper-gui            # launch the GUI
```

### Run tests

```bash
pytest tests/unit/          # pure unit tests — no hardware needed
pytest tests/integration/   # requires pymodbus; uses software simulator
pytest tests/gui/           # Qt GUI tests (headless via xvfb on Linux)
pytest                      # all of the above
```

---

## What it does

- **Discover** devices on a network: Modbus TCP unit scans, and (planned) BACnet Who-Is broadcasts, IEC 61850 GOOSE/MMS announcements, IEC 104 ASDU enumeration.
- **Probe** discovered devices: read registers, browse logical nodes, enumerate objects, fetch identification.
- **Interact as a master** with full read/write capability, gated by a per-session safety profile (Lab / Commissioning / Production) so destructive operations cannot happen by accident.
- **Capture and replay** protocol traffic in a common pcapng-based format, cross-protocol, on a single timeline.
- **Simulate** the slave side of any supported protocol for testing masters during development.
- **Audit** every byte sent and received in an append-only HMAC-signed log, per session — so when something goes sideways at 3am you have proof of exactly what was on the wire.
- **Script** ad-hoc tests through an embedded Python REPL with first-class bindings to every loaded protocol driver (planned).
- **Extend** with third-party protocol plugins distributed as ordinary Python packages — `pip install protoskipper-dnp3` and the new protocol shows up in the GUI on next launch.

---

## Plugin protocol drivers

Third-party drivers register via standard `setuptools` entry points:

```toml
# In your plugin's pyproject.toml
[project.entry-points."protoskipper.protocols"]
my_protocol = "my_package.driver:MyDriver"
```

ProtoSkipper discovers all installed drivers at startup — no core changes needed. See [`docs/PLUGINS.md`](docs/PLUGINS.md) for the full plugin contract and an annotated skeleton example.

---

## Architecture

Four strict layers with enforced dependency rules:

```
GUI   (src/protoskipper/gui/)       — PySide6/Qt6, zero protocol imports
CLI   (src/protoskipper/cli.py)     — thin argparse, headless
Core  (src/protoskipper/core/)      — protocol-agnostic, zero Qt imports
Drivers (builtin_drivers/ + pip)    — per-protocol implementations
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design documentation including the session lifecycle, audit log chain, and plugin contract. See [`docs/GUI_ARCHITECTURE.md`](docs/GUI_ARCHITECTURE.md) for the Qt threading model and signal/slot layout.

---

## Safety model

ProtoSkipper supports full master-side write and control operations. Because an incorrect setpoint on a real substation can trip a feeder, the tool ships with three baked-in safeguards:

1. **Session profiles** chosen at connection time:
   - **Lab** — writes allowed with single-click confirmation.
   - **Commissioning** — writes require an explicit confirm dialog.
   - **Production** — writes require typing back the target tag name, like `kubectl delete --confirm=<name>`.
2. **Dry-run preview** showing the exact protocol bytes that *would* be sent, with a diff against the device's current state, before any write commits.
3. **Append-only signed audit log** per session — every read, every write intent, every committed write, every protocol error, with timestamps and operator identity, written to a per-session SQLite WAL file.

These safeguards apply to every protocol uniformly through the `SafetyContext` abstraction in `protoskipper.core.session`.

---

## Supported protocols

| Protocol | Status | Extra |
|----------|--------|-------|
| Modbus TCP | Alpha — fully functional | `pip install "protoskipper[modbus]"` |
| Modbus RTU | Alpha — fully functional | `pip install "protoskipper[modbus]"` |
| IEC 60870-5-104 | Phase 2 | |
| BACnet/IP | Phase 3 | |
| IEC 61850 (MMS, GOOSE) | Phase 4 | |
| DNP3, OPC UA, others | Community plugins | |

---

## License

ProtoSkipper is released under the **GNU General Public License v3.0 or later** (GPL-3.0-or-later). See [`LICENSE`](LICENSE) for the full text.

The GPL was chosen deliberately: this tool is built for the industrial engineering community to use freely, modify, redistribute, and improve. We do not want vendors silently absorbing it into closed-source products. If you ship ProtoSkipper or a derivative work, the GPL ensures your users receive the same freedoms.

The **ProtoSkipper** name and logo are trademarks of DataSailors and are not covered by the source-code license. You may fork the code under the GPL; you may not ship a fork called "ProtoSkipper" without permission.

---

## Contributing

Contributions are welcome under the project's [Developer Certificate of Origin](CONTRIBUTING.md). We do not require a CLA — DCO sign-off (`git commit -s`) in your commits is sufficient.

Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow, code style guide, and branching strategy.

---

## Security

To report a security vulnerability, please follow the process in [`SECURITY.md`](SECURITY.md). **Do not open a public issue for security bugs.**

---

## Commercial support

DataSailors offers paid commissioning support, custom plugin development, and on-site training for ProtoSkipper. Reach out at [hello@datasailors.io](mailto:hello@datasailors.io).

The community version is, and will remain, free.
