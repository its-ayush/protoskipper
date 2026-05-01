# ProtoSkipper GUI Architecture

> Design document, not a status report. The implementation in
> `src/protoskipper/gui/` is built against this document; if the code and
> the document disagree, the document wins until it is consciously
> revised.

This document covers the GUI tier of ProtoSkipper. The protocol-agnostic
core is documented separately in [`ARCHITECTURE.md`](ARCHITECTURE.md);
this document explains how the user-facing application sits on top of
that core, what threading and state guarantees it provides, and what
each screen does.

The intended reader is a developer who is going to (a) modify the GUI,
(b) write a plugin that contributes GUI panels, or (c) audit the GUI's
safety story. It assumes the reader has read the core architecture doc.

---

## 1. Goals (what the GUI is for)

ProtoSkipper is a tool a SCADA / BMS engineer carries on a laptop into a
substation, a BMS plant room, or a panel-builder's workshop. The GUI
must therefore be:

* **Honest about state.** The operator must always know which device is
  selected, which session profile is active, where the audit log is being
  written, and whether a write actually went on the wire. Hidden state
  causes incidents.
* **Cheap to abandon.** A click or a keystroke must never silently start
  a long operation that cannot be cancelled. Every operation surfaces a
  visible progress indicator and a way to back out.
* **Forgiving in LAB, strict in PRODUCTION.** The same UI elements
  behave differently as the session profile escalates. The behaviour is
  centralised in the safety dialogs so panels do not duplicate the
  policy.
* **Coherent across protocols.** A user who has driven the Modbus side
  should be able to drive the BACnet side without re-learning the
  conventions. Per-protocol idioms are exposed via *contributions*, not
  via per-protocol main views.
* **Cross-platform without uncanny-valley styling.** Native menu and
  window-frame behaviour on each OS (Windows, macOS, Linux) but a
  consistent palette, density, and iconography inside the application
  frame.

What the GUI is **not** trying to be:

* A real-time HMI. Watchlist refresh is interactive, not millisecond.
* A configuration editor for production SCADA databases. The tool reads
  and writes to live devices; configuration export to a SCADA system is
  a downstream concern.
* A visual programming environment. Scripting belongs in the embedded
  REPL, not in the main canvas.

---

## 2. High-level component view

```
+----------------------------------------------------------------------+
|                            Qt main thread                            |
|  +----------------+   +--------------------+   +------------------+  |
|  |  MainWindow    |   |  Dialogs           |   |  Panels          |  |
|  |   - menu bar   |   |   - NewConnection  |   |   - DeviceTree   |  |
|  |   - toolbar    |   |   - WriteConfirm   |   |   - ObjectBrowser|  |
|  |   - dock layout|   |   - SafetyPrompt   |   |   - Watchlist    |  |
|  |   - status bar |   |   - About          |   |   - PacketView   |  |
|  +-------+--------+   +---------+----------+   +---------+--------+  |
|          |                      |                        |           |
|          +---------+------------+------------+-----------+           |
|                    |                         |                       |
|             +------v-------+         +-------v-------+               |
|             | ApplicationState        |  Qt Models   |               |
|             |  - QObject signals      |  - DeviceTree|               |
|             |  - sessions[]           |  - Watchlist |               |
|             |  - devices[]            |  - PacketLog |               |
|             |  - watchlist items[]    +--------------+               |
|             |  - active_profile       |                              |
|             +----------+--------------+                              |
|                        | (signals: device_added, session_opened,     |
|                        |  read_completed, packet_received, ...)      |
|                        |                                             |
|                        v                                             |
|              +---------+----------+                                  |
|              |  SessionManager    |                                  |
|              |   - dispatches to  |                                  |
|              |     workers        |                                  |
|              +---------+----------+                                  |
|                        |  invokeMethod (queued connection)           |
+------------------------|----------------------------------------------+
                         v
+----------------------------------------------------------------------+
|              Worker threads  (one QThread per active session)        |
|                                                                      |
|  +-----------------+    +-----------------+    +----------------+    |
|  | DriverWorker #1 |    | DriverWorker #2 |    | DriverWorker N |    |
|  |  - DriverSession|    |  - DriverSession|    |   - ...        |    |
|  |  - Modbus TCP   |    |  - BACnet/IP    |    |                |    |
|  +--------+--------+    +--------+--------+    +-------+--------+    |
|           |                      |                     |             |
+-----------|----------------------|---------------------|-------------+
            v                      v                     v
+----------------------------------------------------------------------+
|                  Core (already exists)                               |
|   ProtocolDriver / DriverSession / SafetyContext / AuditLog /        |
|   plugin_loader                                                      |
+----------------------------------------------------------------------+
```

Three rules govern this picture:

1. **Panels never call drivers directly.** They call `SessionManager`
   methods or read from `ApplicationState`. Direct driver access from
   inside a paint loop is the single most common way Qt apps hang.
2. **DriverSession instances live exclusively on their worker thread.**
   They are never touched from the UI thread. Cross-thread communication
   is via Qt's signals/slots with queued connections.
3. **Application state changes are the only way panels learn about the
   world.** No polling. Every change emits a signal; panels reactively
   update.

---

## 3. Threading model

### 3.1 Why threads, not asyncio

We considered `qasync` (running an asyncio loop inside Qt's event loop)
and rejected it for v1. Reasons:

* Most protocol stacks we depend on (`pymodbus`, `bacpypes3`,
  `pyiec61850`) have idiomatic synchronous APIs; their async APIs are
  either not stable or not portable.
* Qt's signal/slot machinery already gives us thread-safe callbacks,
  cancellation, and back-pressure. Adding asyncio on top doubles the
  concurrency primitives without doubling the safety.
* QThread + queued connections is well-understood by Qt developers and
  by tooling. asyncio + Qt is not.

A driver MAY use asyncio internally inside its worker thread (run its
own loop, await on its own coroutines) — that is a private detail. From
the outside it looks like any other synchronous worker.

### 3.2 Anatomy of a worker

Each active session runs on its own `QThread`. A `DriverWorker` is a
`QObject` that lives on that thread, owns the underlying
`DriverSession`, and exposes slots for the operations the UI can
request:

```python
class DriverWorker(QObject):
    # Signals (emitted on the worker thread, received on the UI thread)
    discovery_progress = Signal(DeviceRef)         # progressively yielded
    discovery_finished = Signal(int)               # n devices found
    objects_enumerated = Signal(list)              # list[ObjectRef]
    read_completed     = Signal(ReadResult)
    write_authorized   = Signal(WriteIntent)       # before transmit
    write_completed    = Signal(WriteResult)
    error_raised       = Signal(str, str)          # (operation, message)

    # Slots (invoked from the UI thread via queued connection)
    @Slot(str)
    def start_discovery(self, target: str): ...

    @Slot(DeviceRef, SessionProfile, str)
    def open(self, device, profile, operator): ...

    @Slot(ObjectRef)
    def read(self, ref): ...

    @Slot(ObjectRef, object)
    def prepare_write(self, ref, value): ...

    @Slot(WriteIntent)
    def commit_write(self, intent): ...

    @Slot()
    def close(self): ...
```

The worker NEVER raises exceptions across the thread boundary. It
catches everything internally and emits `error_raised` instead. Panels
treat `error_raised` like any other state change.

### 3.3 Cancellation

A pending discovery, read, or capture must be cancellable. This is done
through a per-worker `QAtomicInt` cancel flag the worker checks at
yield points:

```python
def start_discovery(self, target):
    self._cancel.storeRelease(0)
    for device in self._driver.discover(target):
        if self._cancel.loadAcquire():
            break
        self.discovery_progress.emit(device)
    self.discovery_finished.emit(...)
```

The UI calls `worker.cancel()` (a slot that flips the flag). It does
NOT call `QThread.terminate()` — terminating a thread mid-write would
defeat the safety story, since the audit log would not record the
outcome.

### 3.4 Where the SafetyContext confirmation runs

This is subtle. `SafetyContext.require_write_authorization()` MUST
block the calling thread (the worker), but the confirmation dialog MUST
show on the UI thread. We resolve this by making the confirm callback
do a thread hop:

```
worker.commit_write(intent)
  -> safety.require_write_authorization(intent)
  -> confirm_callback(intent, profile)             # on worker thread
       -> QMetaObject.invokeMethod(
              gui_confirm_handler, "ask",
              Qt::BlockingQueuedConnection,        # blocks worker thread,
              Q_ARG(WriteIntent, intent),          # runs slot on UI thread,
              Q_RETURN_ARG(bool, result))          # returns the answer
       -> dialog.exec() on UI thread
       -> returns True/False to worker
  -> safety records audit row, returns result
  -> worker either transmits or raises AuthorizationDenied
```

`BlockingQueuedConnection` is the Qt mechanism that makes this work.
It is the only place we use it; everywhere else, queued connections
are non-blocking.

### 3.5 Threads we do NOT spawn

* **One thread per device on a multi-drop bus.** Modbus RTU and IEC 61850
  GOOSE share a transport; the worker for that transport multiplexes
  internally. The threading model is one worker per *session*, not per
  *device*.
* **A thread for the audit log.** Audit writes are SQLite calls; they
  are fast and synchronous on the session's worker thread. The WAL
  journal is what makes this not block visibly.

---

## 4. State management

### 4.1 The ApplicationState object

`ApplicationState` is a single `QObject` instantiated by `MainWindow`
and passed to every panel that needs it. Panels do not import it as a
module-level singleton; they receive it via constructor injection so
tests can substitute a fake.

```python
class ApplicationState(QObject):
    # Discovery / connection lifecycle
    device_discovered     = Signal(DeviceRef)
    device_lost           = Signal(DeviceRef)
    session_opened        = Signal(SessionId, DeviceRef, SessionProfile)
    session_closed        = Signal(SessionId)
    session_failed        = Signal(SessionId, str)

    # Per-session state
    objects_enumerated    = Signal(SessionId, list)        # list[ObjectRef]
    read_completed        = Signal(SessionId, ReadResult)
    write_intent_prepared = Signal(SessionId, WriteIntent)
    write_completed       = Signal(SessionId, WriteResult)
    write_denied          = Signal(SessionId, WriteIntent)

    # Watchlist
    watchlist_changed     = Signal()  # add/remove items; fine-grained later

    # Capture
    frame_captured        = Signal(SessionId, CapturedFrame)

    # Misc
    error_raised          = Signal(str, str)               # (operation, msg)
    profile_changed       = Signal(SessionId, SessionProfile)
```

State is held in plain dataclasses on the object; signals announce
mutations. Panels mutate state ONLY by calling `SessionManager`
methods — they never write to the dataclasses directly. This keeps the
mutation paths auditable and centralises the logging.

### 4.2 Why not Redux / a single store?

Qt's signal model already gives us most of Redux's benefits (single
source of truth, observable mutations) without the boilerplate. Adding
a Redux-style store would force every action through serialisation
that Qt's signal system handles natively. We revisit if and when we
introduce undo/redo (which would benefit from a transaction log).

### 4.3 Persistence

Three categories:

| Persisted | Where | When |
| --- | --- | --- |
| Window geometry, dock layout | `QSettings` | App close |
| Recent connections, default profile | `QSettings` | App close |
| Watchlist contents per profile | JSON in `~/.config/protoskipper/watchlists/` | Manual save |
| Audit logs | per-session SQLite WAL | Live |
| Captures | per-session pcapng | When user clicks Save Capture |

We deliberately do NOT auto-persist watchlists or captures. Operators
should explicitly choose to save substation telemetry.

---

## 5. Model-View

We use Qt's Model-View architecture for every list and tree:

| View | Model | Data |
| --- | --- | --- |
| Device tree | `DeviceTreeModel(QAbstractItemModel)` | Protocols → Devices → Sessions → Objects |
| Watchlist | `WatchlistModel(QAbstractTableModel)` | Tag, Value, Quality, Updated |
| Packet log | `PacketLogModel(QAbstractTableModel)` | Time, Direction, Protocol, Length, Decoded |
| Object browser | `ObjectBrowserModel(QAbstractTableModel)` | ID, Type, Access, Unit, Label |

Models subscribe to `ApplicationState` signals and emit
`dataChanged` / `rowsInserted` / `rowsRemoved` accordingly. Views are
stock `QTreeView` / `QTableView` instances — no custom painting in v1.

### 5.1 Why Model-View, not QStandardItemModel for everything

Convenience APIs (`QStandardItemModel`, `QTreeWidget`) are tempting but
copy data. With a real `QAbstractItemModel` the model is a thin
projection over `ApplicationState`; updates are O(1) and the underlying
datastore stays single-sourced.

### 5.2 Quality-coded coloring

Watchlist rows colour by `Quality`:

| Quality | Colour |
| --- | --- |
| GOOD | default text |
| UNCERTAIN | amber text |
| BAD / TIMEOUT | red text |
| SIMULATED | blue text + italic |
| UNKNOWN | grey text |

Colours come from a central palette in `gui/theme.py`, NOT hardcoded in
the model. Themes are swappable.

---

## 6. UI layout

### 6.1 Main window

```
+--------------------------------------------------------------------------------+
| File   Session   View   Tools   Help                                           |  menu bar
+--------------------------------------------------------------------------------+
| [+New Connection] [Disconnect]  Profile: LAB ▾   Audit: ~/.protoskipper/...    |  toolbar
+----------------+--------------------------------+------------------------------+
|                | Object Browser  |  Packet View |  Watchlist                   |
|  Device        |                 |              |  ┌────┬───────┬──────┬─────┐  |
|  Tree          |  Tag    Type    |  Time  Dir   |  │Tag │Value  │Qual  │When │  |
|                |  ─────  ─────   |  ────  ───   |  ├────┼───────┼──────┼─────┤  |
|  ▾ Protocols   |  hold:0 uint16  |  10:00 TX→   |  │... │       │      │     │  |
|    ▾ Modbus TCP|  hold:1 uint16  |  10:00 ←RX   |  │    │       │      │     │  |
|      ▾ 10.0.0.5|                 |              |  │    │       │      │     │  |
|        SESSION |                 |              |  │    │       │      │     │  |
|          hold:0|                 |              |  │    │       │      │     │  |
|          hold:1|                 |              |  │    │       │      │     │  |
|                |                 |              |  │    │       │      │     │  |
|                +-----------------+--------------+------------------------------+
|                | Scripting REPL                                                |
|                |  >>> drivers['modbus.tcp'].discover('10.0.0.0/24')            |
|                |  ...                                                          |
|                |                                                               |
+----------------+---------------------------------------------------------------+
| Profile: LAB | 2 driver(s) loaded | Audit: 0 rows this session                 |  status bar
+--------------------------------------------------------------------------------+
```

Panel docking is user-configurable; the layout above is the default.
The state of every dock is persisted via `saveState()` / `restoreState()`.

### 6.2 New Connection dialog

```
+ New Connection -------------------------------------------+
| Step 1 / 3 - Protocol                                     |
|                                                           |
|  ( ) Modbus TCP    Modbus over TCP/IP, RFC 6815           |
|  (•) Modbus RTU    Modbus over serial RS-485 / RS-232     |
|  ( ) BACnet/IP                                            |
|  ( ) IEC 60870-5-104                                      |
|  ( ) IEC 61850 MMS                                        |
|                                                           |
|                         [ Cancel ]  [ < Back ]  [ Next > ]|
+-----------------------------------------------------------+

+ New Connection -------------------------------------------+
| Step 2 / 3 - Target                                       |
|                                                           |
|  Address:  [ 10.0.0.5:502/unit=1                       ]  |
|  Label:    [ Feeder-1 RTU                              ]  |
|                                                           |
|  [ Test connection ]    Status: ✓ responded (12 ms)       |
|                                                           |
|                         [ Cancel ]  [ < Back ]  [ Next > ]|
+-----------------------------------------------------------+

+ New Connection -------------------------------------------+
| Step 3 / 3 - Session                                      |
|                                                           |
|  Profile:  ( ) LAB           single-click writes          |
|            (•) COMMISSIONING confirm dialog per write     |
|            ( ) PRODUCTION    type tag name to confirm     |
|                                                           |
|  Operator: [ kuldeep@datasailors.io                    ]  |
|  Audit:    [ ~/.protoskipper/audit/2026-05-01.../       ] |
|                                                           |
|  ⚠ COMMISSIONING profile cannot be relaxed for the life  |
|    of this session.                                       |
|                                                           |
|                  [ Cancel ]  [ < Back ]  [ Connect ]      |
+-----------------------------------------------------------+
```

### 6.3 Write confirmation (the safety path)

```
+ Write to holding[40001] ----------------------------------+
| Profile: COMMISSIONING                                    |
|                                                           |
| Target:    10.0.0.5:502 / unit=1 / holding[40001]         |
| Tag:       FEEDER1.SETPT.kV                               |
| Type:      uint16                                         |
| Unit:      kV                                             |
|                                                           |
| Current:   228                                            |
| New:       230                                            |
| Diff:      +2                                             |
|                                                           |
| Wire bytes: 06 01 00 00 00 E6 09 6E                       |
|                                                           |
| ⚠ This write will be transmitted immediately on confirm.  |
|   It cannot be undone from ProtoSkipper.                  |
|                                                           |
|              [ Cancel ]            [ Confirm Write ]      |
+-----------------------------------------------------------+
```

In `PRODUCTION` profile the same dialog has an additional input:

```
| Type the tag name to confirm:                             |
| [ FEEDER1.SETPT.kV                                     ]  |
+-----------------------------------------------------------+
```

with the Confirm button disabled until the typed text matches.

### 6.4 Capture/replay

```
File > Capture > Start          starts capturing the active session
File > Capture > Stop           stops, prompts to save .pcapng
File > Capture > Open Replay…   loads a .pcapng for inspection (no transmit)
File > Capture > Verify Audit…  runs verify_log() on a chosen audit file
```

The packet view shows captured frames live during a capture and is also
the inspection view for replays. Replays disable all write paths.

---

## 7. User journeys

### 7.1 Discover and connect

```
User clicks [+New Connection]
  → NewConnectionDialog shows
  → User picks protocol, enters target, picks profile, types operator
  → Dialog calls SessionManager.discover(protocol_id, target)
  → SessionManager creates a transient DriverWorker on a QThread
  → Worker emits discovery_progress signals as devices appear
  → Dialog shows "Test connection" result
  → User clicks Connect
  → SessionManager.open_session(...) creates a permanent worker,
    calls open_session() (core), gets a Session object on the worker thread
  → ApplicationState emits session_opened
  → DeviceTreePanel adds a node under Protocols → Modbus TCP → 10.0.0.5
  → ObjectBrowserPanel populates from objects_enumerated signal
```

### 7.2 Read on demand

```
User selects an object in the tree
  → ObjectBrowserPanel displays its row in the table
  → User clicks "Read now" (or presses F5)
  → Panel calls SessionManager.read(session_id, ref)
  → Worker calls driver_session.read(ref) on its thread
  → Worker emits read_completed
  → ApplicationState forwards read_completed
  → ObjectBrowserPanel updates the cell
  → PacketLogModel (subscribed to frame_captured) shows the bytes if
    capture is on
```

### 7.3 Write with safety

```
User right-clicks an object → "Write..."
  → WriteDialog opens with object metadata + a value input
  → User enters value, clicks Prepare
  → Dialog calls SessionManager.prepare_write(session_id, ref, value)
  → Worker calls driver_session.prepare_write — no transmission
  → Worker emits write_intent_prepared
  → Dialog renders the WriteIntent: bytes, target, diff vs current
  → User clicks Confirm Write (or types the tag in PRODUCTION)
  → Dialog calls SessionManager.commit_write(session_id, intent)
  → Worker calls driver_session.commit_write
    → SafetyContext.require_write_authorization()
      → confirm_callback() runs a BlockingQueuedConnection back to the
        UI thread, which re-shows the same dialog as a final yes/no
      → audit log records the authorization outcome
    → If authorized, driver transmits
  → Worker emits write_completed
  → ApplicationState updates the object's last-known value
  → Dialog closes
```

The two-prompt UX (Prepare → Confirm) is intentional. The first prompt
shows the operator the full picture; the second is the safety net the
audit log records as the explicit authorisation event.

### 7.4 Watch (manual refresh)

```
User drags an ObjectRef from the object browser to the watchlist
  → WatchlistPanel adds the row, ApplicationState records the membership
  → User clicks "Refresh" on the watchlist toolbar
  → Panel iterates rows, dispatches read() per row through SessionManager
  → Each read_completed signal updates the corresponding row
  → Quality-coded coloring applied via the role on the model
```

Live polling (configurable interval) is a follow-up; v1 ships manual
refresh only, by design — operators should explicitly trigger reads on
production substations.

### 7.5 Cancel and disconnect

```
User clicks the cancel button on a long discovery
  → Toolbar calls SessionManager.cancel(session_id)
  → SessionManager calls worker.cancel() (sets the QAtomicInt flag)
  → Worker exits its yield loop on the next check
  → Worker emits discovery_finished with the partial count
  → Tree panel shows "(cancelled, N found)"

User clicks Disconnect
  → SessionManager.close_session(session_id)
  → Worker.close() runs DriverSession.close() and AuditLog.close()
  → Thread quits, joined within 5s or hard-killed (last resort)
  → ApplicationState emits session_closed
  → Tree panel removes the session node
```

---

## 8. Plugin GUI contributions

A protocol plugin MAY ship custom panels. They are registered through a
second entry-point group:

```toml
[project.entry-points."protoskipper.gui_contributions"]
"iec61850" = "protoskipper_iec61850.gui:Iec61850Contributions"
```

`Iec61850Contributions` returns a list of `PanelContribution` objects
the GUI inserts into a "Plugins" menu and into a special "Plugin
Panels" tab area. Contributions get:

* read access to `ApplicationState`,
* a way to dispatch operations through `SessionManager` (the same way
  built-in panels do),
* a stable Qt 6 widget host.

They do NOT get direct access to driver instances (same rule as
built-in panels).

---

## 9. Error handling

A driver call can fail at three layers:

| Layer | Surfaced as | Display |
| --- | --- | --- |
| Encoding (`EncodingError`) | Returned via `error_raised` signal | Inline in the dialog that triggered it |
| Transport (`ConnectionFailure`) | Returned via `error_raised` | Status bar + dialog if user-initiated |
| Authorization (`AuthorizationDenied`) | Returned via `write_denied` signal | Dialog with a clear "denied by safety profile" message |

Generic `Exception` subclasses that *escape* a worker (we have a bug)
get caught by the worker's try/except and re-emitted as `error_raised`
with a "Internal error - please report" prefix.

The status bar always shows the last error with a clickable "View
details" that opens a log dialog.

---

## 10. Visual design / theming

We ship two themes initially (Light, Dark) in `gui/theme.py`. The
theme defines:

* a 9-step neutral palette,
* a 5-step accent palette (for the toolbar profile chip),
* a quality palette (good/uncertain/bad/simulated/unknown),
* font sizing with two density modes ("Comfortable" and "Compact" -
  Compact for small laptops in the field).

We do NOT ship custom widget styles. The native style is used on each
OS; only colours and densities differ between themes. This is the
single biggest deviation from the typical Electron app: ProtoSkipper
should look like a native Win32 / Cocoa / GTK app first and a branded
app second.

Iconography is from a single icon font (`Material Symbols`) bundled
with the app. No raster icons.

---

## 11. Accessibility & internationalisation

* All actionable widgets have keyboard shortcuts and proper tab order.
* Menus and toolbar buttons set `setAccessibleName()` so screen readers
  announce the right thing.
* Strings are wrapped in `self.tr()` from day one. We do not ship
  translations in v1 but the infrastructure is ready.

---

## 12. Packaging

| Platform | Tool | Output |
| --- | --- | --- |
| Windows | PyInstaller + Inno Setup | `ProtoSkipper-Setup-x64.exe` (signed) |
| macOS | PyInstaller + create-dmg + notarytool | `ProtoSkipper.dmg` (notarized) |
| Linux  | Briefcase or AppImage | `ProtoSkipper.AppImage` + .deb / .rpm |

Plugin packages are pure Python and `pip install`-able into the same
Python environment the bundled app uses (we expose the bundled
interpreter at `protoskipper --bundled-pip install …`).

---

## 13. Testing strategy

| Layer | Tool | Scope |
| --- | --- | --- |
| Core | `pytest` | Already done — driver contract, audit, session |
| GUI logic | `pytest-qt` | Models, services, dialogs (without showing windows) |
| GUI smoke | `pytest-qt` + `QApplication` | Construct main window, click buttons, assert state |
| End-to-end | manual + Modbus simulator | Real connection to `tests/integration/modbus_simulator.py` |
| Visual regression | not in v1 | Add later if we feel pain |

The Modbus simulator (in `tests/integration/`) is a real `pymodbus`
slave on `localhost:5020` with a small register map. A pytest fixture
starts and stops it; a CLI command (`protoskipper sim modbus`) exposes
it for manual demos.

We do NOT mock Qt. If a test cannot work without a real Qt event loop,
it runs against a real one. `pytest-qt` makes this cheap.

---

## 14. What this document does not cover (yet)

* Plugin marketplace / discovery beyond entry points (out of scope until
  community uptake).
* Mobile or tablet variant (decided against; target is desktop on a
  field-engineering laptop).
* Localisation pipeline beyond the `tr()` wrapping infrastructure.
* Visual regression / theming snapshot tests.

These get their own design docs when they become real concerns.

---

*Document owner: ProtoSkipper maintainers (DataSailors Pvt Ltd).*
*Changes to this document are PRs in the same repo.*
