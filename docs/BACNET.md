# BACnet User Guide

> **Applies to:** ProtoSkipper ≥ 0.7.0
> **Protocol driver:** `bacnet.ip` (built-in)
> **Stack:** bacpypes3 ≥ 0.0.106

---

## Table of contents

1. [Overview](#1-overview)
2. [Quick start](#2-quick-start)
3. [Opening a BACnet/IP connection](#3-opening-a-bacnetip-connection)
4. [Discovering devices](#4-discovering-devices)
5. [Reading properties](#5-reading-properties)
6. [Writing properties](#6-writing-properties)
7. [Change-of-Value (COV) subscriptions](#7-change-of-value-cov-subscriptions)
8. [Alarms and events](#8-alarms-and-events)
9. [Trend logs](#9-trend-logs)
10. [Schedules and calendars](#10-schedules-and-calendars)
11. [File services](#11-file-services)
12. [Device management](#12-device-management)
13. [BBMD and foreign-device routing](#13-bbmd-and-foreign-device-routing)
14. [Built-in device simulator](#14-built-in-device-simulator)
15. [Setup files (bacnet-setup.json)](#15-setup-files-bacnet-setupjson)
16. [BACnet preferences](#16-bacnet-preferences)
17. [Audit log](#17-audit-log)
18. [Safety profiles](#18-safety-profiles)
19. [Vendor profiles](#19-vendor-profiles)
20. [Troubleshooting](#20-troubleshooting)
21. [CLI reference](#21-cli-reference)

---

## 1. Overview

ProtoSkipper's BACnet driver speaks **BACnet/IP (Annex J, IPv4)** using the
[bacpypes3](https://bacpypes3.readthedocs.io/) asyncio stack. It implements:

- **Discovery** — Who-Is / I-Am, Who-Has / I-Have (§12.11)
- **Property reads** — ReadProperty (RP), ReadPropertyMultiple (RPM), ReadRange (§15.7)
- **Property writes** — WriteProperty (WP), WritePropertyMultiple (WPM) (§15.9)
- **COV** — SubscribeCOV, SubscribeCOVProperty, confirmed and unconfirmed notifications
- **Alarms and events** — GetEventInformation, AcknowledgeAlarm
- **Trend logs** — ReadRange on `logBuffer` for TrendLog objects
- **Schedules** — RPM of WeeklySchedule / ExceptionSchedule + WriteProperty updates
- **File services** — AtomicReadFile / AtomicWriteFile (stream access)
- **Device management** — TimeSync, UTCTimeSync, ReinitializeDevice, DeviceCommunicationControl
- **BBMD** — read BDT/FDT, register as foreign device

The driver lives in `src/protoskipper/builtin_drivers/bacnet/` and loads
automatically through the `protoskipper.protocols` entry-point group —
no installation step is needed beyond `pip install -e ".[all]"`.

---

## 2. Quick start

```bash
# GUI
protoskipper-gui

# CLI — list all BACnet devices on the local network
protoskipper scan bacnet.ip 192.168.1.255/47808

# CLI — read a single property
protoskipper read "bacnet.ip://192.168.1.10:47808/dev=1234" AV:1.present-value
```

GUI workflow:

1. **File → New Connection…** → choose *BACnet/IP*, fill in Device-ID and address, click **Connect**.
2. The device tree populates with every object enumerated via RPM.
3. Click any object row — the object browser shows its full property list.
4. Right-click → **Read** or drag to the Watchlist for continuous polling.
5. Right-click → **Write…** to send a WriteProperty (safety-gated on COMMISSIONING/PRODUCTION profiles).

---

## 3. Opening a BACnet/IP connection

### 3.1 New Connection dialog

**File → New Connection…** (Ctrl+N) opens the dialog. For BACnet/IP:

| Field | Description | Default |
|-------|-------------|---------|
| Protocol | Select *BACnet/IP* | — |
| Device ID | Integer 0–4194302 | — |
| IP address | Target controller IP | — |
| UDP port | Controller port | 47808 |
| Operator | Your name or email (audit log) | Preferences default |
| Safety profile | LAB / COMMISSIONING / PRODUCTION | LAB |

The address is encoded internally as `<ip>:<port>/dev=<device_id>`, e.g.
`192.168.1.10:47808/dev=1234`.

### 3.2 Programmatic / scripting

```python
from protoskipper.core.driver import DeviceRef, SessionProfile
from protoskipper.builtin_drivers.bacnet.driver import BacnetIpDriver

device = DeviceRef(
    protocol="bacnet.ip",
    address="192.168.1.10:47808/dev=1234",
    label="AHU-1",
)
drv = BacnetIpDriver()
session = drv.connect(device, safety=my_safety_ctx)
```

---

## 4. Discovering devices

### 4.1 GUI

**Tools → Probe Network…** — enter a broadcast address (e.g. `192.168.1.255`),
click **Scan**. All devices that respond to Who-Is appear in the results table.
Double-click any row to open a connection.

### 4.2 CLI

```bash
protoskipper scan bacnet.ip 192.168.1.255/47808
```

Output columns: Device-ID, IP address, vendor name, model name, firmware.

### 4.3 Who-Is range

Restrict the scan to a specific Device-ID range in **Preferences → BACnet →
Who-Is range** (e.g. `1-1000,2000`). Leaving the field empty scans the full
range 0–4194302. A narrower range reduces broadcast traffic on large networks.

### 4.4 Who-Has

To find an object by name across all devices:

```python
results = session.who_has("AV:1")          # by object identifier
results = session.who_has(name="AHU-SA-T") # by object name
```

---

## 5. Reading properties

### 5.1 Single read (ReadProperty)

```python
result = session.read(ref)          # ObjectRef → ReadResult
print(result.value, result.quality) # value is str; quality is Quality.GOOD / BAD / …
```

### 5.2 Batch read (ReadPropertyMultiple)

RPM is used automatically when `read_many()` is called. The driver batches
references into groups of `bacnet_rpm_batch_size` (default 16, configurable in
Preferences → BACnet) per request.

```python
refs = list(session.enumerate_objects())
results = session.read_many(refs)
```

### 5.3 Array and range reads

```python
# Read a specific array element (index 0 = array size)
result = session.read_property_array(ref, index=2)

# ReadRange — pull a slice of a log buffer or priority array
entries = session.read_range(ref, prop="log-buffer", count=100)
```

### 5.4 Priority array

For commandable objects (Analog Output, Binary Output, etc.) the full
16-level priority array is returned when reading `priority-array`. Element
at index `i` is `None` if that priority slot is empty (Null in BACnet terms).

---

## 6. Writing properties

### 6.1 Write workflow

ProtoSkipper enforces a **prepare → confirm** two-step write:

```python
intent = session.prepare_write(ref, value=22.5, priority=8)
result = session.commit_write(intent)
print(result.success, result.error)
```

`prepare_write()` performs no I/O — it validates the value and returns a
`WriteIntent`. `commit_write()` transmits the WriteProperty request and
records the outcome in the audit log.

### 6.2 Write dialog (GUI)

Right-click any object in the device tree or object browser → **Write…**
Opens the Write dialog showing:
- Current value (read live before the dialog opens)
- New value input (type-appropriate widget: spin box for numeric, toggle for binary)
- Priority selector (1–16; disabled for non-commandable objects)
- Confirmation button (requires explicit approval on COMMISSIONING / PRODUCTION profiles)

### 6.3 Write-Many (WritePropertyMultiple)

```python
intents = [session.prepare_write(r, v) for r, v in pairs]
results = session.write_many(intents)
```

WPM batches all writes into a single `WritePropertyMultiple` request,
reducing round-trips on slow links.

### 6.4 Priority release

To release a commandable object from a priority slot (write Null):

```python
intent = session.prepare_write(ref, value=None, priority=8)
session.commit_write(intent)
```

---

## 7. Change-of-Value (COV) subscriptions

### 7.1 Subscribe

```python
# Unconfirmed COV on present-value, 5-minute lifetime
session.subscribe_cov(ref, lifetime_s=300, confirmed=False)

# Confirmed COV on a specific property with increment
session.subscribe_cov_property(ref, prop="present-value", increment=0.5, lifetime_s=600)
```

Notifications arrive as `ReadResult` updates on the watchlist and trigger
any registered callbacks.

### 7.2 Defaults

The **COV default lifetime** can be set in **Preferences → BACnet → COV
default lifetime** (default 300 s; 0 = indefinite). Subscriptions are
automatically renewed before expiry.

### 7.3 COV in the GUI

In the Watchlist panel, right-click any row → **Subscribe COV** to switch
from polled to COV-driven updates. An icon in the row indicates the active
subscription and its remaining lifetime.

---

## 8. Alarms and events

```python
# Poll for all active/unacknowledged event summaries
events = session.get_event_information()
for ev in events:
    print(ev.object_identifier, ev.event_state, ev.notify_type)

# Acknowledge an alarm
session.acknowledge_alarm(
    ref,
    event_state="fault",
    ack_text="Checked and cleared",
    timestamp=None,   # None = use current UTC time
)
```

The Alarm & Event panel (when a BACnet session is active) shows a live table
of all outstanding events. Double-click any row to acknowledge.

> **Safety gate:** `acknowledge_alarm()` calls
> `safety.require_write_authorization()` and records a `BacnetEvent.ALARM_ACK`
> row in the audit log.

---

## 9. Trend logs

```python
# Pull the last 200 entries from a TrendLog object
entries = session.read_trend_log(
    ref,          # ObjectRef for the TrendLog object
    count=200,    # number of log entries (negative = from tail)
)
for entry in entries:
    print(entry.timestamp, entry.value, entry.status_flags)
```

Entries are returned as a list of `TrendLogEntry` named tuples.
The driver uses `ReadRange` with `ByPosition` ranging to page through logs.

---

## 10. Schedules and calendars

### 10.1 Read a schedule

```python
sched = session.read_schedule(ref)  # ObjectRef for a Schedule object
print(sched["weekly-schedule"])
print(sched["exception-schedule"])
print(sched["schedule-default"])
```

RPM fetches all 10 schedule-relevant properties in one request.

### 10.2 Update the default value

```python
intent = session.prepare_write(ref, value=21.0, prop="schedule-default")
session.commit_write(intent)
```

Full `weeklySchedule` and `exceptionSchedule` editing (P7.B.11 roadmap item)
and the Schedule/Calendar timeline editor (P7.H.5) are planned for a future
release.

---

## 11. File services

```python
# Stream-read a BACnet file object
data = session.read_file(ref)           # returns bytes

# Stream-write a BACnet file object (safety-gated)
intent = session.prepare_write_file(ref, data=b"...")
session.commit_write(intent)
```

Chunked transfer handles files up to the network MTU per chunk (default 1024 B).

---

## 12. Device management

> All device-management operations are safety-gated. On PRODUCTION profile they
> require explicit confirmation.

```python
# Synchronise time (UTC)
session.time_sync(utc=True)

# Warm restart
session.reinitialize_device(state="warmstart", password="")

# Disable communications for 30 s
session.device_communication_control(time_s=30, enable=False, password="")
```

---

## 13. BBMD and foreign-device routing

### 13.1 Reading the BDT / FDT

```python
bdt = session.read_bdt()    # list[BdtEntry]
fdt = session.read_fdt()    # list[FdtEntry]
```

Each `BdtEntry` contains `address`, `port`, `mask`. Each `FdtEntry` contains
`address`, `port`, `ttl`, `remaining_ttl`.

### 13.2 Registering as a foreign device

Set **Use BBMD as Foreign Device** and fill in **BBMD address** in the New
Connection dialog. The driver sends `Register-Foreign-Device` on connect and
re-registers before the TTL expires.

### 13.3 BBMD routing visualisation

The BBMD/Routing panel (planned P7.E.3) will show a live topology diagram of
all BBMDs and foreign devices on the BVLC network.

---

## 14. Built-in device simulator

ProtoSkipper ships a BACnet/IP device simulator that can impersonate any device
with any object list. It is useful for:

- Testing a GUI or script without real hardware
- Integration testing in CI (no live network required)
- Demonstrating BACnet behaviour in training

### 14.1 Starting the simulator

```python
from protoskipper.builtin_drivers.bacnet.simulator import BacnetSimulator

sim = BacnetSimulator()
sim.start_simulator({
    "device_id": 1234,
    "address": "127.0.0.1",
    "port": 47808,
    "objects": [
        {"type": "analog-value",  "instance": 1, "objectName": "Zone-Temp",
         "presentValue": 21.5, "units": "degrees-celsius", "covIncrement": 0.5},
        {"type": "binary-output", "instance": 1, "objectName": "Fan-Run",
         "presentValue": False},
        {"type": "multi-state-value", "instance": 1, "objectName": "Mode",
         "presentValue": 1, "numberOfStates": 3,
         "stateText": ["Off", "Heating", "Cooling"]},
    ],
})
```

`start_simulator()` blocks until the UDP socket is bound (up to 10 s).

### 14.2 Supported object types

| Long form | Short alias | Notes |
|-----------|-------------|-------|
| `analog-input` | `ai` | Read-only |
| `analog-output` | `ao` | Commandable (priority array) |
| `analog-value` | `av` | Read-write |
| `binary-input` | `bi` | Read-only |
| `binary-output` | `bo` | Commandable |
| `binary-value` | `bv` | Read-write |
| `multi-state-input` | `msi` | Read-only |
| `multi-state-output` | `mso` | Commandable |
| `multi-state-value` | `msv` | Read-write |

### 14.3 Injecting values

```python
sim.update_value("analog-value:1", "presentValue", 25.0)
val = sim.get_value("analog-value:1", "presentValue")
```

Both calls are thread-safe — they schedule work on the asyncio loop that owns
the simulator and block until complete (5 s timeout).

### 14.4 Stopping the simulator

```python
sim.stop_simulator()
```

---

## 15. Setup files (bacnet-setup.json)

A *setup file* captures a complete BACnet commissioning session — transport
configuration, all connected devices and their points lists, watchlist,
COV subscriptions, and bench layout — in a single JSON file that can be
reopened later or shared with a colleague.

### 15.1 Save a setup

```python
from pathlib import Path
from protoskipper.builtin_drivers.bacnet.setup import BacnetSetup, DeviceSetup, WatchlistEntry

setup = BacnetSetup(name="Floor-3-HVAC", operator="alice@example.com")
setup.devices.append(
    DeviceSetup(
        device_id=1234,
        address="192.168.1.10:47808",
        watchlist=[
            WatchlistEntry(objid="AV:1", poll_ms=1000),
            WatchlistEntry(objid="AV:2", poll_ms=1000),
        ],
    )
)
setup.save(Path("floor3.bacnet-setup.json"))
```

### 15.2 Load a setup

```python
setup = BacnetSetup.load(Path("floor3.bacnet-setup.json"))
for dev in setup.devices:
    print(dev.device_id, dev.address)
```

### 15.3 File format

The file is UTF-8 JSON. Top-level keys:

| Key | Type | Description |
|-----|------|-------------|
| `$schema` | string | URL of the JSON schema (for IDE auto-complete) |
| `version` | int | Schema version (currently `1`) |
| `name` | string | Human-readable session name |
| `operator` | string | Operator who created the file |
| `created` | string | ISO-8601 UTC timestamp (auto-set on first save) |
| `transport` | object | `TransportConfig` — interface, port, BBMD settings |
| `devices` | array | `DeviceSetup` objects |
| `device_simulators` | array | Simulator configs (future use) |
| `audit` | object | Audit log pointer metadata |
| `bench_layout` | object | `{ tiles: [BenchTile] }` |

> **Security:** The file intentionally contains **no passwords or keys**.
> Credentials are stored in the OS keychain and referenced by a service name.

---

## 16. BACnet preferences

**Tools → Preferences… → BACnet tab**

| Setting | Default | Description |
|---------|---------|-------------|
| Local UDP port | 47808 | Port the driver binds when creating a new session |
| APDU timeout | 3000 ms | Time to wait for a confirmed-service reply |
| APDU retries | 3 | Number of re-transmissions before declaring a timeout |
| COV default lifetime | 300 s | Subscription lifetime; 0 = indefinite |
| RPM batch size | 16 | Max properties per ReadPropertyMultiple request |
| Vendor ID (I-Am) | 0 | Vendor ID reported in our own I-Am; 0 = ASHRAE |
| Who-Is range | *(empty)* | Limit Who-Is to a Device-ID range, e.g. `1-1000,2000` |

All values are read by the driver when opening a new session. They can be
overridden per-session by the values in the New Connection dialog or a setup file.

Access these defaults programmatically:

```python
from protoskipper.gui.dialogs.preferences import PreferencesDialog

port     = PreferencesDialog.bacnet_local_port()        # int
timeout  = PreferencesDialog.bacnet_apdu_timeout_ms()   # int
retries  = PreferencesDialog.bacnet_apdu_retries()      # int
cov_lt   = PreferencesDialog.bacnet_cov_lifetime_s()    # int
batch    = PreferencesDialog.bacnet_rpm_batch_size()    # int
vid      = PreferencesDialog.bacnet_vendor_id()         # int
wrange   = PreferencesDialog.bacnet_who_is_range()      # str
```

---

## 17. Audit log

Every read, write, COV subscription, alarm acknowledgement, and device-management
action is recorded in a HMAC-chained, append-only SQLite audit log. See
`docs/ARCHITECTURE.md §Audit` for the chain verification protocol.

BACnet-specific event types are defined in
`src/protoskipper/builtin_drivers/bacnet/audit_schema.py`:

| Constant | Meaning |
|----------|---------|
| `BacnetEvent.CONNECT` | Session opened |
| `BacnetEvent.DISCONNECT` | Session closed |
| `BacnetEvent.READ` | Single RP or RPM read |
| `BacnetEvent.WRITE` | Single WP write |
| `BacnetEvent.WRITE_MANY` | WPM batch write |
| `BacnetEvent.READ_RANGE` | ReadRange (trend log page) |
| `BacnetEvent.ALARM` | Event notification received |
| `BacnetEvent.ALARM_ACK` | Alarm acknowledged |
| `BacnetEvent.COV_SUBSCRIBE` | COV subscription created or renewed |
| `BacnetEvent.DISCOVERY` | Who-Is / Who-Has scan |
| `BacnetEvent.DEVICE_MGMT` | TimeSync / Reinitialize / DCC |
| `BacnetEvent.ROUTING` | BDT / FDT read |
| `BacnetEvent.ERROR` | Protocol error logged |

Verify the audit log:

```bash
protoskipper audit verify ~/.protoskipper/audit/session-<id>.db
```

---

## 18. Safety profiles

| Profile | Write allowed? | Confirmation required? | Typical use |
|---------|---------------|----------------------|-------------|
| LAB | Yes | No | Test bench, simulator |
| COMMISSIONING | Yes | Yes (dialog) | Live commissioning |
| PRODUCTION | No (default) | N/A | Read-only monitoring |

On a **PRODUCTION** session, `prepare_write()` raises `WriteNotAllowedError`
unless `safety.override_production_lock()` has been called. The override itself
is audit-logged.

---

## 19. Vendor profiles

Vendor profiles pre-configure known quirks for popular BMS platforms. Select
a profile in the New Connection dialog — it adjusts RPM batch size, proprietary
object types, property encoding, and error-recovery heuristics automatically.

| Profile key | Vendor | Notes |
|-------------|--------|-------|
| `generic` | Any | Strict standard compliance |
| `jci_metasys_nae` | Johnson Controls Metasys NAE/NIE | Proprietary object types |
| `honeywell_ebi` | Honeywell EBI | Large RPM batches OK |
| `trane_tracer` | Trane Tracer SC+ | Slow APDU timeout needed |
| `siemens_apogee` | Siemens APOGEE/Desigo CC | N-point profile |
| `abb` | ABB | Standard-compliant |
| `schneider_ebo` | Schneider Electric EBO | SmartX variants |

Community-contributed vendor profiles are in
`src/protoskipper/builtin_drivers/bacnet/vendor_profiles/`.

---

## 20. Troubleshooting

### No devices found after Who-Is

1. Check the broadcast address — use the subnet broadcast, not 255.255.255.255
   (many switches block it). E.g. for a /24 network: `192.168.1.255`.
2. Verify UDP port 47808 is open on the firewall (both directions).
3. If devices are on a different subnet, configure BBMD or register as a
   foreign device (**New Connection → Use BBMD as Foreign Device**).
4. Narrow the **Who-Is range** (Preferences → BACnet) if the broadcast triggers
   too many simultaneous I-Am replies and saturates the link.

### Timeout errors on read

- Increase **APDU timeout** (Preferences → BACnet → APDU timeout). Some Trane
  Tracer SC+ units need 6000 ms or more.
- Reduce **RPM batch size** — some devices reject RPMs with more than 8 properties.
- Check for a BBMD/router in the path. The router may drop packets if the
  NPDU network number is wrong.

### Write rejected

- Confirm you are not on a **PRODUCTION** profile. Switch to **COMMISSIONING**
  in the New Connection dialog.
- Some objects require a **password** in the WriteProperty request. Use the
  Advanced section of the Write dialog to supply one.
- Check the **priority** — writing at a lower priority than an active
  override will appear to succeed but the present value will not change.

### COV notifications not arriving

- Ensure the device can reach your IP. If you are behind NAT, use the
  **foreign-device** flow so the BBMD delivers notifications to the registered
  address.
- Confirmed COV subscriptions (enabled by default) will retransmit if the
  SimpleACK is lost — check that your firewall does not block incoming UDP.

### Audit log chain broken

```bash
protoskipper audit verify <path>
```

Output will show the first row where the HMAC chain breaks. A broken chain
indicates the file was modified after writing. Export a tamper-evidence
report with `protoskipper audit report <path> --format pdf`.

---

## 21. CLI reference

```
protoskipper scan bacnet.ip <broadcast-addr>[:<port>]
    Discover all BACnet/IP devices. Default port 47808.

protoskipper read "bacnet.ip://<addr>:<port>/dev=<id>" <obj-prop>
    Read a single property. obj-prop format: "AV:1.present-value"

protoskipper write "bacnet.ip://<addr>:<port>/dev=<id>" <obj-prop> <value> [--priority N]
    Write a property value.

protoskipper audit verify <path>
    Verify the HMAC chain of an audit log file.

protoskipper audit report <path> [--format json|csv|pdf]
    Export an audit report.

protoskipper list-protocols
    List all loaded protocol drivers.
```

---

*ProtoSkipper BACnet driver — maintained by DataSailors Pvt Ltd (GPL-3.0-or-later)*
