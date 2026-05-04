# Writing a ProtoSkipper Protocol Plugin

This guide walks through adding a new protocol driver to ProtoSkipper as
an external `pip`-installable package. It assumes you have read
[`ARCHITECTURE.md`](ARCHITECTURE.md) and understand the read/write split
and the SafetyContext.

The shortest path from zero to "the protocol shows up in the GUI" is
about 100 lines of code. This guide covers it end-to-end with a fictional
``hello`` protocol.

## Project layout

```
protoskipper-hello/
├── pyproject.toml
├── README.md
├── LICENSE                 # GPL-compatible if you want users to mix it
│                           # with the GPLv3 ProtoSkipper core
└── src/
    └── protoskipper_hello/
        ├── __init__.py
        └── driver.py
```

## Minimal pyproject.toml

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "protoskipper-hello"
version = "0.1.0"
description = "A toy protocol plugin for ProtoSkipper"
requires-python = ">=3.10"
dependencies = [
    "protoskipper>=0.0.1",
]

[project.entry-points."protoskipper.protocols"]
"hello" = "protoskipper_hello.driver:HelloDriver"

[tool.setuptools.packages.find]
where = ["src"]
```

The entry-point key (`"hello"` here) is what the user sees in the
``protoskipper list-protocols`` output and in the GUI's protocol picker.
It MUST match the driver's ``PROTOCOL_ID`` class attribute.

## A complete driver

```python
# src/protoskipper_hello/driver.py
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, ClassVar

from protoskipper.core.driver import (
    Access,
    DeviceRef,
    DriverSession,
    ObjectRef,
    ProtocolDriver,
    Quality,
    ReadResult,
    SafetyContext,
    WriteIntent,
    WriteResult,
)
from protoskipper.core.errors import AuthorizationDenied


class HelloDriver(ProtocolDriver):
    PROTOCOL_ID: ClassVar[str] = "hello"
    DISPLAY_NAME: ClassVar[str] = "Hello Protocol"
    DESCRIPTION: ClassVar[str] = "A toy protocol with one greeting register."

    def discover(self, target: str) -> Iterator[DeviceRef]:
        # In a real driver this would probe `target`. For the toy we
        # always pretend to find one device per call.
        yield DeviceRef(
            protocol=self.PROTOCOL_ID,
            address=f"hello://{target}",
            label=f"Greeter at {target}",
        )

    def connect(self, device: DeviceRef, safety: SafetyContext) -> DriverSession:
        return _HelloSession(device, safety)


class _HelloSession(DriverSession):
    def __init__(self, device: DeviceRef, safety: SafetyContext) -> None:
        self.device = device
        self.safety = safety
        self._greeting = "hello"

    def enumerate_objects(self) -> Iterator[ObjectRef]:
        yield ObjectRef(
            device=self.device,
            object_id="greeting",
            data_type="string",
            access=Access.READ_WRITE,
            label="Greeting",
        )

    def read(self, ref: ObjectRef) -> ReadResult:
        return ReadResult(
            object_ref=ref,
            value=self._greeting,
            quality=Quality.GOOD,
            timestamp=datetime.now(timezone.utc),
        )

    def prepare_write(self, ref: ObjectRef, value: Any) -> WriteIntent:
        encoded = str(value).encode("utf-8")
        return WriteIntent(
            object_ref=ref,
            requested_value=value,
            encoded_bytes=encoded,
            description=f"Set greeting to {value!r}",
        )

    def commit_write(self, intent: WriteIntent) -> WriteResult:
        if not self.safety.require_write_authorization(intent):
            raise AuthorizationDenied("user denied")
        self._greeting = intent.encoded_bytes.decode("utf-8")
        return WriteResult(
            intent=intent, success=True,
            timestamp=datetime.now(timezone.utc),
        )

    def close(self) -> None:
        pass
```

## Installing and verifying

```bash
cd protoskipper-hello/
pip install -e .

# The driver should now appear:
protoskipper list-protocols
# PROTOCOL             DRIVER CLASS
# hello                protoskipper_hello.driver.HelloDriver
# modbus.tcp           protoskipper.builtin_drivers.modbus.driver.ModbusTcpDriver
```

Run the GUI and you should see the protocol in the device tree under
"Protocols".

## Required attributes and methods

A driver class MUST set:

| Attribute | Type | Notes |
| --- | --- | --- |
| `PROTOCOL_ID` | `str` | Must match the entry-point key. |
| `DISPLAY_NAME` | `str` | Short human label. |
| `DESCRIPTION` | `str` | Optional but encouraged. |

A driver class MUST implement:

| Method | Returns |
| --- | --- |
| `discover(target)` | `Iterator[DeviceRef]` |
| `connect(device, safety)` | `DriverSession` |

A `DriverSession` subclass MUST implement:

| Method | Returns |
| --- | --- |
| `enumerate_objects()` | `Iterator[ObjectRef]` |
| `read(ref)` | `ReadResult` |
| `prepare_write(ref, value)` | `WriteIntent` |
| `commit_write(intent)` | `WriteResult` |
| `close()` | `None` |

It MAY override:

| Method | Why |
| --- | --- |
| `read_many(refs)` | Batch reads when the protocol supports them. |

## Optional capabilities

To declare optional features, additionally inherit from one of the
capability protocols defined in `protoskipper.core.driver`:

```python
from protoskipper.core.driver import DriverSession, Subscriber, Capturer

class _MySession(DriverSession, Subscriber, Capturer):
    def subscribe(self, refs, callback): ...
    def unsubscribe(self, handle): ...
    def start_capture(self, sink): ...
    def stop_capture(self): ...
```

The GUI introspects these via `isinstance()` and enables corresponding UI
affordances only for sessions that declare them.

> **Note on GOOSE:** GOOSE publish/subscribe is a Layer 2 multicast service
> and is not modelled as a per-session capability. Plugins that implement
> IEC 61850 GOOSE should follow the pattern in
> `plugins-builtin/protoskipper-iec61850/src/protoskipper_iec61850/goose/`:
> a standalone `GooseSubscriberService` / `GoosePublisherService` pair with
> Qt wrappers registered through the `protoskipper.gui_contributions`
> entry-point group rather than through `DriverSession` mix-ins.

## Common mistakes

* **Calling `commit_write` without consulting `SafetyContext`.** This will
  fail review. Always call `self.safety.require_write_authorization`
  first and refuse to proceed on `False`.
* **Doing the encoding in `commit_write` instead of `prepare_write`.**
  The whole point of the split is to give the safety layer the real bytes
  before transmission. If encoding errors only surface in `commit_write`,
  the user has already authorised something the driver could not actually
  send.
* **Mismatched `PROTOCOL_ID` and entry-point key.** The plugin loader
  prefers the class attribute and warns on mismatch, but `pip uninstall`
  may then fail to remove the driver from the user's view.
* **Hard-failing on missing optional dependencies at import time.** Use
  lazy imports so the driver class loads even when its transport library
  is missing, and raise a clear `UnsupportedOperation` when the user
  actually tries to connect.

## Testing your plugin

The Modbus tests in the core repository are a useful template; the
shape that works well is:

* Pure-Python parsing tests (no I/O) for any address or data-type encoding.
* A fake or in-memory transport for behavioural tests.
* Optional integration tests against a simulator (deferred behind a
  `pytest -m integration` marker so they do not run in default CI).

If your plugin reaches enough usage to be considered for inclusion in the
ProtoSkipper core, the bar for tests is the same as for the built-in
drivers. Until then, ship what you can.

## Distribution

Plugins are ordinary Python packages. Publish to PyPI under whatever name
you like; a `protoskipper-` prefix is conventional but not required. We
maintain a community list of known-good plugins on the project's
documentation site once we have one.
