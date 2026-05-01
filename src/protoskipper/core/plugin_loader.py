# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Discovers protocol drivers via the ``protoskipper.protocols`` entry-point group.

A third-party plugin package's ``pyproject.toml`` registers itself like::

    [project.entry-points."protoskipper.protocols"]
    "dnp3" = "protoskipper_dnp3.driver:DNP3Driver"

After ``pip install protoskipper-dnp3`` the driver becomes available with no
core code changes. Built-in drivers (Modbus, IEC 104, ...) ship via the
project's own pyproject.toml using the same mechanism, so they are not
privileged over community plugins.

Discovery is cached per-process; call :func:`reload` to pick up plugins
installed at runtime (developer scenarios, ``pip install -e .``).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from protoskipper.core.driver import ProtocolDriver

_logger = logging.getLogger(__name__)

PROTOCOL_GROUP = "protoskipper.protocols"
GUI_GROUP = "protoskipper.gui_contributions"


@lru_cache(maxsize=1)
def load_protocol_drivers() -> dict[str, type["ProtocolDriver"]]:
    """Return a dict of ``protocol_id -> driver class`` for every installed plugin.

    A plugin that fails to import is logged and skipped; one bad plugin must
    not prevent the rest of the application from starting. The error
    surfaces in the GUI's "Plugins" status panel so the operator knows.
    """
    from protoskipper.core.driver import ProtocolDriver  # local import to avoid cycle

    drivers: dict[str, type[ProtocolDriver]] = {}
    for ep in _iter_entry_points(PROTOCOL_GROUP):
        try:
            cls = ep.load()
        except Exception:  # pragma: no cover - defensive
            _logger.exception("Failed to load protocol plugin %r (%s)", ep.name, ep.value)
            continue

        if not isinstance(cls, type) or not issubclass(cls, ProtocolDriver):
            _logger.error(
                "Plugin %r at %s did not resolve to a ProtocolDriver subclass; skipping",
                ep.name,
                ep.value,
            )
            continue

        protocol_id = getattr(cls, "PROTOCOL_ID", None)
        if not protocol_id:
            _logger.error(
                "Plugin %r at %s is missing PROTOCOL_ID; skipping",
                ep.name,
                ep.value,
            )
            continue

        if protocol_id != ep.name:
            # Not fatal, but a strong smell — it means `pip uninstall` won't
            # cleanly remove the driver from the user's view.
            _logger.warning(
                "Plugin entry-point name %r does not match PROTOCOL_ID %r; "
                "preferring the class attribute.",
                ep.name,
                protocol_id,
            )

        if protocol_id in drivers:
            _logger.warning(
                "Two plugins claim PROTOCOL_ID %r: %s and %s. Using the first.",
                protocol_id,
                drivers[protocol_id].__module__,
                cls.__module__,
            )
            continue

        drivers[protocol_id] = cls

    return drivers


def reload() -> dict[str, type["ProtocolDriver"]]:
    """Drop the cache and re-scan entry points. For developer use."""
    load_protocol_drivers.cache_clear()
    return load_protocol_drivers()


def _iter_entry_points(group: str) -> list[EntryPoint]:
    """Wrapper around :func:`importlib.metadata.entry_points` that returns a
    list (so callers can len() it for diagnostics) and works across the
    Python 3.10/3.12 API differences."""
    eps = entry_points()
    if hasattr(eps, "select"):
        return list(eps.select(group=group))
    # Python 3.9 fallback (we don't officially support but keep cheap)
    return list(eps.get(group, []))  # type: ignore[union-attr]
