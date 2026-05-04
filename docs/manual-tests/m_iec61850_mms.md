# Manual Test — IEC 61850 MMS Session

**Scope:** IEC 61850-8-1 MMS client functionality
**Prerequisite:** A live IED or IED simulator reachable on TCP port 102
**Protocol plugin:** `protoskipper-iec61850`

---

## Environment checklist

- [ ] ProtoSkipper installed with IEC 61850 plugin (`pip install "protoskipper[iec61850]"`)
- [ ] IED IP address known and reachable (ping test passes)
- [ ] TCP port 102 not blocked by firewall
- [ ] SCD file available for the IED under test (optional but recommended)

---

## MT-MMS-01 — Connect and browse server directory

**Objective:** Verify that ProtoSkipper can open an MMS association and
enumerate logical devices.

**Steps:**

1. Launch ProtoSkipper GUI (`protoskipper-gui`).
2. Choose **File → New Connection** and select *IEC 61850 MMS* from the
   protocol dropdown.
3. Enter the IED IP address and leave port at `102`.
4. Click **Connect**.

**Expected result:**

- The status bar shows `Connected` within the configured timeout.
- The Object Browser panel populates with one or more *Logical Device* nodes
  (e.g. `IED1LD0`, `PROT`).
- No error dialog appears.

---

## MT-MMS-02 — Read a data attribute

**Objective:** Verify that reading a data attribute returns a plausible value.

**Steps:**

1. Expand a logical device node in the Object Browser.
2. Expand the logical node `LLN0` or any `MMXU*` node.
3. Navigate to a leaf attribute (e.g. `TotW.mag.f`) with FC = `MX`.
4. Right-click and choose **Read**.

**Expected result:**

- A value is displayed (float, bool, or struct).
- The Packet Log records the MMS `Read` request and response.
- No `MmsReadError` exception appears.

---

## MT-MMS-03 — Watch-list live updates

**Objective:** Verify that the watch-list panel shows live values.

**Steps:**

1. Right-click a leaf attribute in the Object Browser and choose
   **Add to Watch-list**.
2. Repeat for two more attributes.
3. Set the poll interval to 1 s (Watch-list toolbar → Interval).
4. Observe for 10 seconds.

**Expected result:**

- Values update at approximately 1-second intervals.
- No "read error" badges appear.
- CPU usage is reasonable (< 5 % on the host).

---

## MT-MMS-04 — Write a configuration parameter

**Objective:** Verify that write-protected sessions reject writes and that
an authorised write succeeds.

**Steps (write rejection):**

1. With a default (read-only) connection, right-click a `CF`-class attribute.
2. Choose **Write** and enter a value.
3. Click **OK** in the write dialog.

**Expected result:**

- A `PermissionError` dialog appears stating writes are disabled.
- No MMS `Write` PDU is sent (verify in Packet Log).

**Steps (authorised write — GUI):**

1. Open **Edit → Preferences → IEC 61850** and enable *Allow writes in session*.
2. Reconnect (the connection badge turns orange to indicate write-capable mode).
3. Repeat the write attempt on the same attribute.
4. The safety confirmation dialog appears.
5. Click **Approve**.

**Expected result:**

- The MMS `Write` request appears in the Packet Log.
- The MMS `Write` response is `success`.
- The attribute value shown in the Object Browser updates.

---

## MT-MMS-05 — Report control block (RCB) subscription

**Objective:** Verify that enabling an unbuffered RCB delivers reports.

**Steps:**

1. In the Object Browser, locate an `RP` (reporting) node under `LLN0`.
2. Right-click an `URCB` dataset reference and choose **Enable Reporting**.
3. Trigger a state change on the IED (if possible).
4. Observe the Report panel.

**Expected result:**

- At least one report entry arrives within the steady-state interval.
- Each entry shows `reportId`, `dataSet`, changed values, and an ISO 8601
  timestamp.

---

## MT-MMS-06 — Disconnect cleanly

**Objective:** Verify that closing the session sends a MMS `Conclude` request.

**Steps:**

1. With a live MMS session, click **Disconnect** in the toolbar.

**Expected result:**

- The Packet Log shows a `Conclude` request followed by a `Conclude` response.
- The status bar shows `Disconnected`.
- No lingering TCP connection (verify with `ss -tnp | grep 102` on the host).

---

## MT-MMS-07 — Scripting: browse and read

**Objective:** Verify the scripting namespace works end-to-end.

**Steps:**

1. Save the following as `mms_browse.py`:

   ```python
   host = argv[0] if argv else "192.168.1.100"
   with iec61850.Session(host) as s:
       lds = s.get_server_directory()
       log.info("Logical devices: %s", lds)
       for ld in lds[:2]:
           lns = s.get_logical_device_directory(ld)
           log.info("  LNs in %s: %s", ld, lns)
   ```

2. Run: `protoskipper run mms_browse.py <IED_IP>`

**Expected result:**

- Terminal shows logical device and logical node names without exceptions.
- Exit code is 0.

---

## MT-MMS-08 — Conformance: mms_ed21 profile

**Objective:** Run the built-in MMS conformance profile against the IED.

**Steps:**

1. Save the following as `conformance_mms.py`:

   ```python
   from protoskipper_iec61850.conformance.schema import load_builtin_profile
   from protoskipper_iec61850.conformance.runner import ConformanceRunner
   from protoskipper_iec61850.conformance.reporter import write_html_report
   from protoskipper_iec61850._mms_client import MmsClient

   host = argv[0]
   client = MmsClient(host=host, port=102)
   client.connect()
   profile = load_builtin_profile("mms_ed21")
   runner = ConformanceRunner(
       profile,
       session_ctx={"mms_client": client},
       operator="Tester",
   )
   report = runner.run()
   write_html_report(report, "mms_conformance.html")
   print(f"Result: {report.passed}/{report.total} passed")
   client.close()
   ```

2. Run: `protoskipper run conformance_mms.py <IED_IP> --allow-writes`

**Expected result:**

- `mms_conformance.html` is created.
- All MANDATORY tests pass (status `PASS`).
- Exit code is 0.
