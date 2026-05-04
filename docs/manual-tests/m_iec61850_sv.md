# Manual Test — IEC 61850 Sampled Values (SV)

**Scope:** Sampled Values decoder, scope panel, and COMTRADE export
**Prerequisite:** A merging unit or IED simulator publishing SV on the local LAN
**Protocol plugin:** `protoskipper-iec61850`

---

## Environment checklist

- [ ] ProtoSkipper installed with IEC 61850 plugin
- [ ] Network interface has Layer-2 access to the merging unit's SV domain
- [ ] Raw socket permissions on the test interface
- [ ] SCD file with `SampledValueControl` entries (optional but recommended)

---

## MT-SV-01 — Subscribe and display waveforms

**Objective:** Verify that ProtoSkipper receives SV frames and renders live
waveforms.

**Steps:**

1. Open ProtoSkipper GUI.
2. Open **File → New Connection → IEC 61850 (SV)**.
3. Select the network interface.
4. Optionally load the SCD file to pre-populate `svID` and channel descriptions.
5. Click **Start**.

**Expected result:**

- The SV Scope panel shows one waveform lane per channel.
- Channel names are populated from the SCD file (if loaded), otherwise shown
  as `CH_0`, `CH_1`, etc.
- The waveform scrolls in real time at the merging unit's sample rate
  (typically 80 or 256 samples/cycle).

---

## MT-SV-02 — Verify sample rate and `smpCnt` continuity

**Objective:** Verify that sample counter (`smpCnt`) increments without gaps.

**Steps:**

1. Start SV subscription (MT-SV-01).
2. Open the Packet Log panel.
3. Filter by `sv.svid = "<your_sv_id>"`.
4. Observe 5 seconds of frames.

**Expected result:**

- `smpCnt` increments by 1 between consecutive ASDUs (or wraps at the
  configured `smpRate`).
- No gap events are logged in the status bar.
- Frame rate matches the published sample rate ÷ `noASDU`.

---

## MT-SV-03 — Freeze and inspect a waveform cycle

**Objective:** Verify the freeze function and single-cycle zoom.

**Steps:**

1. With live waveforms running, click **Freeze** in the SV Scope toolbar.
2. Use the mouse scroll wheel to zoom to a single 50/60 Hz cycle.
3. Hover over the waveform peak to read the peak value.

**Expected result:**

- Waveform stops updating when frozen.
- Single-cycle zoom is smooth (no lag).
- Peak value tooltip matches the expected fundamental amplitude for the
  signal type (e.g. ≈ 325 V peak for 230 V RMS voltage).

---

## MT-SV-04 — COMTRADE export

**Objective:** Verify that a COMTRADE recording is written correctly.

**Steps:**

1. Start SV subscription.
2. Click **Record** in the SV Scope toolbar.
3. After 10 seconds, click **Stop Recording**.
4. Choose a save path (e.g. `sv_recording`).

**Expected result:**

- Files `sv_recording.cfg` and `sv_recording.dat` (binary) are created.
- Open with a COMTRADE viewer (e.g. `python -m protoskipper_iec61850.sv.comtrade_reader sv_recording.cfg`).
- Number of samples = sample_rate × 10 seconds (± 1 %).
- Channel names match the SCD file (if loaded).

---

## MT-SV-05 — COMTRADE reader script

**Objective:** Verify the scripting API for reading a COMTRADE recording.

**Steps:**

1. Save the following as `sv_read.py`:

   ```python
   from protoskipper_iec61850.sv.comtrade_reader import ComtradeReader

   path = argv[0]
   reader = ComtradeReader(path)
   header = reader.header()
   log.info("Channels: %d, Samples: %d, Rate: %s Hz",
             len(header.channels), header.sample_count, header.sample_rate)
   ch0 = reader.channel(0)
   log.info("Ch0 name=%s, first_value=%s", ch0.name, ch0.samples[0])
   ```

2. Run: `protoskipper run sv_read.py sv_recording.cfg`

**Expected result:**

- Correct channel count, sample count, and sample rate are logged.
- First sample of channel 0 is a numeric value (float).
- Exit code is 0.

---

## MT-SV-06 — Raw SV frame decode (scripting)

**Objective:** Verify the dissector decode path via the scripting API.

**Steps:**

1. Capture SV traffic: `sudo tcpdump -i eth0 ether proto 0x88ba -w sv_test.pcap`

2. Save the following as `sv_dissect.py`:

   ```python
   from protoskipper_iec61850.dissect.pcap import PcapReader

   path = argv[0]
   count = 0
   with PcapReader(path) as r:
       for rec in r:
           sv = iec61850.Sv.decode_frame(rec.data)
           if sv:
               count += 1
               if count <= 3:
                   log.info("svId=%s noASDU=%d", sv.asdus[0].sv_id if sv.asdus else "?", sv.no_asdu)
   log.info("Total SV frames: %d", count)
   ```

3. Run: `protoskipper run sv_dissect.py sv_test.pcap`

**Expected result:**

- First 3 frames are logged with `svId` and `noASDU`.
- Total frame count is > 0.

---

## MT-SV-07 — SV gap alarm

**Objective:** Verify that the GUI raises an alarm when SV frames stop.

**Steps:**

1. Start SV subscription (MT-SV-01).
2. Stop the merging unit or simulator.
3. Wait 500 ms.

**Expected result:**

- The SV Scope panel shows a red "SV GAP" overlay.
- The status bar shows "SV loss: <svID>".
- The Substation Overview tile for the associated IED turns **red**.
