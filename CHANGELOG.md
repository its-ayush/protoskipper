# Changelog

All notable changes to ProtoSkipper are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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
