# Reproducible Builds — P6.E

ProtoSkipper aims for **bit-for-bit reproducible** release artefacts.
This document describes what is guaranteed, what is best-effort, and
known current limitations.

## Approach

### 1. `SOURCE_DATE_EPOCH`

All packaging scripts export `SOURCE_DATE_EPOCH` (Unix timestamp of the last
git commit) before invoking build tools.  Tools that respect this variable —
including PyInstaller ≥ 6.3, `wheel`, `build`, and `appimagetool` — will
embed the fixed timestamp rather than the current wall-clock time.

```bash
# Set automatically in each script; override manually if needed:
export SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)
```

### 2. Pinned dependency graph

`pyproject.toml` defines version constraints.  The `requirements-build.lock`
file (generated with `pip-compile`) pins every transitive dependency at an
exact hash for the CI build environment, ensuring the same bytecode is
compiled across runs.

### 3. Frozen stdlib inclusion order

PyInstaller ≥ 6.3 sorts included files deterministically (alphabetically)
when `--contents-directory` is not randomised.  No extra flags are needed.

### 4. `.pyc` bytecode

Python `.pyc` files embed a source modification timestamp that can vary.
We disable `.pyc` embedding entirely via PyInstaller's `--exclude-module`
flags for known-pure modules, and rely on the `SOURCE_DATE_EPOCH` override
for those that must be included.

## Current Limitations

| Artefact | Status | Notes |
|----------|--------|-------|
| `wheel` (`.whl`) | ✅ Reproducible | `python -m build` + `SOURCE_DATE_EPOCH` |
| Linux AppImage | ⚠️ Best-effort | `appimagetool` ≥ 13 honours `SOURCE_DATE_EPOCH`; older versions do not |
| `.deb` / `.rpm` | ⚠️ Best-effort | `fpm` does not yet propagate `SOURCE_DATE_EPOCH` to archive timestamps |
| macOS DMG | ❌ Not reproducible | HFS+ volume UUID is randomised by `hdiutil`; `create-dmg` adds mtime noise |
| Windows installer | ❌ Not reproducible | Inno Setup embeds compiler timestamp; no supported workaround |

## Verification

To verify a wheel:

```bash
# Rebuild from the same tagged commit
git checkout v<VERSION>
export SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)
python -m build --wheel --outdir dist_repro/

# Compare SHA-256
sha256sum dist/protoskipper-<VERSION>-py3-none-any.whl
sha256sum dist_repro/protoskipper-<VERSION>-py3-none-any.whl
```

## Roadmap

- Investigate `diffoscope` integration in CI to surface non-reproducible bits.
- Replace `fpm` with `nfpm` (written in Go, better `SOURCE_DATE_EPOCH` support).
- Evaluate deterministic DMG builders (e.g., [dmgbuild](https://github.com/dmgbuild/dmgbuild)
  with `--defines SOURCE_DATE_EPOCH`).
