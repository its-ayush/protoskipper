# Modbus RTU Hardware Test Plan

**Purpose:** Reproducible step-by-step script for verifying Modbus RTU
support against a physical USB-RS485 adapter and a known PLC or RTU.

---

## Equipment Required

| Item | Example |
|------|---------|
| Computer running ProtoSkipper | Linux / macOS / Windows |
| USB-RS485 adapter | FTDI FT232R, CH340, or Waveshare USB-RS485-B |
| Modbus RTU slave device | Schneider PM5560, Siemens PAC2200, or any Modbus RTU PLC |
| RS-485 cable | A/B twisted pair, correct polarity |

---

## Pre-Test Checks

1. Plug in the USB-RS485 adapter.
2. Confirm it appears as a serial device:
   - **Linux:** `ls /dev/ttyUSB*` or `ls /dev/ttyACM*`
   - **macOS:** `ls /dev/cu.usbserial*` or `ls /dev/cu.SLAB*`
   - **Windows:** Device Manager → Ports (COM & LPT) → note COMx
3. Verify baud rate, parity, and stop bits against the PLC manual.

---

## Supported Baud-Rate / Parity Combos (test each applicable one)

| Combo | Address format example |
|-------|------------------------|
| 9600 baud, no parity, 1 stop | `/dev/ttyUSB0@9600,N,1` |
| 19200 baud, even parity, 1 stop | `/dev/ttyUSB0@19200,E,1` |
| 38400 baud, odd parity, 1 stop | `/dev/ttyUSB0@38400,O,1` |
| 115200 baud, no parity, 1 stop | `/dev/ttyUSB0@115200,N,1` |

---

## Test Script

### TC-RTU-01: Connect to a Known Modbus RTU Slave

**Setup:** PLC at unit-ID 1, baud 9600, N, 1.

1. Launch `protoskipper-gui`.
2. Open **New Connection**.
3. Select **Modbus RTU** in the Protocol dropdown.
4. Confirm the serial-port combo lists your USB adapter.
5. Select the adapter from the dropdown (or type the path).
6. Set address to `/dev/ttyUSB0@9600,N,1/unit=1` (or equivalent).
7. Set profile to **LAB**; enter your name as operator.
8. Click **Connect**.

**Expected:** Session opens. Object browser shows the default holding/input/coil registers.

---

### TC-RTU-02: Import a Register Map and Read Values

**Prerequisite:** TC-RTU-01 passed; a CSV register map exists for your device.

1. Right-click the device in the device tree.
2. Choose **Import register map…** and select the CSV.
3. Object browser updates with the named registers.
4. Double-click a holding register; click **Refresh**.

**Expected:** Value displayed; quality = GOOD; raw bytes captured in Packet View.

---

### TC-RTU-03: Write a Holding Register (LAB Profile)

**Prerequisite:** TC-RTU-02 passed; a writable holding register identified.

1. Double-click the register; modify the value in the write dialog.
2. Click **Write**.
3. Click **Refresh** immediately after.

**Expected:**
- Write succeeds (no error dialog).
- Re-read value matches the value you wrote.
- Audit log records the write.

---

### TC-RTU-04: Write Denied in PRODUCTION Profile

1. Open a second connection to the same device, profile = **PRODUCTION**.
2. Attempt to write a register; type the tag name to confirm.
3. Cancel the confirm dialog.

**Expected:** Write does not proceed; audit log records "denied by operator".

---

### TC-RTU-05: Cable-Disconnect Mid-Session

1. During an active session, unplug the RS-485 cable.
2. Trigger a read (e.g. click Refresh).

**Expected:**
- Read returns quality = BAD with a descriptive error.
- GUI does not freeze or crash.
- Reconnecting the cable and clicking Refresh returns quality = GOOD.

---

### TC-RTU-06: Wrong Baud Rate

1. Connect to the device with the wrong baud rate (e.g. 19200 instead of 9600).
2. Attempt a read.

**Expected:** Read returns BAD quality; error message references timeout or CRC error. No crash.

---

### TC-RTU-07: Wrong Unit ID

1. Connect to the device with unit ID 99 (non-existent slave).
2. Attempt a read.

**Expected:** Read returns BAD quality with a Modbus exception or timeout. No crash.

---

## Failure Mode Reference

| Symptom | Likely Cause |
|---------|--------------|
| Port not listed in dropdown | Adapter not recognised by OS; check driver |
| `ConnectionFailure` on connect | Wrong port path or baud rate |
| All reads BAD (timeout) | Wrong baud/parity/unit-ID; cable polarity reversed |
| All reads BAD (exception 0x02) | Register address out of range |
| All reads BAD (CRC error) | Wrong parity; electrical noise; wrong baud rate |
| GUI freezes after disconnect | Bug — should not occur; file an issue with session log |

---

## Pass Criteria

All TCs **TC-RTU-01 through TC-RTU-07** must complete without crash or hang.
Quality transitions (GOOD ↔ BAD) must match the expected column above.
