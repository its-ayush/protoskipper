# Security Policy

## Supported versions

Only the latest commit on `master` is actively supported. We do not maintain
patch branches for old releases in the pre-1.0 phase.

| Version | Supported |
|---------|-----------|
| `master` (unreleased) | ✅ |
| 0.0.1 | ✅ (only if no fix in master exists) |

---

## Reporting a vulnerability

**Do not open a public GitHub issue for security bugs.** Public disclosure
before a fix is ready puts every user of the tool at risk.

### Private disclosure

Email **[security@datasailors.io](mailto:security@datasailors.io)** with:

1. A brief description of the vulnerability and the affected component.
2. Steps to reproduce (proof-of-concept code / screenshots welcome).
3. The potential impact you see (e.g. arbitrary code execution, log tampering,
   audit-chain bypass, credential exposure).
4. Your name / handle if you want credit in the release notes (optional).

We will acknowledge your report within **3 business days** and aim to publish
a fix within **30 days** for critical issues.

---

## Scope

ProtoSkipper is an *operator tool* that runs on the engineer's own machine and
connects to devices the operator controls. It is not a server or network
daemon. The most relevant security boundaries are:

| Area | Notes |
|------|-------|
| Audit log integrity | HMAC-chained SQLite WAL — tampering should be detectable via `verify_log()`. |
| Plugin loading | Third-party `protoskipper.protocols` entry-point packages run arbitrary Python at startup. Only install plugins you trust. |
| Write safety gates | Safety profiles and confirmation dialogs prevent accidental writes. Bypassing them requires deliberate UI interaction. |
| Network traffic | ProtoSkipper sends exactly what the protocol requires. It does not proxy, re-encrypt, or cache credentials. |

---

## Out of scope

- Vulnerabilities in optional dependencies (pymodbus, PySide6, pyserial, etc.)
  that are not exploitable via ProtoSkipper's own code paths.
- Issues requiring an attacker to already have write access to the machine
  running ProtoSkipper.
- Social-engineering attacks on operators.

---

## Disclosure policy

Once a fix is merged, we will:

1. Publish a new release or tag.
2. Update this file with the CVE / advisory reference if one is assigned.
3. Credit the reporter in `CHANGELOG.md` unless they prefer to remain
   anonymous.

We follow a **coordinated disclosure** model: reporters are asked not to
publish until 90 days after acknowledgement or after a fix ships, whichever
is sooner.
