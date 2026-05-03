# Changelog

All notable changes to ProtoSkipper are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

#### GUI — real-hardware commissioning improvements

- **Poll-all fix** — "Poll All" combo now applies the selected interval to
  registers added *after* the combo was set, not only to pre-existing ones.

- **Setup save/restore with registers** — `File → Save Setup` now persists the
  full register list alongside the connection details. `File → Open Setup`
  restores the registers atomically, using a second `QueuedConnection` handler
  that fires after the driver's empty `enumerate_objects` result lands so the
  injected objects are never wiped by the worker.

- **Add & Next / duplicate check** — `AddRegisterDialog` has an "Add && Next"
  button that pre-fills the following address automatically so a user can
  add a run of consecutive registers without re-opening the dialog. The dialog
  also accepts an `existing_ids` set; attempting to add a duplicate shows a
  warning and re-prompts. Right-click → "Add next register (same table+type)…"
  provides the same flow from any selected row in the register table.

- **Register map export / import** — "Export Map…" and "Import Map…" toolbar
  buttons let users save a device's register layout to a JSON file and load it
  on a different device of the same type. Maps are stored in
  `~/.protoskipper/maps/`.

- **Grid lines visible in dark mode** — Fusion dark palette now sets
  `QPalette.ColorRole.Mid` and an explicit `QTableView { gridline-color }` QSS
  so column dividers are visible in both Light and Dark themes.

- **Gap between Unit and Address columns** — `_LeftPaddedDelegate` (16 px left
  padding) is installed on the Address column so addresses are easy to read
  alongside unit IDs.

- **Float rounding** — `AddRegisterDialog` shows a "Decimal places" spinbox
  (range 0–10, special value "No rounding") when `float32` or `float64` is
  selected. The chosen value is stored in register metadata and applied in the
  register table's value display.

- **Row-click deselect** — Clicking a selected row a second time now
  deselects it (toggle behaviour). Clicking empty space below the last row
  also clears selection. Previously a row, once clicked, stayed highlighted
  with no way to deselect.

#### GUI — quality & polish (Phase P3)

- **Light-mode palette fix** — All palette roles correctly use light colours;
  was previously inheriting dark values after theme switch.

- **Per-row polling** — Each register row has its own poll-interval combo
  (None / 1 s / 5 s / 10 s / 30 s / 60 s), independent of the "Poll All"
  global control.

- **Column reorder** — Object browser columns are now: Type | Address | Tag |
  Value | Unit | Poll | Read | Write (optimised for left-to-right workflow).

- **Panel toggles** — View menu has checkable items to show/hide each dock
  panel (Device Tree, Watchlist, Packet Log).

#### Earlier GUI milestones

- **P3** (commit `b6f8953`) — 12 quality and accessibility tasks: keyboard
  shortcuts, status-bar session summary, tooltips, column resize policies,
  alternate-row colouring, empty-state placeholders.

- **P2.C.1** — Packet view timeline with delta-time column.

- **P2.B.1-2** — Audit log verify dialog and view-log dialog.

- **P2.A.3** — Replay mode disables all write paths globally.

- **P2.A.2** — Capture start/stop/save/open menu actions.

- **P2.A.1** — pcapng writer with SHB + IDB + Custom Block round-trip.

- **P0.C-D** — Probe network dialog, session lifecycle (connect / disconnect /
  reconnect), clone-unit action, structured Host/Port/Unit fields.

- **P0.B.6** — Full session lifecycle smoke test against the in-process simulator.

---

### Phase 0 — Stabilise the scaffold

#### Tooling hygiene (P0.A)

- **P0.A.1** — Added `.pre-commit-config.yaml` with 11 hooks (ruff, ruff-format, mypy,
  pytest unit, end-of-file-fixer, trailing-whitespace, check-yaml, check-toml, debug-statements).
  `CONTRIBUTING.md` updated with Developer setup and Pre-commit hooks sections.
  _Commit: feat(core,modbus,ci,test): P0.A-C.2 tooling, plugin tests, write-outcome audit, frame capture_

- **P0.A.2** — Added `.github/workflows/ci.yml` with 4 CI jobs: lint (ruff + mypy),
  unit tests (3×3 OS×Python matrix), integration tests (Ubuntu), GUI tests (xvfb-run).
  Concurrency group cancels in-progress runs on the same branch.
  _Commit: feat(core,modbus,ci,test): P0.A-C.2 tooling, plugin tests, write-outcome audit, frame capture_

- **P0.A.3** — Added `[tool.coverage.run]` and `[tool.coverage.report]` to `pyproject.toml`
  with `fail_under = 80` on the core layer.
  _Commit: feat(core,modbus,ci,test): P0.A-C.2 tooling, plugin tests, write-outcome audit, frame capture_

- **P0.A.4** — All dev tool deps changed from `>=` ranges to exact `==` pins
  (pytest, pytest-asyncio, pytest-cov, pytest-qt, ruff, mypy, build, pre-commit).
  _Commit: feat(core,modbus,ci,test): P0.A-C.2 tooling, plugin tests, write-outcome audit, frame capture_

#### Test coverage gaps (P0.B)

- **P0.B.1** — Added `tests/unit/test_plugin_loader.py` (11 tests, 100% coverage of
  `plugin_loader.py`). Covers: happy path, ImportError, non-subclass, missing PROTOCOL_ID,
  name mismatch, duplicate IDs (first wins), reload(), lru_cache, and real entry-point scan.
  _Commit: feat(core,modbus,ci,test): P0.A-C.2 tooling, plugin tests, write-outcome audit, frame capture_

#### Runtime correctness (P0.C)

- **P0.C.1** — Audit log now records write outcomes. Added
  `SafetyContext.record_write_outcome(result: WriteResult)` which emits
  `event="write_committed"` (success) or `event="write_failed"` (transport error).
  Modbus driver calls it on all three return paths. The audit chain is now complete:
  every authorised write has a matching outcome row.
  _Commit: feat(core,modbus,ci,test): P0.A-C.2 tooling, plugin tests, write-outcome audit, frame capture_

- **P0.C.2** — Drivers now emit raw wire frames to `ApplicationState.frame_captured`.
  Added `_CapturingMixin` that subclasses `ModbusTcpClient` / `ModbusSerialClient`
  and overrides `send`/`recv` to copy bytes to a `CaptureSink`. Added
  `DriverSession.attach_frame_sink()` concrete method. `DriverWorker` installs a
  `_QtFrameSink` that emits the new `frame_captured` Qt signal. `SessionManager` wires
  that signal into `ApplicationState.record_frame_captured`. `CaptureSink` is now
  `@runtime_checkable`.
  _Commit: feat(core,modbus,ci,test): P0.A-C.2 tooling, plugin tests, write-outcome audit, frame capture_

- **P0.C.3** — Removed `self._object_browser._session_id` private access from `MainWindow`.
  Added `ObjectBrowserPanel.current_session_id() -> SessionId | None` public accessor.
  `MainWindow._currently_selected_session` updated to use it.
  _Commit: refactor(gui): P0.C.3 add current_session_id() to ObjectBrowserPanel_

- **P0.C.4** — Extracted `WriteFlowController` from `MainWindow._open_write_dialog`.
  The 50-line inline method is now a standalone testable class in
  `src/protoskipper/gui/services/write_flow.py`. `MainWindow._open_write_dialog` is now
  a 1-line delegate. Added 3 unit tests in `tests/unit/test_write_flow.py`.
  _Commit: refactor(gui): P0.C.4 extract WriteFlowController from MainWindow_

- **P0.C.6** — Updated both `WriteDialog` and `SafetyConfirmDialog` to use
  `Qt.TextInteractionFlag.TextSelectableByMouse` (fully-qualified enum) instead of the
  deprecated short form. Confirmed safe on PySide6 ≥ 6.6.
  _Commit: fix(gui): P0.C.6 use Qt.TextInteractionFlag.TextSelectableByMouse for PySide6 6.6+ compat_

---

## [0.0.1] — Initial scaffold

_Date: 2026-01 (approximate)_

### Added

- 4-layer architecture: GUI (PySide6/Qt6), CLI (argparse), Core (protocol-agnostic),
  Drivers (pymodbus built-in + plugin entry-point discovery).
- `ProtocolDriver` / `DriverSession` ABCs with full dataclass contract
  (`DeviceRef`, `ObjectRef`, `ReadResult`, `WriteIntent`, `WriteResult`).
- `SafetyContext` with 3-tier profile (LAB / COMMISSIONING / PRODUCTION) and
  HMAC-chained SQLite WAL audit log.
- `SessionManager` with QThread-per-session model; `GuiConfirmHandler` for
  cross-thread confirmation with 300-second timeout.
- Modbus TCP and RTU drivers (pymodbus 3.x, lazy-imported); CIDR / host-list probe.
- `ApplicationState` as the single source of truth for GUI panels.
- Plugin entry-point discovery via `protoskipper.protocols` group.
- CLI: `list-protocols`, `scan`, `connect` subcommands.
- Unit tests (`tests/unit/`), integration tests (`tests/integration/`) with
  in-process Modbus simulator.

[Unreleased]: https://github.com/datasailors/protoskipper/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/datasailors/protoskipper/releases/tag/v0.0.1
