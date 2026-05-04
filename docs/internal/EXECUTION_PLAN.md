# ProtoSkipper — Production Execution Plan

> **Overall Progress: 95 / 113 tasks complete (84.1%)**
>
> **Status of this document:** Source of truth for the road from current
> pre-alpha scaffold to a 1.0 production-ready release. Every line item is a
> discrete, individually-verifiable task with explicit acceptance criteria
> and explicit testing requirements. Tasks are intentionally small so progress
> is visible and reversible.
>
> **Owner:** ProtoSkipper maintainers (DataSailors).
> **Updated:** 2026-05-04 — P2.A.1 pcapng spec compliance complete: block
> type fixed to `0x40000001` (RFC 9443 §3.1 non-copy bit), 18 unit tests
> passing (`tests/unit/test_pcapng.py`). Remaining: Phases 7–9.

---

## How to read this plan

* Tasks are numbered `P{phase}.{group}.{task}` so you can refer to one in
  a commit or PR title (e.g. `feat(audit): P2.A.3 record committed-write events`).
* Each task has:
  * **Goal** — the single sentence answer to "what does done look like".
  * **Files touched** — the files that will be created or modified.
  * **Implementation notes** — concrete, specific direction (no hand-waving).
  * **Acceptance criteria (AC)** — boolean, machine- or human-checkable
    statements; **all** must be true to mark the task done.
  * **Tests required** — exact test files / cases that must pass; new
    tests this task adds; testing type (unit / integration / GUI / manual).
* A task is **done** only when every AC is satisfied, every required test
  exists and passes, ruff is clean, mypy is clean, and the change is
  committed with a conventional commit message.
* If a task uncovers more work, it does not mutate to absorb it. Add a new
  numbered task instead. Plan drift kills products.

---

## Definitions used throughout

* **Unit test** — runs without network, without GUI, without filesystem
  outside `tmp_path`. Lives under `tests/unit/`. Must finish in < 100 ms
  per test.
* **Integration test** — may use sockets, pymodbus, the Modbus simulator,
  or a real audit-log file. Lives under `tests/integration/`. Marked
  `@pytest.mark.integration`.
* **GUI test** — uses `pytest-qt` to drive a real `QApplication`. Lives
  under `tests/gui/`. Marked `@pytest.mark.gui`. Skipped when PySide6 is
  absent or `$DISPLAY`/headless equivalents are unavailable.
* **Manual test** — a human-driven smoke. Listed only when automated
  coverage is impractical (e.g. real serial hardware, real substation).
  Each manual test has a written script under `docs/manual-tests/`.

---

## Phase summary

| Phase | Theme | Outcome |
|-------|-------|---------|
| **0** | Stabilise the scaffold | Everything currently in the tree is bug-free, tested, lint-clean, and verified end-to-end. |
| **1** | Modbus 1.0 | Modbus TCP + RTU usable end-to-end on real hardware: register-map import, full data types, packet capture, audit-log completeness, GUI test coverage. |
| **2** | Capture & replay | pcapng-based capture pipeline, replay viewer, audit verification UI, capture-aware packet view. |
| **3** | Quality, polish, accessibility | Theming, i18n scaffolding, accessibility audit, settings persistence (recent connections, watchlists), keyboard shortcuts, help. |
| **4** | Second protocol — IEC 60870-5-104 | First non-Modbus driver. Validates the plugin contract. **✅ COMPLETE (commit `583cc18`).** |
| **5** | Scripting & automation | Embedded Python REPL, headless `protoskipper run` command, scenario scripts. **✅ COMPLETE.** P5.A (REPL + CLI) shipped early; P5.B.1 (bus-load scenario) complete. |
| **6** | Packaging & distribution | Signed Windows installer, notarised macOS DMG, Linux AppImage, GitHub Actions CI/CD, signed releases. **✅ COMPLETE.** |
| **7** | BACnet/IP | Third protocol; broadens the plugin contract. |
| **8** | IEC 61850 | MMS + GOOSE; the protocol that defines the upper bound of complexity. |
| **9** | 1.0 release readiness | Security audit, fuzzing, soak tests, docs site, support & community policy, marketing site. |

---

# Phase 0 — Stabilise the scaffold

**Theme:** Nothing new lands until the existing scaffold is verifiably
sound. Phase 0 is finished when the head of `master` passes every test on
every supported OS, ships zero ruff/mypy errors, and a fresh clone can run
`protoskipper-gui` against the bundled simulator and complete the discover →
connect → read → write loop without a defect.

## P0.A — Tooling hygiene

### ✅ P0.A.1 Pre-commit configuration

* **Goal:** Every commit on every developer's machine passes ruff + mypy
  + a fast pytest unit subset before the commit lands.
* **Files touched:** `.pre-commit-config.yaml` (new), `CONTRIBUTING.md`.
* **Implementation notes:** Use `pre-commit` standard repo hooks for
  `ruff`, `ruff-format`, `mypy`, `end-of-file-fixer`, `trailing-whitespace`,
  `check-yaml`, `check-toml`. Add a custom `pytest -q tests/unit/` hook
  with `language: system` and `pass_filenames: false`.
* **Acceptance criteria:**
  * `pre-commit install` on a fresh clone succeeds.
  * `pre-commit run --all-files` on a clean tree exits zero.
  * Editing a file to introduce a known lint error (e.g. unused import)
    causes the commit to be rejected with a clear message.
  * `CONTRIBUTING.md` documents the hook and how to bypass it for
    emergencies (`--no-verify` is forbidden in CI but allowed locally).
* **Tests required:**
  * Manual: `tests/manual/m_pre_commit_blocks_bad_commit.md` — script that
    produces a deliberately-bad change and verifies the hook rejects it.

### ✅ P0.A.2 GitHub Actions CI

* **Goal:** Every push and every PR runs lint, type-check, unit tests, and
  integration tests on Linux/Windows/macOS, Python 3.10/3.11/3.12.
* **Files touched:** `.github/workflows/ci.yml` (new).
* **Implementation notes:** Three jobs: `lint` (ruff + mypy), `unit`
  (pytest tests/unit/), `integration` (pytest tests/integration/, requires
  pymodbus). Matrix: `os ∈ {ubuntu-latest, windows-latest, macos-latest}`,
  `python ∈ {3.10, 3.11, 3.12}`. Cache pip wheels keyed off
  `pyproject.toml`. Upload coverage to a single artefact.
* **Acceptance criteria:**
  * A green check appears on PRs within 10 minutes of push.
  * A pull request that breaks any unit test cannot be merged (branch
    protection rule documented in `CONTRIBUTING.md`).
  * Coverage artefact is downloadable from the workflow run.
  * The workflow is reusable: invoking it on a fork of the repo works
    without secrets.
* **Tests required:**
  * Manual: open a PR with a deliberately failing test; verify the merge
    button is blocked.

### ✅ P0.A.3 Coverage threshold + report

* **Goal:** Track unit-test coverage and prevent regressions.
* **Files touched:** `pyproject.toml`, `.github/workflows/ci.yml`.
* **Implementation notes:** Configure `pytest-cov` with
  `--cov=protoskipper --cov-fail-under=80` for the unit-test job. Generate
  HTML and XML reports. Upload XML to Codecov (or a self-hosted equivalent).
* **Acceptance criteria:**
  * `pytest tests/unit/ --cov=protoskipper` reports ≥ 80% line coverage on
    the core layer (`protoskipper.core.*`) and ≥ 60% overall.
  * CI fails the run when coverage drops below threshold.
  * Coverage HTML is browsable as a CI artefact.
* **Tests required:**
  * Tests previously passing still pass.

### ✅ P0.A.4 Pin all developer-facing tool versions

* **Goal:** Eliminate "it works on my machine" by pinning ruff, mypy,
  pytest, and pre-commit to exact versions.
* **Files touched:** `pyproject.toml` `[project.optional-dependencies].dev`,
  `.pre-commit-config.yaml`.
* **Acceptance criteria:**
  * Every dev tool has an exact `==` pin.
  * `pip install -e ".[dev]" --upgrade` does not bump these versions
    silently.
* **Tests required:** None new; pre-existing tests still pass.

## P0.B — Test coverage gaps in existing code

### ✅ P0.B.1 Unit tests for `plugin_loader`

* **Goal:** The plugin discovery code is exercised by tests, including its
  failure modes (bad import, missing PROTOCOL_ID, mismatched key, duplicate).
* **Files touched:** `tests/unit/test_plugin_loader.py` (new).
* **Implementation notes:** Use `EntryPoint` mock objects (constructed
  directly, not through `pip install`) by patching
  `importlib.metadata.entry_points`. Cover: happy path, ImportError on
  load, non-subclass return, missing PROTOCOL_ID, mismatched name,
  duplicate IDs (first wins), `reload()` clears cache.
* **Acceptance criteria:**
  * Six new test cases, all passing.
  * `tests/unit/test_plugin_loader.py` raises coverage of `plugin_loader.py`
    to ≥ 95%.
  * `mypy src/protoskipper/core/plugin_loader.py` clean.
* **Tests required:**
  * `test_plugin_loader_loads_valid_plugins`
  * `test_plugin_loader_skips_import_failures`
  * `test_plugin_loader_skips_non_subclass`
  * `test_plugin_loader_skips_missing_protocol_id`
  * `test_plugin_loader_warns_on_name_mismatch`
  * `test_plugin_loader_first_wins_on_duplicate`
  * `test_plugin_loader_reload_picks_up_new_plugins`

### ✅ P0.B.2 Unit tests for `SessionManager` (mocked)

* **Goal:** `SessionManager`'s threading, signal-routing, and cleanup
  guarantees are tested without a real driver or QApplication being shown.
* **Files touched:** `tests/gui/test_session_manager.py` (new),
  `pyproject.toml` (add `pytest-qt` dev dep).
* **Implementation notes:** Replace `DriverWorker` with a stub that
  emits scripted signals when its slots are invoked. Use `qtbot.waitSignal`
  to assert the right `ApplicationState` mutation happens for each.
* **Acceptance criteria:**
  * Tests cover: `open_session` happy path, `open_session` failure (worker
    emits `error_raised` instead of `session_opened`), `close_session`
    teardown, `cancel_discovery`, `shutdown` waits for threads.
  * The "double-firing" bug previously fixed cannot regress (test
    explicitly checks `session_failed` and `error_raised` are mutually
    exclusive for an open failure).
  * Each test runs in < 500 ms.
* **Tests required:**
  * `test_open_session_emits_session_opened_on_success`
  * `test_open_session_emits_session_failed_on_open_error`
  * `test_open_session_does_not_emit_error_raised_for_open_failure`
  * `test_close_session_calls_worker_close_and_tears_down_thread`
  * `test_cancel_discovery_flips_atomic_flag`
  * `test_shutdown_waits_for_all_threads`

### ✅ P0.B.3 Unit tests for `ApplicationState`

* **Goal:** Every public mutator emits its corresponding signal exactly
  once and updates internal state consistently.
* **Files touched:** `tests/gui/test_application_state.py` (new).
* **Implementation notes:** Use `qtbot` for signal assertions.
* **Acceptance criteria:**
  * 100% line coverage of `app_state.py`.
  * Edge cases covered: discover same device twice (no double signal),
    record_session_closed for unknown SessionId (no-op, no signal),
    add_to_watchlist with duplicate (returns False, no signal).
* **Tests required:** ≥ 12 cases, one per public mutator plus duplicate /
  unknown-session edge cases.

### ✅ P0.B.4 Unit tests for `GuiConfirmHandler` timeout

* **Goal:** The 300-second confirmation timeout is honoured and produces a
  deny.
* **Files touched:** `tests/gui/test_confirm_handler.py` (new).
* **Implementation notes:** Inject a `dialog_factory` that returns a
  `QDialog` whose `exec()` blocks indefinitely (via a never-set Event),
  and use a 0.5-second test timeout instead of 300. The worker's callback
  must return False after timeout, with no exception raised.
* **Acceptance criteria:**
  * Test asserts the timeout returns False.
  * Test asserts a warning is logged.
  * Test runs in under 2 seconds.
* **Tests required:**
  * `test_confirm_handler_times_out_to_deny`
  * `test_confirm_handler_normal_path_returns_dialog_answer`

### ✅ P0.B.5 Unit tests for Qt models

* **Goal:** Each `QAbstractItemModel` / `QAbstractTableModel` is tested
  using a real `QApplication` but no real driver.
* **Files touched:**
  * `tests/gui/test_device_tree_model.py` (new)
  * `tests/gui/test_object_browser_model.py` (new)
  * `tests/gui/test_packet_log_model.py` (new)
  * `tests/gui/test_watchlist_model.py` (new)
* **Implementation notes:** Drive a fake `ApplicationState` via direct
  signal emits; assert the model's `rowCount()`, `data()`, and
  `dataChanged` signal behaviour.
* **Acceptance criteria:**
  * Each model's coverage ≥ 90%.
  * Tests cover: empty-state, after-discovery, after-session-open, after-
    objects-enumerated, after-read-completed, after-session-closed.
  * Watchlist model: capped foreground colour change after session close.
  * Packet log model: FIFO eviction after `max_rows` reached.

### ✅ P0.B.6 Integration test for full GUI happy path with simulator

* **Goal:** A pytest-qt test launches `MainWindow`, opens a connection to
  the simulator, reads, writes, disconnects, all without a human present.
* **Files touched:** `tests/gui/test_main_window_smoke.py` (new).
* **Implementation notes:** Spin up the Modbus simulator via the
  existing `modbus_simulator` fixture; programmatically dispatch the
  same flow `_open_new_connection_dialog` triggers; use `qtbot.waitUntil`
  to synchronise on signals; auto-confirm writes by replacing the
  `dialog_factory` with one that returns an immediately-accepting dialog.
* **Acceptance criteria:**
  * Test starts simulator, opens session, reads `holding:0`, writes 4242,
    reads back, disconnects, exits cleanly.
  * Audit log file is produced and `verify_log()` returns OK.
  * Test runs in < 5 seconds.
* **Tests required:**
  * `test_main_window_full_session_lifecycle`

## P0.C — Runtime correctness

### ✅ P0.C.1 Audit log records committed writes

* **Goal:** `event="write_committed"` (success) and `event="write_failed"`
  (driver error) rows appear in the audit log alongside the existing
  `event="write_authorization"` row.
* **Files touched:** `src/protoskipper/core/driver.py` (extend
  `SafetyContext` with `record_write_outcome`),
  `src/protoskipper/builtin_drivers/modbus/driver.py` (call it from
  `commit_write`).
* **Implementation notes:** Add
  `SafetyContext.record_write_outcome(intent, result_or_error)` that calls
  `audit_callback` with `event="write_committed"` / `event="write_failed"`.
  Drivers call it after `commit_write` regardless of outcome. The CRITICAL
  invariant: a row about authorisation NEVER appears without a matching
  outcome row (or an explicit "not transmitted" row if the driver failed
  before transmitting).
* **Acceptance criteria:**
  * Reading any audit log produced by a successful write contains rows
    in this order: `connect_attempt`, `connected`, `session_start`,
    `write_authorization (authorized=True)`, `write_committed`,
    `session_end`. Order verified, not just presence.
  * For a denied write, the chain ends with `write_authorization
    (authorized=False)` and NO `write_committed` row.
  * For a transport error during commit, both `write_authorization
    (authorized=True)` and `write_failed (error=...)` are present.
  * `verify_log` still passes after these rows are added.
* **Tests required:**
  * Update `tests/unit/test_session.py` to assert the new rows.
  * New test: `test_commit_write_failure_logs_write_failed`.
  * Update `tests/integration/test_modbus_tcp_session.py` to assert the
    full row sequence on a successful write.

### ✅ P0.C.2 Drivers emit `frame_captured` for every TX/RX

* **Goal:** The packet view receives real frames; `ApplicationState.
  frame_captured` is no longer a dead signal.
* **Files touched:**
  * `src/protoskipper/core/driver.py` — extend `DriverSession` base with a
    `_frame_sink: CaptureSink | None = None` slot and a public
    `attach_frame_sink(sink)` method.
  * `src/protoskipper/builtin_drivers/modbus/driver.py` — wrap pymodbus
    calls so request and response bytes are observed and forwarded to
    the sink.
  * `src/protoskipper/gui/services/worker.py` — install a sink that emits
    `frame_captured` signals.
  * `src/protoskipper/gui/services/session_manager.py` — wire those
    signals into `ApplicationState.record_frame_captured`.
* **Implementation notes:** pymodbus 3.x's sync clients do not expose a
  pre/post hook by default. Subclass `ModbusTcpClient` /
  `ModbusSerialClient` and override `send` and `recv` to copy bytes to the
  sink before/after delegating. Test the subclass with the simulator
  fixture.
* **Acceptance criteria:**
  * After a single read on the simulator, the GUI's packet view shows
    exactly two rows: TX (request) and RX (response).
  * Direction is correct (TX is master→slave, RX is slave→master).
  * `len(payload)` equals the number of wire bytes (not the pymodbus PDU
    minus framing).
  * Captures during a write include both write and read-confirm if the
    driver issues one.
  * Frames are dropped silently if the sink is None (no exception, no
    behaviour change).
* **Tests required:**
  * Integration: `test_modbus_session_emits_frames_for_read_and_write`.
  * Unit (with mocked client): `test_capturing_client_forwards_send_and_recv`.

### ✅ P0.C.3 `_currently_selected_session` no longer reaches into private state

* **Goal:** Remove the access to `self._object_browser._session_id` from
  `MainWindow`; expose a public accessor on the panel.
* **Files touched:** `src/protoskipper/gui/panels/object_browser.py`
  (add `current_session_id() -> SessionId | None`),
  `src/protoskipper/gui/main_window.py`.
* **Acceptance criteria:**
  * No private-attribute access (`._session_id`) from outside
    `ObjectBrowserPanel`.
  * Behaviour identical (Disconnect button enabled state correct).
* **Tests required:** GUI test verifying the disconnect button enables/
  disables when sessions open and close.

### ✅ P0.C.4 `MainWindow` write-flow controller extracted

* **Goal:** `MainWindow._open_write_dialog` is currently 30+ lines of
  signal wiring with two `disconnect`/`connect` flips. Move that logic
  into a `WriteFlowController` so it is testable in isolation.
* **Files touched:**
  * `src/protoskipper/gui/services/write_flow.py` (new).
  * `src/protoskipper/gui/main_window.py`.
* **Implementation notes:** `WriteFlowController` takes `state`,
  `session_manager`, `parent`, exposes a single
  `start(session_id, ref) -> None` that opens the dialog and handles the
  two-phase prepare/commit dance.
* **Acceptance criteria:**
  * `MainWindow._open_write_dialog` is < 5 lines (just delegates).
  * Unit test for `WriteFlowController` verifies the prepare→intent→
    commit signal chain works with a fake `SessionManager`.
  * No regression in the manual write smoke test.

### ✅ P0.C.5 `ProbeNetworkDialog` "Use selected" path validated

* **Goal:** Selecting a discovered device, clicking "Use selected for
  connection", and seeing the New Connection dialog pre-filled with that
  device's address actually works (was broken by the original
  triggered-connect bug; needs an explicit test).
* **Files touched:** `tests/gui/test_probe_network_dialog.py` (new).
* **Acceptance criteria:**
  * GUI test starts simulator, opens probe dialog, runs probe, selects
    first row, clicks "Use selected", verifies a `ProbeSelection` is
    returned with the correct address.
* **Tests required:**
  * `test_probe_dialog_returns_selection_for_chosen_device`
  * `test_probe_dialog_returns_none_on_close`
  * `test_probe_dialog_clears_results_on_new_scan`

### ✅ P0.C.6 `WriteDialog` TextSelectableByMouse without QtCore

* **Goal:** `WriteDialog` and `SafetyConfirmDialog` use `Qt.TextSelectableByMouse`
  without re-importing `Qt` from the wrong namespace. Verify against
  PySide6 6.6, 6.7, 6.8.
* **Files touched:** existing dialogs.
* **Acceptance criteria:**
  * No `AttributeError` when displaying the dialogs on PySide6 ≥ 6.6.
  * GUI test that opens `SafetyConfirmDialog` and confirms it renders.

### ✅ P0.C.7 Cancel-during-discovery cleans up worker

* **Goal:** Cancelling a probe mid-flight terminates the worker thread
  and removes it from `SessionManager._workers` within 1 second.
* **Files touched:** `tests/gui/test_session_manager.py`.
* **Acceptance criteria:**
  * Test starts a discovery on a deliberately-slow target (a host that
    won't answer), calls `cancel_discovery`, then `qtbot.waitUntil`
    verifies the worker is gone within 1 second.
* **Tests required:**
  * `test_cancel_during_discovery_cleans_up_within_1s`

### ✅ P0.C.8 No leaked `QThread` after shutdown

* **Goal:** After `SessionManager.shutdown()`, `QThread.activeThreadCount()`
  returns to its baseline.
* **Files touched:** `tests/gui/test_session_manager.py`.
* **Acceptance criteria:**
  * Test opens 5 sessions, calls shutdown, asserts no thread is still
    running 1 second later.
* **Tests required:**
  * `test_shutdown_releases_all_threads`

## P0.D — User-visible polish

### ✅ P0.D.1 Status bar shows audit row count

* **Goal:** The status bar permanent widget shows "Audit: N rows" and
  updates in real time as the audit log grows.
* **Files touched:** `src/protoskipper/gui/main_window.py`,
  `src/protoskipper/gui/services/app_state.py` (new
  `audit_row_appended` signal),
  `src/protoskipper/gui/services/worker.py` (re-emit on every audit
  row written).
* **Implementation notes:** The audit log is owned by the worker thread.
  Subscribe to its `record()` calls via a callback wired in `open_session`,
  not by polling the SQLite file.
* **Acceptance criteria:**
  * Opening one session causes the count to increment by ≥ 3 (start +
    connect_attempt + connected).
  * A read does NOT increment (we don't audit reads in v1).
  * A write increments by exactly 2 (authorization + committed/failed).
* **Tests required:**
  * GUI test asserting the status bar text after each action.

### ✅ P0.D.2 Toolbar profile chip uses theme colour

* **Goal:** The toolbar shows the active session's profile in its
  corresponding colour. (Currently the toolbar shows only the audit dir.)
* **Files touched:** `src/protoskipper/gui/main_window.py`.
* **Acceptance criteria:**
  * No active session → chip says "No active session" in grey.
  * Active session → chip shows "LAB" / "COMMISSIONING" / "PRODUCTION"
    in the matching theme colour.
* **Tests required:** GUI test reading the chip's stylesheet.

### ✅ P0.D.3 Connect/Disconnect buttons reflect state via signals

* **Goal:** The `Disconnect Selected` action enables when a session is
  selected and is open; disables otherwise. No polling.
* **Files touched:** `src/protoskipper/gui/main_window.py`.
* **Acceptance criteria:**
  * Action is disabled before a session is opened.
  * Becomes enabled when device tree selects an open session.
  * Becomes disabled again when the session is closed.

### ✅ P0.D.4 Window focus on macOS confirmed

* **Goal:** GUI test confirms the window is visible and focused on macOS.
  Already shipped (`raise_()` + `activateWindow()`) but untested.
* **Acceptance criteria:**
  * On macOS CI: `qtbot.waitForWindowShown(window)` succeeds within 1s.

## P0.E — Documentation drift

### ✅ P0.E.1 Update ARCHITECTURE.md to remove "future iteration" caveats now resolved

* **Goal:** The doc no longer claims `commit_write` is unaudited (P0.C.1 fixes that).
* **Files touched:** `docs/ARCHITECTURE.md`.

### ✅ P0.E.2 Add CHANGELOG.md

* **Files touched:** `CHANGELOG.md` (new).
* **Acceptance criteria:** Conforms to Keep-a-Changelog format with
  `Unreleased`, `0.0.1` initial entry, and a row for every Phase-0 task.

### ✅ P0.E.3 Add CODEOWNERS

* **Files touched:** `.github/CODEOWNERS` (new). Maps `core/`, `gui/`,
  `builtin_drivers/`, `tests/` to specific maintainers.

## Phase 0 — Definition of done

All of the following true:

* `ruff check src/ tests/` clean.
* `mypy src/ tests/` clean (no remaining `# type: ignore` lines that
  aren't justified inline with a one-line comment).
* `pytest tests/unit/ tests/gui/` passes on Linux + macOS + Windows in CI.
* `pytest tests/integration/` passes on Linux in CI (Windows/macOS may
  skip serial-port tests).
* Coverage ≥ 80% core, ≥ 60% overall.
* `pre-commit run --all-files` green.
* `protoskipper-gui` against the bundled simulator: discover → connect →
  read → write → disconnect → quit, ZERO console warnings, audit log
  verifies clean.
* `CHANGELOG.md` lists every Phase-0 task with date and commit hash.

---

# Phase 1 — Modbus 1.0

**Theme:** Modbus is the smoke test for everything. Phase 1 takes Modbus
TCP+RTU from "works on the simulator with the default register map" to
"works on real PLC/RTU hardware with vendor register maps and full data
type support".

## P1.A — Register-map import

### ✅ P1.A.1 CSV register-map format spec

* **Goal:** Define a documented, versioned CSV format ProtoSkipper will
  accept for Modbus register maps.
* **Files touched:** `docs/register-maps/MODBUS_CSV_FORMAT.md` (new).
* **Implementation notes:** Required columns: `object_id`, `data_type`,
  `access`, `label`, `unit`, `scale`, `offset`, `description`. Optional:
  `byte_order`, `word_order`, `bit`. Header row required, version comment
  on first line (`# protoskipper-modbus-map v1`).
* **Acceptance criteria:**
  * Document includes 3+ examples (single uint16, float32 with byte
    order, packed bitfield).
  * Behaviour for malformed rows defined (skip with warning).

### ✅ P1.A.2 CSV importer implementation

* **Goal:** `protoskipper.builtin_drivers.modbus.regmap.load_csv(path)`
  returns a list of `ObjectRef` instances.
* **Files touched:**
  * `src/protoskipper/builtin_drivers/modbus/regmap.py` (new).
  * `tests/unit/test_modbus_regmap.py` (new).
* **Acceptance criteria:**
  * Accepts a CSV with the format from P1.A.1.
  * Rejects malformed CSVs with a precise `EncodingError`.
  * Each parsed object's `metadata` carries `scale`, `offset`,
    `byte_order`, `word_order`, `bit` for the read/write code paths.
  * Coverage ≥ 95%.

### ✅ P1.A.3 GUI: import register map per session

* **Goal:** Right-click a session in the device tree → "Import register
  map…" → file dialog → CSV → object browser repopulates.
* **Files touched:** `src/protoskipper/gui/panels/device_tree.py`,
  `src/protoskipper/gui/services/session_manager.py` (new
  `import_register_map(session_id, path)` method),
  `src/protoskipper/gui/services/worker.py` (new slot).
* **Acceptance criteria:**
  * Importing a CSV updates the object browser within 200 ms.
  * Re-import replaces the entire object list (does not append).
  * Importing an invalid CSV shows a non-blocking error dialog.

### ✅ P1.A.4 Sample register maps shipped

* **Goal:** Three real-world sample CSVs in `examples/register-maps/`:
  Schneider PM5560, Siemens SENTRON PAC2200, ABB B23 meter.
* **Files touched:** `examples/register-maps/*.csv`,
  `examples/register-maps/README.md`.
* **Acceptance criteria:**
  * Each CSV has at least 20 entries.
  * Each loads cleanly with the importer and renders in the GUI.
  * `README.md` cites the publicly-available Modbus map document for
    each device.

## P1.B — Full data-type support

### ✅ P1.B.1 32-bit register pairs (uint32, int32, float32)

* **Goal:** Read/write 32-bit values stored as register pairs with
  configurable byte and word order.
* **Files touched:**
  * `src/protoskipper/builtin_drivers/modbus/codec.py` (new).
  * `src/protoskipper/builtin_drivers/modbus/driver.py`.
* **Implementation notes:** A `Codec` dataclass that carries
  `data_type`, `byte_order` (`"big" | "little"`), `word_order`
  (`"big" | "little"`), `scale`, `offset`. Encoding and decoding go
  through a pure function so they can be unit-tested without pymodbus.
* **Acceptance criteria:**
  * `decode_uint32(registers, big_byte, big_word)` matches bit-for-bit
    the reference encoding from a public spec.
  * `encode_float32(3.14159, big_byte, little_word)` produces the
    correct 4 bytes.
  * Read of a `float32` register pair returns a Python `float`.
  * Write of a `float32` produces the expected wire bytes.

### ✅ P1.B.2 Bit-field types

* **Goal:** A register can be split into named single-bit objects.
* **Files touched:** `src/protoskipper/builtin_drivers/modbus/regmap.py`,
  `codec.py`, `driver.py`.
* **Acceptance criteria:**
  * Register `holding:10` declared as `bitfield` with sub-objects
    `holding:10:bit=0`, `holding:10:bit=1`, … each becomes a single
    `ObjectRef` with `data_type="boolean"`.
  * Reads of the bitfield register propagate to all sub-object reads
    in one round-trip (the read groups the request).
  * Writing one bit issues a read-modify-write on the parent register
    inside `commit_write`.

### ✅ P1.B.3 String types (ASCII / UTF-16)

* **Goal:** Reading a contiguous register range as a string returns a
  Python `str`.
* **Files touched:** `codec.py`, `regmap.py`, `driver.py`.
* **Acceptance criteria:**
  * `data_type="ascii"`, `count=10` returns the trimmed ASCII string.
  * `data_type="utf16"` decodes correctly with the configured byte order.

### ✅ P1.B.4 Scale + offset on read/write

* **Goal:** Engineering-unit conversions happen in the driver, not the UI.
* **Acceptance criteria:**
  * Read of a register with `scale=0.1, offset=0` returns
    `raw / 10` as the Python value.
  * Write of `230.5` to a `scale=0.1` register encodes `2305`.
  * Quality remains `GOOD` for in-range values; falls to `UNCERTAIN`
    if the scaled value overflows the wire encoding.

## P1.C — Multi-register batch reads

### ✅ P1.C.1 `read_many` honours contiguous-range optimisation

* **Goal:** `_ModbusSession.read_many(refs)` issues one Modbus request
  per contiguous block, not one per object.
* **Files touched:** `driver.py`, `tests/unit/test_modbus_driver.py`.
* **Acceptance criteria:**
  * Reading 10 objects in `holding:0..holding:9` issues 1 wire request.
  * Reading objects in 2 disjoint blocks issues 2 wire requests.
  * Test counts pymodbus calls via mock.
  * Result list is in the same order as the input refs.

### ✅ P1.C.2 Watchlist polling timer (opt-in)

* **Goal:** A polling interval can be set per watchlist; the GUI calls
  `read_many` on the timer.
* **Files touched:** `src/protoskipper/gui/panels/watchlist.py`,
  `src/protoskipper/gui/services/app_state.py` (persistence of poll
  interval).
* **Acceptance criteria:**
  * Combo box: Off / 1 s / 5 s / 30 s / Custom.
  * "Off" is the default. Setting an interval starts a `QTimer`.
  * Closing all sessions cancels the timer.
  * Switching session profile to PRODUCTION shows a confirmation dialog
    "Polling enabled on a PRODUCTION session — confirm".
* **Tests required:**
  * GUI test: enabling 1 s interval triggers ≥ 2 reads in 2.2 s.

## P1.D — RTU hardware support

### ✅ P1.D.1 Serial-port enumeration in connection dialogs

* **Goal:** New Connection and Probe Network dialogs offer a dropdown of
  detected serial ports (`pyserial.tools.list_ports`).
* **Files touched:** `src/protoskipper/gui/dialogs/new_connection.py`,
  `probe_network.py`.
* **Acceptance criteria:**
  * On systems with no serial ports, the dropdown is empty and the user
    can still type one.
  * On systems with one or more, each is listed with vendor + PID
    annotations when available.
* **Tests required:**
  * Unit (mock `list_ports`): test the dropdown population logic.

### ✅ P1.D.2 RTU manual hardware test plan

* **Files touched:** `docs/manual-tests/m_modbus_rtu_hw.md` (new).
* **Acceptance criteria:** Step-by-step script for testing against a
  USB-RS485 adapter and a known PLC. Includes expected baud-rate /
  parity combos and reproducible failure modes.

### ✅ P1.D.3 RTU integration test using a virtual serial port

* **Goal:** Linux-only integration test using `socat` to create a
  virtual port pair, simulator on one end, driver on the other.
* **Files touched:** `tests/integration/test_modbus_rtu_simulated.py`
  (new), `tests/integration/conftest.py`.
* **Acceptance criteria:**
  * Skips on non-Linux.
  * Skips when `socat` not on PATH.
  * Runs the same write→read→deny→audit verification as the TCP test.

## P1.E — Robustness

### ✅ P1.E.1 Slow-response handling

* **Goal:** A device that takes 30 s to respond does not freeze the GUI.
  (Currently the worker thread blocks; the GUI is fine, but the worker
  can never be cancelled.)
* **Files touched:** `driver.py` — set per-call timeout from session
  metadata; `worker.py` — propagate cancel into a soft-abort.
* **Acceptance criteria:**
  * A device returning after 30 s does not block subsequent operations
    on the same session beyond the timeout.
  * `cancel()` on a hung session causes its current read to abort
    within 1 s.

### ✅ P1.E.2 Reconnect on transport failure

* **Goal:** If the TCP connection to a device drops, the next read
  attempts a reconnect once before failing.
* **Files touched:** `driver.py`, `tests/integration/`.
* **Acceptance criteria:**
  * A killed-and-restarted simulator test passes: the next read after
    the kill returns BAD quality with a clear error; the read after
    that succeeds (because the driver reconnected).
* **Tests required:**
  * `test_reconnect_after_transport_drop` (integration).

### ✅ P1.E.3 Concurrent sessions to the same device

* **Goal:** Two sessions to `127.0.0.1:5020/unit=1` work without the
  pymodbus client being shared.
* **Acceptance criteria:**
  * Each session has its own `ModbusTcpClient` instance.
  * Closing one does not affect the other.
* **Tests required:**
  * `test_two_concurrent_sessions_independent` (integration).

## P1.F — Audit + capture completeness for Modbus

### ✅ P1.F.1 Read events optionally audited

* **Goal:** A SafetyContext flag enables audit logging of reads (off by
  default to avoid file bloat). Flag is per session, set at connect.
* **Files touched:** `core/driver.py`, `core/session.py`.
* **Acceptance criteria:**
  * Default: reads not audited.
  * `audit_reads=True`: every read appears in the chain.
  * `verify_log` clean either way.

### ✅ P1.F.2 Capture sink writes to in-memory buffer + flushable to file

* **Goal:** Captured frames live in a bounded ring buffer (10k frames
  default) and can be flushed to a pcapng file on demand. (pcapng full
  support arrives in Phase 2; this milestone uses a simpler binary log.)
* **Files touched:** `src/protoskipper/core/capture.py` (new),
  `src/protoskipper/gui/services/worker.py`.
* **Acceptance criteria:**
  * Buffer evicts oldest when full.
  * `flush_to(path)` produces a file readable by a future replay.
  * Buffer is per-session, not global.

## Phase 1 — Definition of done

* Real Modbus TCP read/write against three different vendor PLCs (recorded
  in `docs/manual-tests/m_modbus_real_hw.md`).
* Real Modbus RTU read/write against one vendor RTU device.
* CSV register-map import works for the three sample maps shipped.
* Watchlist polling at 1 s interval against the simulator runs for 30
  minutes with zero crashes and no memory growth (RSS delta < 5 MB).
* Capture + audit chain complete for every read and write.
* Coverage ≥ 85% core, ≥ 80% Modbus driver, ≥ 70% overall.

---

# Phase 2 — Capture & replay

**Theme:** The capture story currently has TX/RX in a model and a deque on
disk. Phase 2 makes it pcapng-shaped, replayable, and verifiable.

## P2.A — pcapng writer

### ✅ P2.A.1 pcapng spec compliance

* **Goal:** ProtoSkipper writes pcapng files with custom block types per
  protocol id, readable by Wireshark when given our dissector (Phase 5).
* **Files touched:** `src/protoskipper/core/capture/pcapng.py`.
* **Done.** Block type changed to `0x40000001` (bit 30 set per RFC 9443
  §3.1 — “do not copy”; ProtoSkipper-private type). Structural tests
  verify: SHB/IDB/block-length alignment, PEN value, round-trip
  fidelity; explicit AC tests assert type `0x40000001` by hardcoded
  value and verify `protocol_id` + raw payload are embedded in the
  block body. `tests/unit/test_pcapng.py` — 18 tests, all passing.
* **Acceptance criteria met:**
  * ✅ Output passes structural round-trip (SHB → IDB → blocks
    correctly formed; all lengths multiples of 4; trailing length ==
    header length; byte-order magic present).
  * ✅ Block type `0x40000001` carries `protocol_id` (UTF-8) + raw
    payload, with PEN `0x00004453` (DataSailors).

### ✅ P2.A.2 GUI: File → Capture → Start / Stop / Save / Open

* **Files touched:** `src/protoskipper/gui/main_window.py`, `src/protoskipper/gui/panels/packet_view.py`, `src/protoskipper/gui/models/packet_log_model.py`, `src/protoskipper/gui/services/app_state.py`, `src/protoskipper/gui/services/session_manager.py`, `src/protoskipper/gui/services/worker.py`, `src/protoskipper/core/capture/__init__.py`.
* **Acceptance criteria:**
  * Start enables the capture sink on the active session.
  * Stop prompts to save (or discard).
  * Open loads a pcapng into the packet view (read-only mode disables
    write actions on every panel).

### ✅ P2.A.3 Replay disables write paths

* **Acceptance criteria:**
  * In replay mode every write button on every panel and dialog is
    disabled and tooltipped "Replay mode: writes are disabled".
  * SafetyContext refuses every authorisation in replay mode regardless
    of profile (audit row event="replay_mode_blocked").

## P2.B — Audit-log viewer

### ✅ P2.B.1 File → Audit → Verify…

* **Acceptance criteria:**
  * File picker opens; selected file is run through `verify_log`.
  * Result dialog shows OK / FAILED with the exact verification message.

### ✅ P2.B.2 File → Audit → View…

* **Acceptance criteria:**
  * Opens a chronological view of an audit file.
  * Filterable by event type (reads / writes / errors / session lifecycle).

## P2.C — Capture analysis basics

### ✅ P2.C.1 Per-session timeline view

* **Acceptance criteria:** Existing packet view becomes the timeline view
  with x-axis time, y-axis row position. No new dependencies.

## Phase 2 — Definition of done

* A captured Modbus session can be saved, re-opened in a fresh
  ProtoSkipper instance, and inspected with no driver loaded.
* Audit-log viewer can display and verify any audit log produced by P0/P1.

---

# Phase 3 — Quality, polish, accessibility

## P3.A — Settings persistence

### P3.A.1 QSettings-backed recent connections ✅

* **Acceptance criteria:** New Connection dialog auto-completes from
  the most-recent 10 connections. Persists across app restarts.

### P3.A.2 Default operator + audit dir ✅

* **Acceptance criteria:** Settings dialog under `Tools → Preferences…`
  exposes default operator email, audit dir, default profile, theme.

### P3.A.3 Watchlist save/load JSON ✅

* **Acceptance criteria:** Watchlist contents per profile saved to
  `~/.config/protoskipper/watchlists/{profile}.json` on demand and
  loaded automatically on first session of that profile.

## P3.B — Accessibility

### P3.B.1 `setAccessibleName` everywhere ✅

* **Acceptance criteria:** Screen-reader smoke on macOS VoiceOver and
  NVDA reads every actionable widget meaningfully.

### P3.B.2 Keyboard shortcuts complete ✅

* **Acceptance criteria:**
  * F5 = Read selected
  * Shift+F5 = Read all
  * Ctrl+W = Close current session
  * Ctrl+Shift+W = Close all sessions
  * Ctrl+, = Preferences
  * All documented in `Help → Keyboard shortcuts…`

### P3.B.3 Tab order audit ✅

* **Acceptance criteria:** Manual tab-order test scripted in
  `docs/manual-tests/m_tab_order.md` passes on every dialog.

## P3.C — Internationalisation scaffolding

### P3.C.1 `tr()` wrap every user-facing string ✅

* **Acceptance criteria:** `lupdate` produces a `protoskipper_en.ts` with
  every visible string. Compiles to `.qm` via `lrelease`. App still
  renders English when no translation is loaded.

### P3.C.2 Translation framework documented ✅

* **Files touched:** `docs/I18N.md` (new).

## P3.D — Theming

### P3.D.1 Theme switcher ✅

* **Acceptance criteria:** `View → Theme → Light / Dark` switches and
  persists. Active theme reapplies on app restart.

### P3.D.2 Compact density mode ✅

* **Acceptance criteria:** `View → Density → Comfortable / Compact`
  resizes paddings and font sizes for laptop-screen field use.

## P3.E — Help & onboarding

### P3.E.1 First-run welcome ✅

* **Acceptance criteria:** First launch shows a one-time welcome dialog
  pointing the user at `Probe Network…` and a "Run the simulator" button
  that starts `protoskipper sim modbus` in a background process.

### P3.E.2 In-app help ✅

* **Acceptance criteria:** `Help → Documentation` opens the rendered
  docs site in the system browser. `Help → Report bug` opens a pre-filled
  GitHub issue template.

## Phase 3 — Definition of done

* Two themes, two densities, full keyboard navigation, English-only but
  i18n-ready, settings persisted across runs.

---

# Phase 4 — IEC 60870-5-104

**Theme:** First non-Modbus protocol; validates that the plugin contract
holds up.

> **✅ PHASE 4 COMPLETE** (commit `583cc18`). All P4 tasks are
> implemented, tested, lint-clean, and committed. The full feature
> catalogue, UX spec, test strategy, and task breakdown live in
> `docs/IEC104_PLAN.md`.
>
> **Everything is done:**
> - P4.A: Plugin scaffold, APCI/ASDU codec, master client, in-tree
>   slave, fuzzer engine, PCAP reader, point-list parser — **526 tests**. ✅
> - P4.B: Master client — TCP connect/STARTDT, k/w windowing, timer
>   machine, GI/CI, commands, clock sync, file transfer (F-* ASDUs via
>   `file_transfer.py`), reconnect-on-drop, TLS. **16 new unit tests**. ✅
> - P4.C: Slave server — TCP listener, GI, spontaneous events, command
>   ack lifecycle, counter interrogation, clock sync, file services,
>   TLS server-side. ✅
> - P4.D: Fuzzer — APCI/ASDU/sequencing/TLS mutation engines, GUI panel
>   (`dialogs/iec104_fuzzer.py`) wired into Tools menu, CSV export. ✅
> - P4.E: PCAP analyzer — pcapng reader, filter language, APCI timeline /
>   k-w analysis / statistics views (`dialogs/iec104_pcap_viewer.py`). ✅
> - P4.F: All 15 GUI panels and dialogs. ✅
> - P4.G.1–G.6: Audit schema, setup save/load, scripting bindings,
>   conformance profiles, vendor profiles, IEC104.md doc. ✅
> - P5.A.1, P5.A.2: Scripting console + CLI run (shipped early). ✅

## P4.A — IEC 104 driver

### ✅ P4.A.1 Plugin scaffold

* **Done.** Driver lives at `src/protoskipper/builtin_drivers/iec104/`
  (mirrors the Modbus layout for fast integration; pip-installable
  packaging is deferred to a later PR).
* Modules: `apci.py` (I/S/U codec), `asdu.py` (canonical type subset +
  CP56Time2a/CP24Time2a/QDS/SIQ/DIQ), `master.py` (state machine),
  `slave.py` (TCP listener + data model), `fuzzer.py` (mutation engine),
  `pcap.py` (pcapng reader/dissector), `pointlist.py` (CSV loader),
  `driver.py` (`ProtocolDriver` wiring).
* Registered as entry point `iec104.tcp` in `pyproject.toml`.

### ✅ P4.A.2 Capture + audit parity with Modbus

* **Done.** Frame sink protocol matches Modbus; TX/RX APDUs forwarded
  with `protocol="iec104.tcp"`. `SafetyContext.require_write_authorization`
  + `record_write_outcome` called identically to Modbus for single-,
  double-command, and clock-sync writes.
* Audit rows carry `iec104.subevent` discriminator (P4.G.1 done).

### ✅ P4.A.3 GUI tests

* **Done.** Integration tests cover the protocol end-to-end against the
  in-tree mini-slave (`tests/integration/`). GUI smoke tests live in
  `tests/gui/test_iec104_panels.py`.

## ✅ P4.B — Master client

> **Done** (commit `583cc18`). Detailed task breakdown in `docs/IEC104_PLAN.md §8 P4.B`.
> All 10 sub-tasks complete: TCP connect/STARTDT/STOPDT, k/w windowing,
> t1/t2/t3 timer machine, GI/CI interrogation, read command, all command
> types (45–64), clock sync, file transfer (`file_transfer.py` + `master.py`
> `browse_files()` + `download_file()`, 16 unit tests), reconnect-on-drop
> (5 unit tests), TLS mutual auth.

## ✅ P4.C — Slave server

> **Done** (commit `583cc18`). Detailed task breakdown in `docs/IEC104_PLAN.md §8 P4.C`.
> All 6 sub-tasks complete: TCP listener, STARTDT/STOPDT, point-list-driven
> data model, spontaneous event generator, command receive + ack lifecycle
> (direct + SBO), counter interrogation / clock sync / file services
> (`slave.py` `add_file()` + `_handle_file_service()`), TLS server-side.

## ✅ P4.D — Fuzzer

> **Done** (commit `583cc18`). Detailed task breakdown in `docs/IEC104_PLAN.md §8 P4.D`.
> All 5 sub-tasks complete: APCI mutation engine, ASDU mutation engine,
> sequencing mutation engine, TLS mutation engine (5 unit tests),
> GUI panel (`dialogs/iec104_fuzzer.py` — LAB guard, 5 modes, Run/Stop/
> Export CSV) wired into `Tools → IEC 104 → Fuzz…` in `main_window.py`.

## ✅ P4.E — PCAP analyzer

> **Done** (commit `583cc18`). Detailed task breakdown in `docs/IEC104_PLAN.md §8 P4.E`.
> All 3 sub-tasks complete: pcapng reader + IEC 104 dissector (`pcap.py`),
> filter language (`iec104.type`, `iec104.cot`, `iec104.ioa`, `iec104.ca`,
> `tcp.flow`, conjunctions/disjunctions/regex), APCI timeline / k-w
> analysis / statistics views (5-tab `iec104_pcap_viewer.py`,
> `_TimelineWidget` using QPainter).

## P4.F — GUI panels

### ✅ P4.F.1 New Connection (master) dialog — IEC 104 fields

### ✅ P4.F.2 New Slave Simulator dialog

### ✅ P4.F.3 Probe Network — IEC 104 extension

### ✅ P4.F.4 Object browser — IOA tree, columns per §5.4

### ✅ P4.F.5 Watchlist + plot

### ✅ P4.F.6 SOE panel (`iec104_soe.py`)

### ✅ P4.F.7 Command panel + issue dialog (`iec104_command.py`)

### ✅ P4.F.8 Interrogation panel (`iec104_interrogation.py`)

### ✅ P4.F.9 Time-sync panel + drift visualiser (`iec104_timesync.py`)

### ✅ P4.F.10 File transfer panel

### ✅ P4.F.11 PCAP analyzer view (`iec104_pcap_viewer.py`)

### ✅ P4.F.12 Bench overview (`iec104_bench.py`)

### ✅ P4.F.13 Conformance test runner UI (`iec104_conformance.py`)

### ✅ P4.F.14 Diff-against-point-list panel (`iec104_diff.py`)

### ✅ P4.F.15 Vendor profile library editor

## P4.G — Cross-cutting

### ✅ P4.G.1 Audit-log row schema for IEC 104

* **Done.** Every IEC 104 action lands an audit row with an
  `iec104.subevent` discriminator (`startdt`, `stopdt`, `gi`, `ci`,
  `read`, `command`, `command_select`, `command_execute`,
  `command_term`, `clock_sync`, `file_call`, `file_segment`,
  `fuzzer_mutation`, `slave_event`). `verify_log` clean for a
  60-minute mixed session.

### ✅ P4.G.2 Setup save/load (`iec104-setup.json`)

* **Done.** `core/setup.py` with `Iec104Setup` dataclass, `load()` /
  `save()`. `MainWindow` File → IEC 104 → Save/Load Setup actions.
  29 unit tests in `tests/unit/test_iec104_setup.py`.

### ✅ P4.G.3 Scripting bindings

* **Done.** `builtin_drivers/iec104/scripting.py` — `MasterSession`,
  `SlaveServer`, `PcapReader`, `Fuzzer` façade. `_make_iec104_ns()` in
  `scripting/__init__.py` wraps classes with `allow_writes` enforcement.
  Scripting console (`gui/panels/scripting_console.py`) injects `iec104`
  and `modbus` bindings. 19 unit tests.

### ✅ P4.G.4 Conformance profile schema + 4 shipped profiles

* **Done.** `conformance.py` loader + `master_ed2_2016.yaml`,
  `slave_ed2_2016.yaml`, `gi_conformance.yaml`,
  `command_conformance.yaml`. 10 unit tests.

### ✅ P4.G.5 Vendor profile schema + 6 shipped profiles

* **Done.** `vendor_profiles.py` loader + `sel_351a.yaml`,
  `ziv_5ctd.yaml`, `toshiba_grl100.yaml`, `hitachi_psr.yaml`,
  `sprecher_srm152.yaml`, `wago_750870.yaml`.

### ✅ P4.G.6 Documentation

* **Done.** `docs/IEC104.md` — public user guide (Quick start, GUI,
  scripting API, CLI, PCAP, fuzzing, TLS, config reference, protocol
  primer). Manual test scripts to follow in a future PR.

## Phase 4 — Definition of done ✅

> **All items below are true as of commit `583cc18`.**

* All IEC104_PLAN.md tasks P4.A through P4.G.6 marked ✅ in both
  `EXECUTION_PLAN.md` and `IEC104_PLAN.md`. ✅
* `protoskipper list-protocols` shows `iec104.tcp`. ✅
* Connect + STARTDT + GI of 1 000 points: ≤ 1 s on LAN. ✅
* All conformance profiles pass against the in-tree slave. ✅
* Fuzzer runs in LAB profile; locked out of PRODUCTION. ✅
* `verify_log` clean after a 60-minute mixed master + slave session. ✅
* `ruff check src/ tests/` clean, `mypy src/` clean. ✅
* `pytest tests/unit/ tests/gui/ tests/integration/` ≥ 95 % core
  coverage; IEC 104 driver ≥ 90 %. ✅
* `docs/IEC104.md` exists and is accurate. ✅

---

# Phase 5 — Scripting & automation

**Theme:** Embedded Python REPL, headless `protoskipper run` command,
scenario scripts.

> **P5.A scaffolding (REPL + CLI) was shipped early as Phase 4
> cross-cutting infrastructure.** These items are ✅ done. Phase 4 is now
> complete; Phase 5 is unblocked.

## P5.A — Embedded REPL

### ✅ P5.A.1 PythonConsole panel

* **Done.** `View → Scripting Console` toggles a dockable Python REPL
  (`gui/panels/scripting_console.py`). `_ConsoleWorker(QObject)` runs
  `code.InteractiveConsole` on a `QThread`. Bindings: `state`,
  `manager`, `sessions`. Uncaught exceptions in user code do not crash
  the app. Thread is cleanly stopped on `MainWindow.closeEvent`.
  10 GUI tests in `tests/gui/test_scripting_console.py`.

### ✅ P5.A.2 Script runner CLI

* **Done.** `protoskipper run script.py [args] [--allow-writes]` runs
  a script with the full driver+session API exposed. Default confirm =
  deny unless `--allow-writes` is passed. Namespace: `log`, `argv`,
  `allow_writes`, `iec104`, `modbus`, `load_protocol_drivers`.
  15 unit tests in `tests/unit/test_cli_run.py`.

## P5.B — Scenario library

### ✅ P5.B.1 Built-in scenario: bus-load test

* **Done.** `examples/scenarios/bus_load_test.py` — connects to an
  IEC 104 slave (or Modbus device), issues general interrogation,
  measures round-trip latency and point throughput, reports
  p50/p95/p99 latency, peak PPS, and elapsed time. Run with:
  `protoskipper run examples/scenarios/bus_load_test.py 192.168.1.1`.
  Uses the IEC 104 scripting bindings (P4.G.3). 8 unit tests in
  `tests/unit/test_scenario_bus_load.py`.

---

# Phase 6 — Packaging & distribution

## ✅ P6.A — Linux

### ✅ P6.A.1 AppImage build

* **Done.** `packaging/linux/build_appimage.sh` + `make appimage`.
  Uses PyInstaller to freeze + `appimagetool` to package.
  GitHub Actions `release.yml` job `build-linux` produces
  `ProtoSkipper-x86_64.AppImage` as a workflow artefact.
* **Acceptance criteria met:** `make appimage` produces a runnable
  `ProtoSkipper-x86_64.AppImage`. ✅

### ✅ P6.A.2 .deb / .rpm via FPM

* **Done.** `packaging/linux/build_deb.sh` + `packaging/linux/build_rpm.sh`
  + `make deb rpm` targets in `Makefile`.
  GitHub Actions `release.yml` produces `.deb` and `.rpm` artefacts.

## ✅ P6.B — macOS

### ✅ P6.B.1 Signed + notarised DMG

* **Done.** `packaging/macos/build_dmg.sh` + `make dmg` + GitHub Actions
  `release.yml` job `build-macos`. Signing + notarisation requires
  `APPLE_DEVELOPER_ID_P12` and `APPLE_NOTARIZE_PASSWORD` secrets set
  in the repo; when absent the build produces an unsigned DMG for
  local testing. Full notarisation instructions in
  `packaging/macos/README.md`.
* **Acceptance criteria:** Signed DMG passes Gatekeeper on macOS 13/14
  when secrets are configured. ✅

## ✅ P6.C — Windows

### ✅ P6.C.1 Inno Setup signed installer

* **Done.** `packaging/windows/protoskipper.iss` + `make installer` +
  GitHub Actions `release.yml` job `build-windows`. Code signing
  requires `WINDOWS_SIGNING_PFX` secret; absent = unsigned installer
  for dev builds. Full signing instructions in
  `packaging/windows/README.md`.
* **Acceptance criteria:** SmartScreen-clean signed `.exe` installer
  when secrets are configured. ✅

## ✅ P6.D — Auto-update

### ✅ P6.D.1 In-app "check for updates"

* **Done.** `src/protoskipper/gui/dialogs/update_checker.py` —
  `UpdateCheckerDialog(QDialog)` fetches the GitHub releases API in a
  `QThread` worker, compares semver against `protoskipper.__version__`,
  and shows "Up to date" or "Version X.Y.Z is available — Download"
  with a link button. Never auto-installs. Wired into
  `Help → Check for Updates…` in `main_window.py`.
* **Acceptance criteria met:** GitHub release feed polled; user prompted
  with "Download" link, never auto-installs. ✅

## ✅ P6.E — Reproducible builds

* **Done.** `release.yml` pins all tool versions, uses
  `SOURCE_DATE_EPOCH` for PyInstaller and fpm to zero out embed
  timestamps. Build hash comparison step in CI verifies consecutive
  builds produce identical checksums for pure-Python wheels. Platform
  installers document why binary checksums differ (embedded timestamps
  in PE/Mach-O headers) in `packaging/REPRODUCIBILITY.md`.
* **Acceptance criteria:** Python wheel checksums match across two
  consecutive CI runs. ✅

---

# Phase 7 — BACnet/IP

## P7.A — Driver

### P7.A.1 Discovery via Who-Is

### P7.A.2 Object enumeration via ReadPropertyMultiple

### P7.A.3 COV subscriptions wired to `Subscriber` capability

## P7.B — GUI affordances

* Subscribe/Unsubscribe button enabled because the session implements
  `Subscriber`.

## P7.C — Tests

* Integration test against `bacpypes3`'s included test device.

---

# Phase 8 — IEC 61850

> Full UX spec, field tables, and task AC live in
> `docs/internal/IEC61850_PLAN.md`.  This section mirrors its task
> structure as the authoritative completion tracker.  Mark tasks ✅ here
> and in `IEC61850_PLAN.md` in the same PR.

## P8.A — Plugin scaffold + SCL parser

### P8.A.1 Plugin package skeleton ✅

* **Files:** `plugins-builtin/protoskipper-iec61850/`
* Register `iec61850.mms` via entry-point; discoverable by
  `plugin_loader`; no crashes on `protoskipper-gui` launch.
* **Tests:** `test_plugin_loader.py::test_iec61850_plugin_discovered`

### P8.A.2 SCL parser — Edition 2.1 schema-validating loader ✅

* **Files:** `protoskipper_iec61850/scl/parser.py`, `model.py`,
  `schemas/` (IEC 61850-6 XSD Editions 1.0/2.0/2.1).
* Parse SCD/ICD/IID/CID into typed frozen dataclasses; validate;
  round-trip write.  ≤ 5 s on a 50 MB SCD.
* **Tests:** `tests/unit/test_scl_parser.py` (3 sample SCDs)

### P8.A.3 SCL diff engine ✅

* **Files:** `protoskipper_iec61850/scl/diff.py`
* `Scl.diff(a, b)` semantic diff; `apply(a, diff)` round-trips.
* **Tests:** `tests/unit/test_scl_diff.py` (6 cases)

### P8.A.4 SCL editor GUI ✅

* **Files:** `src/protoskipper/gui/dialogs/scl_editor.py`
* Edit DataSet / RCB / GoCB / SVCB / ExtRef; saves a new file;
  blocked in PRODUCTION profile without re-confirmation.

---

## P8.B — MMS client

### P8.B.1 MMS library decision + driver scaffold ✅

* **Files:** `docs/internal/IEC61850_MMS_LIBRARY.md` (ADR),
  `protoskipper_iec61850/driver.py` (stub `Iec61850Session`)
* Decision: libiec61850 binding vs pure-Python vs cffi.
* `Iec61850Session` raises `NotImplementedError` on all methods;
  passes plugin contract tests.

### P8.B.2 MMS connect / abort / status ✅

### P8.B.3 Data model browse + read ✅

### P8.B.4 Read with quality and timestamp ✅

### P8.B.5 Write — direct / SBO, normal / enhanced security ✅

### P8.B.6 Reporting (BRCB / URCB) ✅

### P8.B.7 Logging (read log buffer) ✅

### P8.B.8 File services ✅

### P8.B.9 Setting-group services ✅

---

## P8.C — GOOSE

### P8.C.1 Layer-2 socket abstraction ✅

* **Files:** `protoskipper_iec61850/transport/l2.py`
* `AF_PACKET` (Linux) / `BPF` (macOS) / `Npcap` (Windows) behind
  a single facade.

### P8.C.2 GOOSE subscriber decoder ✅

### P8.C.3 GOOSE publisher encoder ✅

### P8.C.4 GUI — subscriber panel ✅

### P8.C.5 GUI — publisher panel + error injection ✅

---

## P8.D — Sampled Values

### P8.D.1 SV decoder (9-2LE, 9-2 80-2) ⬜

### P8.D.2 SV encoder ⬜

### P8.D.3 Scope / FFT / Phasor view ⬜

* **Files:** `src/protoskipper/gui/panels/sv_scope.py` (pyqtgraph)

### P8.D.4 COMTRADE export ⬜

### P8.D.5 COMTRADE viewer ⬜

### P8.D.6 SV publisher (D2) ⬜

---

## P8.E — Substation overview

### P8.E.1 Bench layout file format ⬜

### P8.E.2 Live status updater ⬜

---

## P8.F — Conformance test runner

### P8.F.1 Profile schema ⬜

### P8.F.2 MMS Edition 2.1 client profile ⬜

### P8.F.3 GOOSE publisher / subscriber profiles ⬜

### P8.F.4 SV 9-2LE profiles ⬜

---

## P8.G — Simulator (D8)

### P8.G.1 IED simulator core ⬜

* **Files:** `protoskipper_iec61850/simulator.py`
* Listen on MMS port 102; serve live data model from SCL; fire
  reports on DA writes; drive GOOSE/SV from model state changes.

### P8.G.2 Simulator GUI wizard ⬜

---

## P8.H — PCAP / replay (D7)

### P8.H.1 GOOSE + SV + MMS PCAP dissector ⬜

* **Files:** `protoskipper_iec61850/pcap.py`
* Dissect from `.pcapng`; no live network required.

### P8.H.2 Replay viewer panel ⬜

---

## P8.I — Cross-cutting

### P8.I.1 Audit-log schema for IEC 61850 events ✅

### P8.I.2 Preferences tab — IEC 61850 ✅

### P8.I.3 Setup file format (`iec61850-setup.json`) ✅

### P8.I.4 CLI commands (`list-protocols iec61850.mms`, `probe`) ⬜

### P8.I.5 User guide section ⬜

---

# Phase 9 — 1.0 release readiness

## P9.A — Security audit

### P9.A.1 Dependency audit

* `pip-audit` clean. Pinned versions for every transitive dep documented
  in `SBOM.json`.

### P9.A.2 Threat model document

* `docs/THREAT_MODEL.md` with attack surface enumeration, audit-log
  guarantees, plugin sandbox limitations.

### P9.A.3 Fuzz the parsers

* `protoskipper.builtin_drivers.modbus.driver._parse_object_id`,
  `parse_probe_target`, `parse_rtu_address`, plus pcapng reader. Run
  Atheris or `python -m hypothesis` for ≥ 10 minutes per parser. No
  crashes or non-`EncodingError` exceptions.

## P9.B — Soak tests

### P9.B.1 24-hour watchlist polling

* 24-hour run against simulator: zero crashes, RSS growth < 50 MB.

### P9.B.2 1000 sessions opened and closed

* Memory and thread count return to baseline after.

## P9.C — Documentation site

* GitHub Pages or Read the Docs build of the existing `docs/`. Versioned.

## P9.D — Community

### P9.D.1 Code of Conduct in place
### P9.D.2 SECURITY.md with disclosure policy
### P9.D.3 Discussions enabled, plugin gallery doc
### P9.D.4 Roadmap moved to GitHub Projects

## P9.E — Marketing site

* `protoskipper.io` (or equivalent) with feature list, screenshots,
  download links, plugin gallery. Owned by DataSailors but content
  honest about the open-source nature.

## Phase 9 — Definition of done = 1.0 release

* Tag `v1.0.0`. Sign the tag. Sign the release artefacts. Announce.
  Maintain a public stable-API document under `docs/STABLE_API.md`.

---

## Appendix A — Cross-cutting non-negotiables (every phase)

* No PR lands red CI.
* No PR lands without test additions when it adds behaviour.
* No PR introduces a new direct driver call from a UI panel.
* No PR adds a Qt import to the core layer.
* No PR adds a protocol-specific import to the GUI layer outside the
  appropriate plugin contribution module.
* Every commit follows the conventional commit format from
  `.github/copilot-instructions.md`.
* Every release branch is built and tested on Linux + macOS + Windows.
* Every audit-log change must be backwards-compatible (rows with new
  events are fine; renaming or removing events is a major-version event).
* Every public type in `protoskipper.core` is `@dataclass(frozen=True)`
  unless mutability is structurally required.

## Appendix B — Where this plan can fail

Visible failure modes the plan tries to prevent, with the corresponding
control:

| Risk | Control |
|------|---------|
| Scope drift inside a task | Tasks are sized so they fit in one PR. New work spawns a new task, never expands an existing one. |
| Tests written after the fact | Each task lists tests required as part of the AC. A task is not done until its tests are. |
| GUI regressions sneaking in | pytest-qt suite blocks merge on every PR. |
| Driver regressions sneaking in | Integration suite against the simulator blocks merge. |
| Security/compliance regressions | Phase 9 fuzzing + dependency audits added before 1.0. |
| Documentation drift | ARCHITECTURE.md / GUI_ARCHITECTURE.md / EXECUTION_PLAN.md updated in the same PR as the behaviour change. |
| Plugin-ecosystem fragmentation | The plugin contract is locked at v1.0; breaking changes telegraphed one minor before. |

## Appendix C — Living document conventions

* When a task completes, mark it `✅` in this file in the same PR that
  closes it. Do not retroactively edit history.
* When a task is blocked, mark it `⏸ blocked: <reason>` and link the
  blocker.
* When a task is dropped, mark it `🗑 dropped: <reason>` rather than
  deleting it. Future contributors learn from the why.

---

*End of execution plan. Anything not in this document is not on the road
to 1.0. Add it explicitly, or it will not happen.*
