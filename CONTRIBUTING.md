# Contributing to ProtoSkipper

Thank you for considering a contribution. ProtoSkipper exists because
substation and BMS engineers need a tool that nobody else is building, and
every patch — bug fix, new protocol driver, GUI polish, documentation —
makes the tool more useful to the field.

## Developer Certificate of Origin (DCO)

ProtoSkipper does **not** require contributors to sign a Contributor
License Agreement. We use the
[Developer Certificate of Origin](https://developercertificate.org/) (DCO)
instead. The DCO is a lightweight, well-understood declaration that you
have the right to contribute the code you are submitting.

By signing off on each commit you make, you certify that:

> 1. The contribution was created in whole or in part by you and you have
>    the right to submit it under the open source license indicated in the
>    file; or
> 2. The contribution is based upon previous work that, to the best of your
>    knowledge, is covered under an appropriate open source license and you
>    have the right under that license to submit that work with
>    modifications, whether created in whole or in part by you, under the
>    same open source license (unless you are permitted to submit under a
>    different license), as indicated in the file; or
> 3. The contribution was provided directly to you by some other person who
>    certified (1), (2) or (3) and you have not modified it.
> 4. You understand and agree that this project and the contribution are
>    public and that a record of the contribution (including all personal
>    information you submit with it, including your sign-off) is maintained
>    indefinitely and may be redistributed consistent with this project or
>    the open source license(s) involved.

Sign off on a commit by adding the `-s` / `--signoff` flag to `git commit`:

```bash
git commit -s -m "modbus: fix unit-id handling for non-default port"
```

This appends a line to your commit message:

```
Signed-off-by: Your Name <your.email@example.com>
```

CI will reject commits without a DCO sign-off.

We do **not** accept CLAs because no single party — including DataSailors —
should have the unilateral power to relicense the project. The DCO + GPL-3.0
combination keeps the project genuinely community-owned.

## Developer setup

```bash
# Full install with all extras and dev tools
pip install -e ".[all,dev]"

# Install pre-commit hooks (run once per clone)
pre-commit install

# Run all checks manually at any time
pre-commit run --all-files
```

## Pre-commit hooks

ProtoSkipper ships a `.pre-commit-config.yaml` that enforces the quality
bar on every commit automatically. The hook chain is:

1. **File hygiene** — end-of-file newlines, trailing whitespace, YAML/TOML
   validity, merge-conflict markers, debug statements.
2. **Ruff lint** — applies safe auto-fixes and checks the full rule set.
3. **Ruff format** — normalises whitespace and blank lines.
4. **Mypy** — strict type-checking on the core layer.
5. **pytest (unit)** — runs the fast unit suite (< 2 s) to catch
   regressions before the commit lands.

All hook versions are **exactly pinned**. Update them deliberately:

```bash
# Example: bump ruff to v0.X.Y
# 1. Edit the rev in .pre-commit-config.yaml AND the version in pyproject.toml
# 2. pre-commit run --all-files
# 3. pytest
# 4. git commit -m "chore(deps): bump ruff to 0.X.Y"
```

Emergency bypass (`--no-verify`) is **never permitted in CI** and should
be a last resort locally. If you need to bypass, explain why in the commit
message and open a follow-up issue.

## Code style

- Python 3.10+, type hints required on all public APIs.
- `ruff` for linting and import sorting.
- `mypy --strict` for the core package; protocol drivers are encouraged
  but not required to pass strict.
- Run `pre-commit run --all-files` before opening a PR (or rely on the
  git hook to do it automatically).

## Writing a new protocol driver

The shortest path to a new protocol driver is to copy
`src/protoskipper/builtin_drivers/modbus/` into a new package, implement
the abstract methods on `ProtocolDriver` and `DriverSession`, and register
the entry point in your `pyproject.toml`:

```toml
[project.entry-points."protoskipper.protocols"]
"my-protocol" = "my_package.driver:MyDriver"
```

After `pip install -e .`, the driver appears in the GUI on next launch.

The driver contract lives in `src/protoskipper/core/driver.py`. Key
responsibilities:

- `discover()` — yield `DeviceRef` objects for every device found on a
  network range or bus.
- `connect()` — return a `DriverSession` for an individual device.
- `enumerate_objects()` — list readable/writable points on a device.
- `read()` — fetch a value, return a `ReadResult` with quality metadata.
- `prepare_write()` — return a `WriteIntent` describing exactly what bytes
  would go on the wire. **This must not transmit anything.**
- `commit_write()` — actually send the write, gated by `SafetyContext`.

The `prepare_write` / `commit_write` split is what makes dry-run mode and
audit logging possible across all protocols uniformly. Drivers that ignore
this split will fail review.

## Reporting bugs and security issues

- Functional bugs: open an issue at
  [github.com/datasailors/protoskipper/issues](https://github.com/datasailors/protoskipper/issues).
- Security vulnerabilities: do **not** open a public issue. Email
  [security@datasailors.io](mailto:security@datasailors.io) with details
  and we will coordinate disclosure.

## Code of conduct

Be kind. Industrial control is a small, professional community; we want
ProtoSkipper to be a place where students, vendors, integrators, utility
engineers, and security researchers can collaborate without friction. The
maintainers reserve the right to remove contributions or block contributors
that don't meet that standard.
