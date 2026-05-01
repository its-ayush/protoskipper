# ProtoSkipper — Copilot Instructions

## Memory Bank

At the start of every session, load context from `/memories/repo/protoskipper.md`.
This file contains the full project architecture, dependency graph, key design
patterns, module responsibilities, and the complete write-flow sequence.
Use it instead of re-exploring the codebase when you need to understand context.

## Project Snapshot

**ProtoSkipper** is an open-source SCADA/BMS protocol testing and commissioning
toolkit (GPL-3.0-or-later) by DataSailors Pvt Ltd.

### 4-Layer Architecture
```
GUI  (src/protoskipper/gui/)       — PySide6/Qt6, no protocol deps
CLI  (src/protoskipper/cli.py)     — thin argparse, headless
Core (src/protoskipper/core/)      — protocol-agnostic, no Qt deps
Drivers (builtin_drivers/ + pip)   — per-protocol implementations
```

### Non-Negotiable Rules
1. **Read/write split is load-bearing.** `prepare_write()` returns `WriteIntent` (no I/O).
   `commit_write()` transmits. Never fuse them.
2. **Core has zero Qt imports.** GUI has zero protocol-specific imports.
3. **Panels never call drivers directly.** All driver calls go through `SessionManager`.
4. **`DriverSession` lives exclusively on its `QThread`.**
   Cross-thread communication is via queued Qt signals/slots only.
5. **`GuiConfirmHandler` is the only place a non-UI thread blocks on UI input**
   (bounded by 300-second timeout → default deny).
6. **Plugins use the `protoskipper.protocols` entry-point group.**
   A bad plugin is logged and skipped; it must not crash the app.

### Key Modules (quick reference)
| Module | Role |
|--------|------|
| `core/driver.py` | ABCs `ProtocolDriver` / `DriverSession`; all shared dataclasses and enums |
| `core/session.py` | `open_session()` factory; glues driver + SafetyContext + AuditLog |
| `core/audit.py` | SQLite WAL, HMAC-chained rows, `verify_log()` |
| `core/plugin_loader.py` | `load_protocol_drivers()` (lru_cached), entry-point discovery |
| `core/errors.py` | Exception hierarchy rooted at `ProtoSkipperError` |
| `gui/services/app_state.py` | `ApplicationState` — single source of truth, Qt signals |
| `gui/services/session_manager.py` | Creates QThread + DriverWorker per session |
| `gui/services/worker.py` | `DriverWorker` — owns core `Session`, serves UI via slots |
| `builtin_drivers/modbus/driver.py` | Modbus TCP + RTU; lazy-imports pymodbus |

## Coding Standards

- Target Python 3.10+; use `from __future__ import annotations` in all new files.
- Type-annotate everything; run `mypy src/` to verify.
- Lint with `ruff check src/` before committing.
- Tests live in `tests/unit/` (pure) and `tests/integration/` (require pymodbus).
- Keep core free of optional dependencies — guard with `TYPE_CHECKING` or lazy imports.
- Follow existing docstring style (module-level docstrings explain purpose + design notes).

## Commit Instructions

**After every task is completed, commit the changes.**

```bash
# Stage relevant files (never use git add . blindly — be specific)
git add <changed files>

# Commit with a conventional commit message:
# format: <type>(<scope>): <short description>
# types: feat | fix | refactor | test | docs | chore | style
git commit -m "<type>(<scope>): <short description>"

# Examples:
# git commit -m "feat(modbus): add RTU multi-register read support"
# git commit -m "fix(audit): handle missing session_meta row on verify"
# git commit -m "test(session): add SafetyContext PRODUCTION profile coverage"
# git commit -m "docs(arch): clarify plugin discovery flow"
```

**Commit message rules:**
- Subject line ≤ 72 characters, imperative mood ("add", not "added").
- Scope = affected layer or module (e.g. `modbus`, `audit`, `gui`, `core`, `cli`).
- If a change touches multiple layers, use the highest-level scope.
- Never commit with `--no-verify`; keep pre-commit hooks intact.

## Development Workflow

```bash
# Full dev install
pip install -e ".[all,dev]"

# Run all tests
pytest

# Unit tests only (no pymodbus needed)
pytest tests/unit/

# Lint
ruff check src/

# Type check
mypy src/

# GUI entry point
protoskipper-gui

# CLI entry point
protoskipper --help
protoskipper list-protocols
protoskipper scan modbus.tcp 192.168.1.0/24
```
