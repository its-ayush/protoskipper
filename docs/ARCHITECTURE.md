# ProtoSkipper Architecture

This document is for developers who want to understand how ProtoSkipper is
put together — either to contribute to the core, write a third-party
protocol plugin, or audit the safety guarantees before deploying the tool
on a live substation.

The intended reader has a working knowledge of Python and at least passing
familiarity with one industrial protocol (Modbus is enough). It does not
assume Qt experience; the GUI is intentionally a thin layer over the core
abstractions documented here.

## Component overview

ProtoSkipper is a single Python application split into four cleanly
separated layers:

```
+--------------------------------------------------------------+
|  GUI  (PySide6 / Qt 6, src/protoskipper/gui/)                |
|  - Main window, dockable panels                              |
|  - Confirmation dialogs, profile picker                      |
|  - Packet view, watchlist, scripting REPL                    |
|  - GOOSE subscriber / publisher panels                       |
+--------------------------------------------------------------+
|  CLI  (src/protoskipper/cli.py)                              |
|  - Headless commands: list-protocols, scan, ...              |
+--------------------------------------------------------------+
|  Core  (src/protoskipper/core/)                              |
|  - ProtocolDriver / DriverSession ABCs (driver.py)           |
|  - Plugin loader (plugin_loader.py)                          |
|  - Session lifecycle + audit log (session.py + audit.py)     |
|  - Safety context, error hierarchy                           |
+--------------------------------------------------------------+
|  Drivers                                                     |
|  - Built-in: builtin_drivers/modbus/ (TCP + RTU)            |
|  - Built-in plugin: plugins-builtin/protoskipper-iec61850/  |
|    (IEC 61850 MMS + GOOSE, installed via pip install -e)     |
|  - Community: any pip-installable plugin registered through  |
|    the `protoskipper.protocols` entry-point group            |
+--------------------------------------------------------------+
```

The core has no GUI dependencies and the GUI has no protocol-specific
dependencies. A headless CI install with only the `[modbus]` extra is a
supported configuration.

## The plugin contract

Every protocol driver subclasses two abstract base classes from
`protoskipper.core.driver`:

| ABC | Purpose |
| --- | --- |
| `ProtocolDriver` | Stateless factory: `discover()` finds devices, `connect()` opens a session. |
| `DriverSession` | Per-device state: enumerate, read, prepare/commit writes, close. |

Three optional capability mix-ins (defined as `typing.Protocol` so they
do not affect ABC instantiation) declare additional features a driver may
support:

| Protocol | Capability |
| --- | --- |
| `Subscriber` | Push-style updates (BACnet COV, IEC 61850 reports, BRCB/URCB). |
| `Simulator` | Driver can act as the slave/server side. |
| `Capturer` | Driver can stream raw frames to a `CaptureSink`. |

GOOSE publish/subscribe lives *outside* the `DriverSession` contract
because it is a Layer 2 multicast service, not a per-device connection.
It is implemented as standalone services (`GooseSubscriberService` and
`GoosePublisherService` in `protoskipper_iec61850.goose`) with thin Qt
wrappers (`GooseSubscriberQt`, `GoosePublisherQt`) in
`gui/services/goose_service.py`. The GUI panels for GOOSE operate
independently of the session lifecycle.

The GUI uses `isinstance(session, Subscriber)` etc. to decide which
affordances to enable; a Modbus-only driver simply does not inherit from
the protocols it does not support, and no UI for those features appears.

## The read/write split

Writes are *two* operations: `prepare_write()` returns a `WriteIntent`
describing the exact bytes that would go on the wire, and `commit_write()`
is what actually transmits. This split is what makes three features work
uniformly across every protocol:

1. **Dry-run mode.** The GUI can show the operator the encoded bytes,
   plus a diff against the device's current state, before any transmission.
2. **Confirmation dialogs.** The dialog shows real bytes, real target,
   real value — not an approximation.
3. **Audit log fidelity.** Every authorisation request, denial, and
   committed write is recorded with the same payload shape across
   protocols.

A driver that fuses prepare and commit will fail review. The split is
load-bearing.

## The safety model

Three layers protect against accidental writes on a live system:

### 1. Session profile

Chosen by the operator at connection time, the profile is a member of the
`SessionProfile` enum and cannot be relaxed for the life of the session
(only tightened):

| Profile | Confirmation requirement |
| --- | --- |
| `LAB` | Single-click confirm. Intended for benchtop test rigs. |
| `COMMISSIONING` | Explicit confirm dialog with target + value before each write. |
| `PRODUCTION` | Operator must type the target tag back to confirm, like `kubectl delete --confirm=<name>`. |

The profile is part of the `SafetyContext` passed to every `DriverSession`
when it is opened.

### 2. SafetyContext

Drivers do not call confirmation dialogs directly. They call
`safety.require_write_authorization(intent)` and either receive `True`
(proceed) or `False` (deny). The `SafetyContext` resolves the call through
the configured profile, records both outcome paths in the audit log, and
returns. Drivers must refuse to send if authorisation is denied.

### 3. Append-only signed audit log

`protoskipper.core.audit.AuditLog` writes every read, write authorisation,
committed write, and protocol error to a per-session SQLite WAL file.
Each row is chained via a SHA-256 hash of the previous row and signed
with an HMAC keyed off a per-session secret. The verification helper
`verify_log()` re-derives the chain and detects any post-hoc edit.

The threat model is explicit: the audit log defends against accidental
loss and *post-hoc* tamper attempts. It does not defend against an
attacker with full filesystem access at write time. What it provides is
that, once a session closes and the key is published in the
`session_meta` table, any later modification leaves evidence.

## Plugin discovery

`protoskipper.core.plugin_loader.load_protocol_drivers()` calls
`importlib.metadata.entry_points()` for the `protoskipper.protocols`
group. A plugin package's `pyproject.toml` registers itself like:

```toml
[project.entry-points."protoskipper.protocols"]
"dnp3" = "protoskipper_dnp3.driver:DNP3Driver"
```

Discovery is cached per-process; `plugin_loader.reload()` drops the cache
for developer scenarios (`pip install -e .`). A plugin that fails to
import is logged and skipped — one bad plugin must not prevent the rest
of the application from starting.

Built-in drivers register through the same mechanism in the project's
own `pyproject.toml`. There is no privileged registration path.

## Data flow: a complete write

To make the architecture concrete, here is the full sequence when an
operator clicks "Write 230 to holding[40001]" on a Modbus TCP device:

1. GUI converts the click into a call: `session.driver_session.prepare_write(ref, 230)`.
2. The Modbus driver encodes 230 as `b"\x00\xe6"` and returns a
   `WriteIntent` with the encoded bytes and a human-readable description.
3. GUI passes the intent to its confirmation dialog (which is the
   `confirm_callback` on the `SafetyContext`).
4. Operator confirms (or, in `PRODUCTION`, types the tag name back).
5. `safety.require_write_authorization(intent)` returns `True` and writes
   an `event="write_authorization"` row to the audit log.
6. GUI calls `session.driver_session.commit_write(intent)`.
7. The Modbus driver re-checks the safety context, then transmits
   function code 0x06 to the device.
8. On response, the driver calls `safety.record_write_outcome(result)` which
   writes an `event="write_committed"` row (success) or `event="write_failed"`
   row (transport error) to the audit log. Every authorised write therefore
   has a matching outcome row.
9. GUI updates the watchlist and packet view.

If the operator denies in step 4, steps 6–9 never run, but the audit log
*does* record the denied authorisation. Forensic reconstruction can tell
the difference between "wasn't asked" and "was asked, declined".

## Where to read first

| Question | File |
| --- | --- |
| What can a driver do? | `src/protoskipper/core/driver.py` |
| How do plugins register? | `src/protoskipper/core/plugin_loader.py` |
| What does the audit log record? | `src/protoskipper/core/audit.py` |
| How is a session opened? | `src/protoskipper/core/session.py` |
| What does a Modbus driver look like? | `src/protoskipper/builtin_drivers/modbus/driver.py` |
| What does an IEC 61850 driver look like? | `plugins-builtin/protoskipper-iec61850/src/protoskipper_iec61850/driver.py` |
| How does GOOSE pub/sub work? | `plugins-builtin/protoskipper-iec61850/src/protoskipper_iec61850/goose/` |
| How is the GUI laid out? | `src/protoskipper/gui/main_window.py` |

## Where the architecture is going (roadmap)

This document describes the current state and intended evolution:

* **Phase 1 (done)** — Modbus TCP end-to-end, safety profiles, audit log,
  GUI shell, plugin contract.
* **Phase 2** — IEC 60870-5-104, capture/replay pipeline (pcapng with
  custom block types), packet-view rendering, watchlist live updates.
* **Phase 3** — BACnet/IP discovery and object browsing, scripting REPL
  with full driver bindings, register-map import for Modbus.
* **Phase 4 (done)** — IEC 61850 MMS (connect, browse, read/write,
  reporting BRCB/URCB, logging, file services, setting-group services)
  plus GOOSE subscriber and publisher with burst retransmission schedule
  (IEC 61850-8-1) and GUI panels. SCL parser and diff engine.
* **Phase 5+** — Community plugin ecosystem (DNP3, OPC UA, Profibus,
  vendor-specific stacks), simulation features, vendor-specific GUI
  contributions, cross-protocol scripted test workflows.

The contract documented in `driver.py` is intended to be stable through
all of these. Breaking changes to the ABC are major-version events and
will be telegraphed at least one minor release in advance.
