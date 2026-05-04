# Manual Test — IEC 61850 GOOSE

**Scope:** GOOSE subscriber and publisher functionality
**Prerequisite:** An IED or IED simulator publishing GOOSE on the local LAN
**Protocol plugin:** `protoskipper-iec61850`

---

## Environment checklist

- [ ] ProtoSkipper installed with IEC 61850 plugin
- [ ] A network interface on the test host has Layer-2 access to the IED's
  GOOSE multicast domain (no routers between host and IED)
- [ ] SCD file for the IED under test (optional but recommended)
- [ ] Raw socket permissions on the test interface
  (Linux: `sudo setcap cap_net_raw+ep $(which protoskipper-gui)`)

---

## MT-GOOSE-01 — Subscribe and receive frames

**Objective:** Verify that ProtoSkipper receives and decodes live GOOSE frames.

**Steps:**

1. Open ProtoSkipper GUI.
2. Open **File → New Connection → IEC 61850 (GOOSE)**.
3. Select the network interface (e.g. `eth0`).
4. Optionally load the SCD file to pre-populate control block references.
5. Click **Start Listening**.

**Expected result:**

- The GOOSE panel populates with rows as frames arrive.
- Each row shows: `goCBRef`, `datSet`, `stNum`, `sqNum`, allData values, and
  last-received timestamp.
- The `stNum` counter increments only when state changes occur.
- The `sqNum` counter increments on every retransmission.

---

## MT-GOOSE-02 — State-change detection

**Objective:** Verify that ProtoSkipper correctly identifies state changes vs.
retransmissions.

**Steps:**

1. With the GOOSE panel open and receiving, trigger a state change on the IED
   (e.g. open/close a circuit breaker).

**Expected result:**

- The row for the affected control block flashes briefly.
- `stNum` increments by exactly 1.
- `sqNum` resets to 0, then increments as the burst completes.
- The *allData* value column shows the new value.

---

## MT-GOOSE-03 — Staleness health indicator

**Objective:** Verify that the health indicator turns amber/red when frames stop.

**Steps:**

1. Start receiving GOOSE frames (MT-GOOSE-01).
2. Note the `timeAllowedToLive` value from any frame in the Packet Log.
3. Disconnect the IED from the network (or stop the simulator).
4. Wait for 1.5 × `timeAllowedToLive` ms.

**Expected result:**

- The GOOSE panel row turns **amber** after the first missed interval.
- After a further timeout it turns **red**.
- The Substation Overview tile for that IED also shows red.

---

## MT-GOOSE-04 — Publish a GOOSE frame (scripting)

**Objective:** Verify that the publisher can send a GOOSE frame and that a
separate subscriber receives it.

**Steps:**

1. Save the following as `goose_pub.py`:

   ```python
   import time
   iface = argv[0] if argv else "eth0"
   pub = iec61850.Goose.Publisher(interface=iface)
   pub.start(
       go_cb_ref="ProtoSkipperTest/LLN0$GO$gcbTest",
       data_set_ref="ProtoSkipperTest/LLN0$TestDS",
       app_id=0x4000,
   )
   pub.publish([True, 42])
   time.sleep(2)
   pub.publish([False, 43])
   pub.stop()
   ```

2. On a second host (or in a second terminal), run the GOOSE subscriber test
   script or open the GUI GOOSE panel.

3. Run: `protoskipper run goose_pub.py eth0 --allow-writes`

**Expected result:**

- Two GOOSE messages arrive at the subscriber: `[True, 42]` then `[False, 43]`.
- `stNum` increments from 1 to 2 between the two publishes.

---

## MT-GOOSE-05 — PCAP dissection of GOOSE frames

**Objective:** Verify that the PCAP dissector correctly parses a GOOSE capture.

**Steps:**

1. Capture GOOSE traffic for 30 seconds:
   `sudo tcpdump -i eth0 ether proto 0x88b8 -w goose_test.pcap`

2. Open ProtoSkipper GUI → **File → Open PCAP**.

3. In the filter bar, type: `goose.stnum > 0`

4. Press **Enter** to apply the filter.

**Expected result:**

- Only frames with `stNum > 0` are shown.
- Each row shows decoded `goCBRef`, `goID`, `stNum`, `sqNum`, `t` (ms), and
  `allData` values.
- The frame count matches `tshark -r goose_test.pcap -Y 'goose.stNum > 0'`.

---

## MT-GOOSE-06 — confRev mismatch detection

**Objective:** Verify that ProtoSkipper raises an alert when `confRev`
changes unexpectedly.

**Steps:**

1. Subscribe to a GOOSE control block.
2. Manually edit the SCD file to change the `confRev` attribute.
3. Reload the SCD (**File → Reload SCD**).

**Expected result:**

- A warning badge appears on the affected GOOSE row.
- The tooltip reads "confRev mismatch: received X, expected Y".

---

## MT-GOOSE-07 — Conformance: goose_pub_ed21 profile

**Objective:** Run the built-in GOOSE publisher conformance profile.

**Steps:**

1. Save the following as `conformance_goose.py`:

   ```python
   from protoskipper_iec61850.conformance.schema import load_builtin_profile
   from protoskipper_iec61850.conformance.runner import ConformanceRunner
   from protoskipper_iec61850.conformance.reporter import write_html_report

   profile = load_builtin_profile("goose_pub_ed21")
   runner = ConformanceRunner(profile, operator="Tester")
   report = runner.run()
   write_html_report(report, "goose_conformance.html")
   print(f"Result: {report.passed}/{report.total} passed")
   ```

2. Run: `protoskipper run conformance_goose.py`

**Expected result:**

- `goose_conformance.html` is created.
- MANDATORY tests report `PASS` (CONDITIONAL and OPTIONAL may be skipped).
- Exit code is 0.
