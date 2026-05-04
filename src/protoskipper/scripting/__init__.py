# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""ProtoSkipper scripting engine (P5.A.2 of EXECUTION_PLAN.md).

Provides the runtime environment for ``protoskipper run <script.py>``.

The script executes in a namespace that includes:

* ``iec104``   — module alias for the IEC 104 driver package (if installed)
* ``modbus``   — module alias for the Modbus driver package (if installed)
* ``log``      — a :class:`logging.Logger` named ``protoskipper.script``
* ``argv``     — ``sys.argv[2:]`` (arguments after the script path)

Safety
------
When a session is opened from a script the confirm handler always **denies**
write requests unless ``--allow-writes`` is passed on the command line.
This mirrors the PRODUCTION profile default-deny behaviour.
"""

from __future__ import annotations

import logging
import runpy
import sys
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_SCRIPT_LOGGER = logging.getLogger("protoskipper.script")


# ---------------------------------------------------------------------------
# Scripting namespace factories
# ---------------------------------------------------------------------------


def _make_iec104_ns(allow_writes: bool) -> Any:
    """Return the ``iec104`` namespace for scripts / REPL.

    When *allow_writes* is False the returned namespace's ``MasterSession``
    is pre-configured with ``allow_writes=False`` so scripts cannot send
    commands without explicit confirmation.  We do this by wrapping the class
    rather than monkey-patching the module, to keep things clean.
    """
    try:
        import types

        from protoskipper.builtin_drivers.iec104.scripting import (
            Fuzzer,
            MasterSession,
            PcapReader,
            SlaveServer,
        )

        if allow_writes:
            _MasterSession = MasterSession  # noqa: N806
            _SlaveServer = SlaveServer  # noqa: N806
        else:
            # Shadow the classes so that allow_writes defaults to False and
            # cannot be overridden without explicitly passing allow_writes=True.
            class _MasterSession(MasterSession):  # type: ignore[no-redef]
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    kwargs.setdefault("allow_writes", False)
                    super().__init__(*args, **kwargs)

            class _SlaveServer(SlaveServer):  # type: ignore[no-redef]
                def start(self) -> None:
                    raise PermissionError(
                        "SlaveServer.start() requires --allow-writes "
                        "(or allow_in_production=True for production environments)."
                    )

        ns = types.SimpleNamespace(
            MasterSession=_MasterSession,
            SlaveServer=_SlaveServer,
            PcapReader=PcapReader,
            Fuzzer=Fuzzer,
        )
        return ns
    except ImportError:
        return None


def _make_namespace(script_path: Path, extra_argv: list[str], allow_writes: bool) -> dict[str, Any]:
    """Build the global namespace injected into the user script."""
    ns: dict[str, Any] = {
        "__name__": "__main__",
        "__file__": str(script_path),
        "__doc__": None,
        "log": _SCRIPT_LOGGER,
        "argv": extra_argv,
        "allow_writes": allow_writes,
    }

    # Inject protocol-specific helpers as top-level names (best-effort).
    ns["iec104"] = _make_iec104_ns(allow_writes)

    try:
        from protoskipper.scripting.iec61850 import make_iec61850_ns

        ns["iec61850"] = make_iec61850_ns(allow_writes)
    except Exception:
        ns["iec61850"] = None

    try:
        modbus_mod = __import__("protoskipper.builtin_drivers.modbus", fromlist=[""])
        ns["modbus"] = modbus_mod
    except ImportError:
        ns["modbus"] = None

    # Expose load_protocol_drivers so scripts can enumerate drivers.
    from protoskipper.core.plugin_loader import load_protocol_drivers

    ns["load_protocol_drivers"] = load_protocol_drivers

    return ns


def run_script(
    script_path: Path,
    extra_argv: list[str],
    *,
    allow_writes: bool = False,
) -> int:
    """Execute *script_path* in the ProtoSkipper scripting namespace.

    Returns the exit code (0 on success, 1 on script exception, 2 on I/O error).
    """
    if not script_path.exists():
        print(f"protoskipper run: no such file: {script_path}", file=sys.stderr)
        return 2
    if not script_path.is_file():
        print(f"protoskipper run: not a file: {script_path}", file=sys.stderr)
        return 2

    ns = _make_namespace(script_path, extra_argv, allow_writes)

    # Temporarily prepend the script's directory to sys.path so relative
    # imports from the script work (same behaviour as `python script.py`).
    script_dir = str(script_path.parent.resolve())
    sys.path.insert(0, script_dir)
    try:
        runpy.run_path(str(script_path), init_globals=ns, run_name="__main__")
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0
    except Exception as exc:
        _logger.error("Script raised an unhandled exception: %s", exc, exc_info=True)
        print(f"protoskipper run: unhandled exception: {exc}", file=sys.stderr)
        return 1
    finally:
        sys.path.remove(script_dir)

    return 0
