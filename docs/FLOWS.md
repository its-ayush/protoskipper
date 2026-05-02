# ProtoSkipper — User Flows, Edge Cases & UX Design

_Living document. Updated whenever a flow changes. Owner: GUI layer._

---

## 1. Feature Inventory

| # | Feature | Status | Entry Point |
|---|---------|--------|-------------|
| 1 | Connect to Modbus TCP device | ✅ Complete | File → New Connection |
| 2 | Connect to Modbus RTU device | ✅ Complete | File → New Connection |
| 3 | Probe / scan a network for devices | ✅ Complete | File → Probe Network |
| 4 | Add a register manually to an open session | ✅ Implemented | Object Browser → Add Register… |
| 5 | Remove a register from a session | ✅ Implemented | Object Browser → Remove |
| 6 | Import a CSV register map | ✅ Complete | Device Tree → right-click → Import register map… |
| 7 | Read a single register | ✅ Complete | Object Browser → Read selected / F5 |
| 8 | Read all registers in a session | ✅ Complete | Object Browser → Read all / Shift+F5 |
| 9 | Write to a register (LAB profile) | ✅ Complete | Object Browser → Write… |
| 10 | Write to a register (COMMISSIONING / PRODUCTION) | ✅ Complete | Write… with confirm dialog |
| 11 | Add a register to the Watchlist | ✅ Complete | Object Browser → Add to Watchlist |
| 12 | Save / Load a Watchlist | ✅ Complete | Watchlist panel → Save… / Load… |
| 13 | Auto-restore Watchlist on reconnect | ✅ Complete | Automatic on session open |
| 14 | Reconnect a closed session | ✅ Complete | Device Tree → right-click → Reconnect |
| 15 | Add another unit on the same gateway | ✅ Complete | Device Tree → right-click → Add unit to same gateway… |
| 16 | View raw packet capture | ✅ Complete | Packet View tab |
| 17 | Save packet capture to pcapng | ✅ Complete | Capture toolbar |
| 18 | Load pcapng for replay / post-analysis | ✅ Complete | File → Load Capture |
| 19 | View HMAC-chained audit log | ✅ Complete | Tools → View Audit Log |
| 20 | Verify audit log integrity | ✅ Complete | Tools → View Audit Log → Verify |
| 21 | Preferences (theme, density, default profile) | ✅ Complete | Tools → Preferences |
| 22 | Switch light / dark theme | ✅ Complete | View → Theme |
| 23 | Compact / comfortable density | ✅ Complete | View → Density |

---

## 2. Flows

Each flow is described with: trigger, happy path, edge cases, and outputs.

---

### Flow 1 — Connect to a Modbus TCP Device

**Trigger**: File → New Connection (Ctrl+N), or clicking a discovered device in
the Device Tree after probing.

**Inputs collected by `NewConnectionDialog`**:

| Field | Type | Constraints | Default |
|-------|------|-------------|---------|
| Protocol | Combo | Must be a registered driver | First available |
| Host | Text | Non-empty hostname or IPv4/IPv6 | — |
| Port | Spinbox | 1–65535 | 502 |
| Unit ID | Spinbox | 1–247 | 1 |
| Label | Text | Optional | — |
| Session profile | Radio | LAB / COMMISSIONING / PRODUCTION | LAB |
| Operator | Text | Non-empty; recorded in audit log | From Preferences |

**Address string built**: `"{host}:{port}/unit={unit}"` e.g. `"10.10.172.74:4196/unit=101"`

**Happy path**:
1. Dialog validates: host non-empty, operator non-empty → Connect enabled.
2. `SessionManager.open_session()` spawns a QThread + DriverWorker.
3. Worker calls `ModbusTcpDriver.connect()`: opens TCP socket to host:port.
4. On success: `session_opened` signal → session appears in Device Tree.
5. Worker calls `enumerate_objects()` → object browser starts **empty** (Modbus
   has no self-description; user must add registers manually or import a map).
6. Status bar shows "Session opened".

**Edge cases**:

| Case | Handling |
|------|----------|
| Host unreachable / refused | `ConnectionFailure` → `session_failed` signal → QMessageBox "Could not connect" |
| Timeout (>3 s) | Same as above; timeout configurable via `device.metadata["timeout"]` |
| Port 0 or > 65535 | Prevented by spinbox range |
| Unit ID 0 or > 247 | Prevented by spinbox range |
| No protocol drivers installed | Protocol combo disabled; warning QMessageBox on attempt |
| Duplicate session (same host:port/unit) | Allowed; two independent sessions are created |
| Empty operator field | Connect button disabled |

**Outputs**:
- `SessionInfo` added to `ApplicationState._sessions`
- Object browser is **empty** until user adds registers or imports a map
- Audit log records session-open event

---

### Flow 2 — Connect to a Modbus RTU Device

**Inputs**:

| Field | Type | Constraints | Default |
|-------|------|-------------|---------|
| Protocol | Combo | modbus.rtu | — |
| Serial port | Combo + editable | Non-empty path (e.g. /dev/ttyUSB0, COM4) | First detected |
| Baud rate | Combo | 1200/2400/4800/9600/19200/38400/57600/115200 | 9600 |
| Unit ID | Spinbox | 1–247 | 1 |
| Parity | Fixed N for now (dialog shows N/E/O: future) | — | N |
| Stop bits | Fixed 1 for now | — | 1 |

**Address string built**: `"{port}@{baud},N,1/unit={unit}"` e.g. `"/dev/ttyUSB0@9600,N,1/unit=3"`

**Edge cases**:

| Case | Handling |
|------|----------|
| Port not present / in use by another process | `ConnectionFailure` → error dialog |
| Wrong baud rate | Connection succeeds but reads return Modbus exception or garbage values |
| Protocol switch TCP→RTU in dialog | Serial port fields appear; host/port fields disappear; no address bleed |
| Protocol switch RTU→TCP | TCP fields appear; serial fields disappear; no bleed |

---

### Flow 3 — Add a Register Manually

**Trigger**: Object Browser toolbar → "Add Register…" button. Only enabled when a
session is open and the GUI is not in replay mode.

**Dialog: `AddRegisterDialog`**

| Field | Type | Constraints | Notes |
|-------|------|-------------|-------|
| Table | Combo | Holding / Input / Coils / Discrete | Determines FC and access defaults |
| Address | Spinbox | 0–65535 | Wire (0-based); Modbus notation shown as hint |
| Data type | Combo | See table below | Coils/Discrete force "boolean" |
| Count | Spinbox | 1–120 | Auto-filled from data type; editable for ascii/utf16 |
| Byte order | Combo | Big (standard) / Little | Hidden for boolean/coils/discrete |
| Word order | Combo | Big (MSW first) / Little (LSW first) | Shown only when count > 1 |
| Label | Text | Optional | Friendly tag name |
| Engineering unit | Text | Optional | e.g. V, A, kWh, degC |
| Scale | Float | Any finite float ≠ 0 | Applied: displayed = raw × scale + offset |
| Offset | Float | Any finite float | Applied after scale |
| Access | Radio | Read-Only / Read-Write | Forced RO for Input/Discrete |

**Data types and register counts**:

| Type | Count | Notes |
|------|-------|-------|
| boolean | 1 | Coils/Discrete only |
| uint16 | 1 | — |
| int16 | 1 | — |
| uint32 | 2 | — |
| int32 | 2 | — |
| float32 | 2 | Most common for energy meters |
| uint64 | 4 | — |
| int64 | 4 | — |
| float64 | 4 | — |
| ascii | user-set | Bytes per register = 2 |
| utf16 | user-set | Bytes per register = 2 |

**Modbus address notation hints** (shown below the address spinbox):

| Table | Notation | Example (address=100) |
|-------|----------|-----------------------|
| Holding | 4xxxx | 40101 |
| Input | 3xxxx | 30101 |
| Coils | 0xxxx | 00101 |
| Discrete | 1xxxx | 10101 |

**Happy path**:
1. User opens dialog, selects Table = "Holding registers".
2. Enters Address = 100, Data type = float32.
3. Count auto-fills to 2. Byte/word order default to Big.
4. Optionally fills Label = "Voltage L1", Unit = "V", Scale = 0.1.
5. Access = Read-only (or Read-Write for holding).
6. Clicks "Add Register".
7. `ObjectRef(object_id="holding:100:2", data_type="float32", ...)` added to session.
8. Register appears in Object Browser. User can read it immediately.

**Built `object_id`**:
- Single-register: `"holding:100"` (count=1, so count omitted)
- Multi-register: `"holding:100:2"` (float32 needs 2 registers)
- String (8 regs): `"holding:100:8"`

**Built `metadata`**:
```python
{
  "byte_order": "big",   # or "little"
  "word_order": "big",   # or "little", only for count > 1
  "scale": 0.1,          # omitted if 1.0
  "offset": 0.0,         # omitted if 0.0
}
```

**Edge cases**:

| Case | Handling |
|------|----------|
| Table = Coils → data type forced to boolean | Dialog locks the data_type combo |
| Table = Discrete → RO forced | Access radio disabled |
| Table = Input → RO forced | Access radio disabled |
| Data type = float32, count manually set to 1 | Warning label "float32 needs 2 registers; count overridden" and count snapped |
| Data type = ascii, count = 0 | Add button disabled; validation message |
| scale = 0 | Validation error (division by zero on write) |
| address + count > 65535 | Warning shown, count clamped |
| Add duplicate (same table + address already in session) | Allowed; user may want to read same register with different types |

---

### Flow 4 — Remove a Register

**Trigger**: Object Browser toolbar → "Remove" button (enabled when a row is
selected). Also available via right-click context menu on a row.

**Happy path**:
1. User selects a register row.
2. Clicks "Remove".
3. No confirmation dialog (non-destructive; register can be re-added).
4. `ObjectRef` removed from session's object list.
5. Row disappears from Object Browser.

**Edge cases**:

| Case | Handling |
|------|----------|
| Register had a pending read in flight | Read result is discarded when it arrives (session object list no longer contains it) |
| Register is in the Watchlist | Watchlist entry remains (shows stale value with UNCERTAIN quality) |
| No row selected | Remove button disabled |

---

### Flow 5 — Read a Register

**Trigger**: "Read selected" button / F5 (single), "Read all" / Shift+F5 (batch).

**Inputs**: the selected `ObjectRef`(s).

**What happens on the wire** (Modbus):

| Table | FC | pymodbus call |
|-------|----|---------------|
| holding | FC03 | `read_holding_registers(address, count)` |
| input | FC04 | `read_input_registers(address, count)` |
| coils | FC01 | `read_coils(address, count)` |
| discrete | FC02 | `read_discrete_inputs(address, count)` |

**Read all** uses contiguous-block optimisation: registers in the same table at
consecutive addresses are fetched in a single FC03/FC04 request.

**Result displayed in Object Browser**:

| Quality | Display | Color |
|---------|---------|-------|
| GOOD | Decoded value ± engineering unit | Normal text |
| BAD | "Error: <message>" | Red |
| TIMEOUT | "Timeout" | Orange |
| UNCERTAIN | Last known value (stale) | Grey italic |

**Edge cases**:

| Case | Handling |
|------|----------|
| Transport dropped mid-read | Driver attempts one reconnect; if fails, QUALITY.BAD returned |
| Modbus exception code (e.g. illegal data address 0x02) | Mapped to QUALITY.BAD + error message |
| count > device's maximum response size | pymodbus handles splitting; driver uses per-register fallback if bulk fails |
| float32 decode with wrong byte/word order | Value will be wrong; user must adjust register definition and re-read |
| scale applied after decode | `value = raw × scale + offset` |
| Replay mode | Read button disabled; reads from recorded packet log |

---

### Flow 6 — Write to a Register

**Trigger**: "Write…" button in Object Browser (only enabled for rw registers).

**Phase 1 — Prepare**:
1. `WriteDialog` opens showing register label, current value (if last read available).
2. User types the desired value (engineering-unit domain, e.g. "230.5" for voltage).
3. Clicks "Prepare write".
4. `SessionManager.prepare_write()` calls driver `prepare_write()` on worker thread.
5. Driver encodes value: `invert_scale(value, scale, offset)` → raw integer → register list.
6. `WriteIntent` returned: contains raw bytes, Modbus PDU description, encoded registers.

**Phase 2 — Confirm** (depends on profile):

| Profile | Confirm mechanism |
|---------|------------------|
| LAB | "Confirm write" button; shows encoded bytes |
| COMMISSIONING | Separate `SafetyConfirmDialog` with Yes/No |
| PRODUCTION | User must type the tag name (object_id) back to confirm |

**What goes on the wire** (Modbus):

| Table | FC | pymodbus call |
|-------|----|---------------|
| holding | FC16 (multiple) | `write_registers(address, values)` |
| coils | FC15 (multiple) | `write_coils(address, values)` |
| Single holding | FC06 | `write_register(address, value)` when count=1 |
| Single coil | FC05 | `write_coil(address, value)` when count=1 |

**Audit log entry**: written regardless of outcome (authorization event + result event).

**Edge cases**:

| Case | Handling |
|------|----------|
| Write to read-only register | Write… button disabled |
| Value out of range for data type (e.g. 70000 for uint16) | `EncodingError` shown in dialog |
| Operator cancels after prepare | Intent recorded as "denied" in audit log; no bytes sent |
| Transport drops between prepare and commit | `WriteResult.success = False`; recorded in audit |
| Scale = 0 | `EncodingError` (division by zero during invert_scale) |
| PRODUCTION profile: operator types wrong tag | Write denied, audit records the failed attempt |

---

### Flow 7 — Probe / Scan Network

**Trigger**: File → Probe Network (Ctrl+P).

**Inputs**:

| Field | Notes |
|-------|-------|
| Protocol | Modbus TCP or RTU |
| Target | Host, CIDR (`10.0.0.0/24`), comma list, with optional `:port` and `/units=1-10` |

**What happens**:
1. `SessionManager.start_discovery()` spawns transient worker.
2. Worker iterates `driver.discover(target)`.
3. For TCP: ThreadPoolExecutor probes each (host, unit) pair with FC03 to address 0.
4. Devices that respond appear live in Device Tree as they are found.
5. When done: `discovery_finished` signal → status bar shows count.
6. User clicks a discovered device → `NewConnectionDialog` pre-filled with
   host, port, unit, label.

**Edge cases**:

| Case | Handling |
|------|----------|
| CIDR > 65536 hosts | `EncodingError` in dialog |
| No devices respond | Tree shows no new entries; status "0 devices found" |
| Probe cancelled by user | Cooperative cancel via `cancel_discovery()` |
| Probe finds a device already in an open session | Two entries for the same address (browse vs session) — expected |

---

### Flow 8 — Import CSV Register Map

**Trigger**: Device Tree → right-click open session → "Import register map…"

**CSV format** (see `builtin_drivers/modbus/regmap.py`):
```
# object_id,data_type,access,label,unit,scale,offset,byte_order,word_order
holding:0,float32,rw,Voltage L1,V,0.1,0,big,big
holding:2,float32,ro,Current L1,A,0.01,0,big,big
```

**Edge cases**:

| Case | Handling |
|------|----------|
| Missing sentinel row | `EncodingError` dialog |
| Bad numeric scale | Row skipped with warning log |
| object_id with unknown table | Row skipped |
| File encoding not UTF-8 | `EncodingError` dialog |
| Empty file | Zero objects imported; session object browser cleared |

---

### Flow 9 — Reconnect a Closed Session

**Trigger**: Device Tree → right-click closed session → "Reconnect".

**What happens**:
- `SessionManager.reconnect_session()` is called with the existing `SessionId`.
- The same `SessionId` is reused so the Device Tree entry updates in place.
- The session's stored `DeviceRef`, `SessionProfile`, and operator are used.
- Transport is re-opened; `enumerate_objects()` is called again.
- Objects that were manually added (via Add Register… or CSV import) are **lost**
  because `enumerate_objects()` returns empty. User must re-add them.
- **Future improvement**: persist the object list on the `SessionInfo` and
  re-inject it after reconnect.

**Edge cases**:
- Session is already open → reconnect is a no-op.
- Session ID not found in state → no-op.
- Reconnect transport fails → `session_failed` signal → error dialog.

---

### Flow 10 — Add Unit to Same Gateway

**Trigger**: Device Tree → right-click open TCP session → "Add unit to same gateway…"

**What happens**:
1. `QInputDialog` asks for a unit ID (1–247).
2. New address built: `"{host}:{port}/unit={new_unit}"`.
3. `SessionManager.open_session()` with same `SessionProfile` and operator.
4. New session appears as a sibling in the Device Tree.

**Edge cases**:
- RTU sessions: not supported (RTU sessions appear as open, but the gateway
  is a serial bus; the action is still available but might create a session to the
  same serial port with a different unit ID — this is correct Modbus RTU behaviour).
- Unit ID already in an open session on the same host:port: allowed; duplicate sessions
  are permitted (useful for side-by-side comparison).

---

### Flow 11 — Watchlist

**Trigger**: Object Browser → "Add to Watchlist" / right-click → "Add to Watchlist".

**What watchlist shows**:
- Protocol, session label, object label, last value, quality, timestamp.
- Refresh button polls all watchlist entries.
- Refresh selected polls just the selected entry.

**Persistence**:
- "Save…" button writes to `~/.config/protoskipper/watchlists/{profile}.json`.
- "Load…" button restores entries; matches against open sessions by
  protocol + address + object_id.
- On session open: auto-restores matching entries.

**Edge cases**:
- Session closed → watchlist entries show last known value with UNCERTAIN quality.
- Session reconnected → entries re-bind automatically via `session_opened` signal.

---

### Flow 12 — Audit Log

**Every operation is recorded**:
| Event | When |
|-------|------|
| session_open | Connection established |
| enumerate_objects | Object list populated |
| read | Every FC03/04/01/02 request+response |
| write_authorization | When operator initiates a write (stores intent) |
| write_committed | After successful transmission |
| write_denied | When operator cancels or profile rejects |
| replay_mode_blocked | If write attempted in replay mode |
| session_close | When session is torn down |

**Integrity**: Each row is HMAC-SHA256 chained to the previous row.
`Tools → View Audit Log → Verify` recomputes the chain and flags any tampering.

---

## 3. Current vs. Desired State Summary

| Gap | Root cause | Fix status |
|-----|-----------|------------|
| No way to add registers after connecting | `enumerate_objects()` returned a hardcoded sample; no UI to add/remove | ✅ Fixed: empty default + AddRegisterDialog |
| Object browser showed fake sample registers | `enumerate_objects()` in `_ModbusSession` yielded hardcoded rows | ✅ Fixed: returns empty iterator |
| No Remove button in Object Browser | Not implemented | ✅ Fixed |
| Empty object browser was blank with no guidance | No empty-state message | ✅ Fixed: placeholder label |
| RTU→TCP address bleed in dialog | `_on_protocol_changed` only pre-filled if field was empty | ✅ Fixed: separate widget sets |
| No reconnect after disconnect | Not implemented | ✅ Fixed |
| No way to open a second unit on the same gateway | Not implemented | ✅ Fixed |
| Monolithic address string (host:port/unit=N) | Single text field, no structure | ✅ Fixed: separate spinboxes |

---

## 4. Object ID & Address Reference (Modbus)

```
object_id  = table ":" address [":" count]
table      = "holding" | "input" | "coils" | "discrete"
address    = 0-based wire address (0–65535)
count      = number of 16-bit registers (1–120)
```

**5-digit Modbus notation** (PLC / HMI convention):

| Table | Range | 0-based addr | Formula |
|-------|-------|-------------|---------|
| Coils | 00001–09999 | 0–9998 | `coil_number = address + 1` |
| Discrete | 10001–19999 | 0–9998 | `discrete_number = address + 10001` |
| Input | 30001–39999 | 0–9998 | `input_number = address + 30001` |
| Holding | 40001–49999 | 0–9998 | `holding_number = address + 40001` |

**Function codes**:

| Table | Read FC | Write FC |
|-------|---------|---------|
| coils | FC01 | FC05 (single) / FC15 (multiple) |
| discrete | FC02 | — (read-only) |
| input | FC04 | — (read-only) |
| holding | FC03 | FC06 (single) / FC16 (multiple) |

---

## 5. Data Types Reference

| Type | Registers | Notes |
|------|-----------|-------|
| boolean | 1 | Coils/discrete only; 0 or 1 |
| uint16 | 1 | 0–65535 |
| int16 | 1 | −32768–32767 |
| uint32 | 2 | Requires byte_order + word_order |
| int32 | 2 | Requires byte_order + word_order |
| float32 | 2 | IEEE-754; most common for energy meters |
| uint64 | 4 | Requires byte_order + word_order |
| int64 | 4 | Requires byte_order + word_order |
| float64 | 4 | Requires byte_order + word_order |
| ascii | user-defined | 2 chars per register, null-padded |
| utf16 | user-defined | 1 char per register |

**Byte order (within each 16-bit register)**:
- `big` (default, standard Modbus): high byte first.
- `little`: low byte first (rare; some non-compliant devices).

**Word order (across registers, multi-register types only)**:
- `big` (default): most-significant word at lower address (ABCD, standard).
- `little`: least-significant word at lower address (CDAB).

Some device manuals describe this as "ABCD", "BADC", "CDAB", "DCBA" byte-swap
orders; the mapping is:

| Manual term | byte_order | word_order |
|-------------|-----------|------------|
| ABCD | big | big |
| BADC | little | big |
| CDAB | big | little |
| DCBA | little | little |
