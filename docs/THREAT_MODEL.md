# ProtoSkipper — Threat Model

**Version:** 0.1.0
**Date:** 2026-05-04
**Authors:** DataSailors Pvt Ltd
**Scope:** Desktop application + CLI; does not cover hosted SaaS or cloud deployments.

---

## 1. System Overview

ProtoSkipper is an open-source OT/SCADA protocol testing and commissioning
toolkit.  Operators run it as a desktop application (`protoskipper-gui`) or
from the CLI (`protoskipper`) on a trusted workstation.  The application
connects to industrial devices (PLCs, IEDs, RTUs, BACnet BACDs) over a
LAN, serial bus, or VPN (e.g. Tailscale subnet routing).

### 1.1 Trust Boundaries

```
┌───────────────────────────────────────────────────────────┐
│  Operator workstation (trusted)                           │
│   ├─ protoskipper-gui / protoskipper CLI                  │
│   ├─ Plugin packages installed by the operator            │
│   └─ Audit log (SQLite, ~/.local/share/protoskipper/)     │
│                                                           │
│  ← trust boundary ──────────────────────────────────────  │
│                                                           │
│  Routed OT network (untrusted)                            │
│   ├─ Modbus TCP/RTU devices                               │
│   ├─ IEC 60870-5-104 RTUs                                 │
│   ├─ IEC 61850 IEDs                                       │
│   └─ BACnet/IP BACDs                                      │
└───────────────────────────────────────────────────────────┘
```

ProtoSkipper **initiates all connections** — it does not listen on any
network port and does not run a server.  The only inbound data is
protocol responses from devices that the operator has explicitly targeted.

### 1.2 Assets

| Asset | Confidentiality | Integrity | Availability |
|-------|----------------|-----------|--------------|
| Audit log (HMAC-chained SQLite) | Medium | **Critical** | High |
| Session credentials (operator name, profile) | Low | Medium | Low |
| Plugin packages (Python wheels) | Low | **High** | Medium |
| Captured packet data | Medium | Medium | Low |
| Device point-list / SCD files supplied by operator | Medium | High | Low |

---

## 2. Threat Actors

| Actor | Motivation | Capability |
|-------|-----------|-----------|
| **OT device under test** | Malformed responses, fuzzing back at the client | Low–medium: constrained protocol responses |
| **Malicious plugin author** | Code execution on the operator's workstation | High: arbitrary Python in the plugin entry point |
| **Network eavesdropper** | Read cleartext OT traffic | Medium: passive sniffing on the same segment |
| **Rogue OT device (on-path)** | Inject spoofed protocol responses | Medium: requires network access to the OT segment |
| **Physical attacker** | Steal audit log, extract session data from disk | Low (physical access assumed out of scope for workstation) |

---

## 3. Attack Surface

### 3.1 Network-facing (active when a session is open)

| Surface | Protocol | Direction | Exposure |
|---------|----------|-----------|---------|
| Modbus TCP client | TCP 502 | Outbound only | Responses parsed by pymodbus |
| IEC 104 master | TCP 2404 | Outbound only | Responses parsed by custom APCI/ASDU parser |
| IEC 61850 MMS client | TCP 102 | Outbound only | Responses parsed by pyiec61850 (C library) |
| BACnet/IP client | UDP 47808 | Send + Receive | Who-Is / I-Am / ReadProperty responses via bacpypes3 |

All network access is **opt-in** — the operator explicitly opens a session
or triggers a discovery scan.  No persistent listeners.

### 3.2 File-system

| Surface | Sensitivity |
|---------|------------|
| Point-list CSV / EDE / JSON parsed on load | Medium (user-supplied file; parsing must not crash or exec) |
| SCD / SCL files parsed by SCL parser | Medium |
| PCAP / pcapng files opened in offline mode | Medium |
| Audit log SQLite read by `verify_log` | Low (integrity-checked; read-only by verify) |
| Plugin packages installed from PyPI or local wheel | **High** — see §4.4 |

### 3.3 IPC / inter-process

None.  ProtoSkipper does not expose a D-Bus service, HTTP API, RPC server,
or named pipe.

---

## 4. Threat Catalogue and Mitigations

### 4.1 THREAT-01: Malformed OT device response causes memory safety violation

**STRIDE:** Tampering, Denial of Service
**Likelihood:** Medium (constrained attacker with network access)
**Impact:** Medium (crash of the GUI; no privilege escalation expected on CPython)

**Mitigations:**
- All protocol parsers wrap device input in `try/except` and emit a
  `DriverError`; the GUI records an error row rather than crashing.
- `pymodbus`, `bacpypes3` and `pyiec61850` are maintained third-party
  libraries with their own fuzz/test suites.
- The IEC 104 APCI/ASDU parser in `builtin_drivers/iec104/` is covered
  by Hypothesis property-based fuzz tests (P9.A.3) that exercise the full
  boundary space of field values.
- `protoskipper.core.capture` stores raw bytes; replay is read-only.

**Residual risk:** Low.  A crafted C-level segfault in `pyiec61850` (native
library) could crash the process; mitigated by the OS-level process boundary.

---

### 4.2 THREAT-02: Audit log tampering

**STRIDE:** Tampering, Repudiation
**Likelihood:** Low (requires local write access to `~/.local/share/protoskipper/`)
**Impact:** High (regulatory / compliance contexts)

**Mitigations:**
- Every audit row is HMAC-SHA256 chained: `row_hash = HMAC(prev_hash ‖ row_json)`.
- The chain root is seeded with a per-session secret stored only in memory.
- `verify_log()` in `core/audit.py` checks the full chain; any gap or
  modified row raises `AuditVerificationError`.
- Rows are append-only in WAL mode (`PRAGMA journal_mode=WAL`).

**Residual risk:** An attacker with local write access *and* the in-memory
secret could forge a valid chain.  Acceptable for the workstation threat model
(physical access is out of scope).

---

### 4.3 THREAT-03: Write to live device without operator confirmation

**STRIDE:** Tampering
**Likelihood:** Low (requires a bug in the write-flow or a malicious plugin)
**Impact:** **Critical** — unintended write to a live PLC/IED can cause equipment damage

**Mitigations (defence-in-depth, 3 layers):**
1. **SessionProfile enum** — operator selects `LAB`, `COMMISSIONING`, or
   `PRODUCTION` at connect time.  `PRODUCTION` sessions require explicit
   write authorization for every write; `LAB` sessions do not.
2. **SafetyContext.require_write_authorization()** — all writes pass through
   here; any `PRODUCTION` write raises `AuthorizationDenied` if not confirmed.
3. **GuiConfirmHandler** — the worker thread blocks on a `threading.Event`
   until the operator confirms in `SafetyConfirmDialog`.  Timeout (300 s)
   defaults to **deny**.
4. **AuditLog** — every write attempt (confirmed or denied) is recorded.
5. **Read/Write split** — `prepare_write()` builds the `WriteIntent` with
   no I/O; `commit_write()` transmits.  They are never fused; the
   confirmation dialog sits between them.

**Residual risk:** A malicious plugin can bypass SafetyContext by calling
the device's protocol library directly.  See §4.4.

---

### 4.4 THREAT-04: Malicious or compromised plugin package

**STRIDE:** Elevation of Privilege, Tampering
**Likelihood:** Medium (supply-chain risk for community plugins)
**Impact:** **Critical** — arbitrary code execution on the operator's workstation

**Mitigations:**
- Plugins are Python packages installed via `pip`; there is **no sandbox**.
  This is intentional: sandboxing native-library protocol stacks is
  impractical without a separate process.
- Built-in drivers ship in the `protoskipper` package itself (same trust level).
- The plugin discovery loop (`core/plugin_loader.py`) catches all exceptions
  during import; a bad plugin is logged and skipped.
- **Operator guidance:** install plugins only from trusted sources; treat
  `pip install` of a community plugin as equivalent to running arbitrary code.
- Future (post-1.0): optional signature verification of plugin wheels.

**Residual risk:** High by design.  Documented in `PLUGINS.md` and this file.

---

### 4.5 THREAT-05: Network eavesdropping on cleartext OT traffic

**STRIDE:** Information Disclosure
**Likelihood:** Medium (shared OT segment or VLAN)
**Impact:** Low–medium (OT telemetry is often non-confidential; depends on asset)

**Mitigations:**
- ProtoSkipper does not add encryption — it faithfully implements the
  underlying protocol (Modbus TCP, IEC 104, etc.) which are inherently
  cleartext in their base specifications.
- Operators using Tailscale or IPsec VPN for remote access get transport
  encryption at the VPN layer.
- Captured traffic stored on disk is only accessible to the local user.

**Residual risk:** Inherent to cleartext industrial protocols.  Out of scope
for ProtoSkipper to solve; document this clearly to operators.

---

### 4.6 THREAT-06: Malicious SCD / point-list file

**STRIDE:** Tampering, Denial of Service
**Likelihood:** Low (requires attacker to supply a file to the operator)
**Impact:** Low (crash of the parser; no privilege escalation on CPython)

**Mitigations:**
- SCL/SCD parser (`protoskipper_iec61850.scl.parser`) uses Python's
  `xml.etree.ElementTree` with `defusedxml` wrapper to prevent XXE
  (XML External Entity injection).
- Point-list CSV parser uses the stdlib `csv` module; no formula injection
  risk because values are parsed as typed data, not executed.
- All parsers raise `EncodingError` on malformed input; errors surface in
  the GUI without crashing.

---

### 4.7 THREAT-07: CLI injection via device-supplied strings

**STRIDE:** Tampering
**Likelihood:** Low
**Impact:** Medium (log pollution, unexpected labels in the GUI)

**Mitigations:**
- Device-supplied strings (object names, vendor strings) are treated as
  untrusted data: they are HTML-escaped before display in Qt widgets and
  sanitised before being written to the audit log.
- No `eval()`, `exec()`, or `subprocess` calls are made with device data.

---

## 5. Audit Log Guarantee Summary

```
┌──────────────────────────────────────────────────────┐
│ Guarantee: every write attempt is recorded BEFORE    │
│ the write is committed to the wire.                  │
│                                                      │
│ Chain: row[n].hash = HMAC_SHA256(row[n-1].hash,      │
│                                  row[n].json_body)   │
│                                                      │
│ Verification: verify_log() replays the chain; any    │
│ gap, deletion, or modification → AuditVerificationError │
└──────────────────────────────────────────────────────┘
```

---

## 6. Plugin Sandbox Limitations

ProtoSkipper **does not sandbox plugins**.  A plugin that is imported runs
with the same OS privileges as the operator's workstation user.  This is
the same trust model as any `pip install`.

Operators who need stronger isolation should:
1. Run ProtoSkipper in a VM or container.
2. Verify plugin package signatures (future feature).
3. Audit plugin source code before installing.

---

## 7. Known Unmitigated Risks

| Risk | Severity | Owner |
|------|---------|-------|
| Native code crash in `pyiec61850` (C library) | Medium | Upstream `libiec61850` |
| Plugin supply-chain compromise | High | Operator due diligence |
| Cleartext OT protocols | Inherent | Protocol standards bodies |
| Audit log secret lost on process exit | Low | By design (in-memory) |

---

## 8. Security Contact

See [SECURITY.md](../SECURITY.md) for the vulnerability disclosure policy.
