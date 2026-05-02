# Manual Test: Tab Order Audit

**Scope:** Verify that Tab / Shift+Tab navigation follows a logical reading
order in every ProtoSkipper dialog. Screen-reader users and keyboard-only
operators rely on correct tab order.

**Tester:** _(name)_ &nbsp;**Date:** _(YYYY-MM-DD)_ &nbsp;**Build:** _(version)_

---

## How to run

1. Launch `protoskipper-gui`.
2. Open each dialog listed below.
3. Press **Tab** repeatedly from the first focusable widget and record the
   sequence. Compare against the *Expected order* column.
4. Repeat with **Shift+Tab** to verify the reverse sequence.
5. Mark each dialog **PASS** or **FAIL** and note any deviations.

---

## MT-01 — NewConnectionDialog

**Open via:** File → New Connection… (Ctrl+N)

| Step | Widget | Expected accessible name |
|------|--------|--------------------------|
| 1 | Protocol combo | "Protocol" |
| 2 | Address line edit | "Device address" |
| 3 | Label line edit | "Session label (optional friendly name)" |
| 4 | LAB radio button | (text of radio) |
| 5 | COMMISSIONING radio button | (text of radio) |
| 6 | PRODUCTION radio button | (text of radio) |
| 7 | Operator line edit | "Operator name or email" |
| 8 | Connect button (OK) | — |
| 9 | Cancel button | — |

**Result:** ☐ PASS &nbsp; ☐ FAIL &nbsp; Notes: ___

---

## MT-02 — PreferencesDialog

**Open via:** Tools → Preferences… (Ctrl+,)

| Step | Widget | Expected accessible name |
|------|--------|--------------------------|
| 1 | Default operator line edit | "Default operator email" |
| 2 | Audit log directory line edit | "Audit log directory path" |
| 3 | Browse… button | "Browse for audit log directory" |
| 4 | Default session profile combo | "Default session profile" |
| 5 | Theme combo | "Application colour theme" |
| 6 | Density combo | "Interface density" |
| 7 | OK button | — |
| 8 | Cancel button | — |

**Result:** ☐ PASS &nbsp; ☐ FAIL &nbsp; Notes: ___

---

## MT-03 — WriteDialog

**Open via:** Object Browser → select a writable register → Write…

| Step | Widget | Expected accessible name / label |
|------|--------|----------------------------------|
| 1 | Value line edit or spin box | (label from form row) |
| 2 | OK / Write button | — |
| 3 | Cancel button | — |

**Result:** ☐ PASS &nbsp; ☐ FAIL &nbsp; Notes: ___

---

## MT-04 — ProbeNetworkDialog

**Open via:** File → Probe Network… (Ctrl+P)

| Step | Widget | Expected accessible name / label |
|------|--------|----------------------------------|
| 1 | Protocol combo | — |
| 2 | Target range line edit | — |
| 3 | Probe / Scan button | — |
| 4 | Results table | — |
| 5 | Connect to selected button | — |
| 6 | Cancel button | — |

**Result:** ☐ PASS &nbsp; ☐ FAIL &nbsp; Notes: ___

---

## MT-05 — SafetyConfirmDialog (COMMISSIONING profile)

**Open via:** Attempt a write on a COMMISSIONING-profile session.

| Step | Widget | Expected accessible name / label |
|------|--------|----------------------------------|
| 1 | Confirm button | — |
| 2 | Cancel / Deny button | — |

**Result:** ☐ PASS &nbsp; ☐ FAIL &nbsp; Notes: ___

---

## MT-06 — SafetyConfirmDialog (PRODUCTION profile)

**Open via:** Attempt a write on a PRODUCTION-profile session.

| Step | Widget | Expected accessible name / label |
|------|--------|----------------------------------|
| 1 | Tag-name line edit | — |
| 2 | Confirm button | — |
| 3 | Cancel / Deny button | — |

**Result:** ☐ PASS &nbsp; ☐ FAIL &nbsp; Notes: ___

---

## MT-07 — ObjectBrowserPanel keyboard shortcuts

| Key | Expected action |
|-----|----------------|
| F5 | Read selected register |
| Shift+F5 | Read all registers |

Verify these work when:
- The Object Browser table has focus.
- A dock widget (Watchlist) has focus.
- The menu bar has focus.

**Result:** ☐ PASS &nbsp; ☐ FAIL &nbsp; Notes: ___

---

## Sign-off

| Tester | Date | Result |
|--------|------|--------|
| | | ☐ All PASS &nbsp; ☐ Issues found |
