# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the plugin-loader discovery logic.

All tests use fake EntryPoint-like objects injected via
``unittest.mock.patch`` so that no real ``pip install`` is required.
The LRU cache on ``load_protocol_drivers`` is cleared before every test
via ``reload()`` to guarantee test isolation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from protoskipper.core import plugin_loader
from protoskipper.core.driver import (
    DeviceRef,
    DriverSession,
    ProtocolDriver,
    SafetyContext,
)
from protoskipper.core.plugin_loader import load_protocol_drivers, reload

# ---------------------------------------------------------------------------
# Minimal driver stubs used as plugin targets
# ---------------------------------------------------------------------------


class _Alpha(ProtocolDriver):
    PROTOCOL_ID = "alpha"
    DISPLAY_NAME = "Alpha"

    def discover(self, target: str) -> Iterator[DeviceRef]:  # pragma: no cover
        return iter([])

    def connect(  # pragma: no cover
        self, device: DeviceRef, safety: SafetyContext
    ) -> DriverSession:
        raise NotImplementedError


class _Beta(ProtocolDriver):
    PROTOCOL_ID = "beta"
    DISPLAY_NAME = "Beta"

    def discover(self, target: str) -> Iterator[DeviceRef]:  # pragma: no cover
        return iter([])

    def connect(  # pragma: no cover
        self, device: DeviceRef, safety: SafetyContext
    ) -> DriverSession:
        raise NotImplementedError


class _BetaImposter(ProtocolDriver):
    """Claims the same PROTOCOL_ID as _Beta — duplicate-detection test."""

    PROTOCOL_ID = "beta"
    DISPLAY_NAME = "Beta Imposter"

    def discover(self, target: str) -> Iterator[DeviceRef]:  # pragma: no cover
        return iter([])

    def connect(  # pragma: no cover
        self, device: DeviceRef, safety: SafetyContext
    ) -> DriverSession:
        raise NotImplementedError


class _NoId(ProtocolDriver):
    """Missing PROTOCOL_ID — should be skipped by the loader."""

    DISPLAY_NAME = "NoId"

    def discover(self, target: str) -> Iterator[DeviceRef]:  # pragma: no cover
        return iter([])

    def connect(  # pragma: no cover
        self, device: DeviceRef, safety: SafetyContext
    ) -> DriverSession:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helper: build fake EntryPoint-like objects
# ---------------------------------------------------------------------------


def _ep(name: str, loader: Callable[[], Any], value: str = "fake:Cls") -> Any:
    """Return a SimpleNamespace that satisfies plugin_loader's EntryPoint interface."""
    obj = SimpleNamespace()
    obj.name = name
    obj.value = value
    obj.load = loader
    return obj


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear the LRU cache before and after every test."""
    load_protocol_drivers.cache_clear()
    yield
    load_protocol_drivers.cache_clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_loads_valid_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: a single valid driver is returned under its PROTOCOL_ID."""
    monkeypatch.setattr(
        plugin_loader,
        "_iter_entry_points",
        lambda _group: [_ep("alpha", lambda: _Alpha)],
    )

    drivers = load_protocol_drivers()

    assert "alpha" in drivers
    assert drivers["alpha"] is _Alpha


def test_loads_multiple_distinct_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple drivers with unique IDs are all registered."""
    monkeypatch.setattr(
        plugin_loader,
        "_iter_entry_points",
        lambda _group: [
            _ep("alpha", lambda: _Alpha),
            _ep("beta", lambda: _Beta),
        ],
    )

    drivers = load_protocol_drivers()

    assert set(drivers.keys()) == {"alpha", "beta"}


def test_skips_import_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plugin whose load() raises an ImportError is silently skipped; the
    error is logged and other plugins are unaffected."""

    def _boom() -> Any:
        raise ImportError("missing dep")

    monkeypatch.setattr(
        plugin_loader,
        "_iter_entry_points",
        lambda _group: [
            _ep("bad", _boom),
            _ep("alpha", lambda: _Alpha),
        ],
    )

    with caplog.at_level(logging.ERROR, logger="protoskipper.core.plugin_loader"):
        drivers = load_protocol_drivers()

    assert "bad" not in drivers
    assert "alpha" in drivers
    assert any("bad" in msg for msg in caplog.messages)


def test_skips_non_subclass(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plugin that resolves to something that is not a ProtocolDriver subclass
    is skipped with an error log."""

    class _NotADriver:
        pass

    monkeypatch.setattr(
        plugin_loader,
        "_iter_entry_points",
        lambda _group: [_ep("notadriver", lambda: _NotADriver)],
    )

    with caplog.at_level(logging.ERROR, logger="protoskipper.core.plugin_loader"):
        drivers = load_protocol_drivers()

    assert "notadriver" not in drivers
    assert any("ProtocolDriver subclass" in msg for msg in caplog.messages)


def test_skips_non_class(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A plugin that resolves to a non-class (e.g. a function or module) is skipped."""
    monkeypatch.setattr(
        plugin_loader,
        "_iter_entry_points",
        lambda _group: [_ep("fn", lambda: lambda: None)],
    )

    with caplog.at_level(logging.ERROR, logger="protoskipper.core.plugin_loader"):
        drivers = load_protocol_drivers()

    assert "fn" not in drivers


def test_skips_missing_protocol_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A driver class with no PROTOCOL_ID attribute (or a falsy one) is skipped."""
    monkeypatch.setattr(
        plugin_loader,
        "_iter_entry_points",
        lambda _group: [_ep("noid", lambda: _NoId)],
    )

    with caplog.at_level(logging.ERROR, logger="protoskipper.core.plugin_loader"):
        drivers = load_protocol_drivers()

    assert "noid" not in drivers
    assert any("PROTOCOL_ID" in msg for msg in caplog.messages)


def test_warns_on_name_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the entry-point name does not match the class's PROTOCOL_ID, a warning
    is logged but the driver is still registered under the class PROTOCOL_ID."""
    # ep.name="wrong" but _Alpha.PROTOCOL_ID="alpha"
    monkeypatch.setattr(
        plugin_loader,
        "_iter_entry_points",
        lambda _group: [_ep("wrong", lambda: _Alpha)],
    )

    with caplog.at_level(logging.WARNING, logger="protoskipper.core.plugin_loader"):
        drivers = load_protocol_drivers()

    assert "alpha" in drivers
    assert any("does not match" in msg for msg in caplog.messages)


def test_first_wins_on_duplicate_protocol_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When two plugins claim the same PROTOCOL_ID, the first one wins and a
    warning is emitted.  The second plugin's class must NOT overwrite the first."""
    monkeypatch.setattr(
        plugin_loader,
        "_iter_entry_points",
        lambda _group: [
            _ep("beta", lambda: _Beta),
            _ep("beta", lambda: _BetaImposter),
        ],
    )

    with caplog.at_level(logging.WARNING, logger="protoskipper.core.plugin_loader"):
        drivers = load_protocol_drivers()

    assert drivers["beta"] is _Beta  # first wins
    assert any("Using the first" in msg for msg in caplog.messages)


def test_reload_clears_cache_and_rescans(monkeypatch: pytest.MonkeyPatch) -> None:
    """reload() must drop the lru_cache result and return fresh data.

    This simulates a developer scenario where a plugin is pip-installed at
    runtime and the running process picks it up without a restart.
    """
    # First load: only alpha
    monkeypatch.setattr(
        plugin_loader,
        "_iter_entry_points",
        lambda _group: [_ep("alpha", lambda: _Alpha)],
    )
    drivers_before = load_protocol_drivers()
    assert set(drivers_before.keys()) == {"alpha"}

    # Now "install" beta by changing the patched function
    monkeypatch.setattr(
        plugin_loader,
        "_iter_entry_points",
        lambda _group: [
            _ep("alpha", lambda: _Alpha),
            _ep("beta", lambda: _Beta),
        ],
    )

    # Without reload the cache still holds the old result
    cached = load_protocol_drivers()
    assert set(cached.keys()) == {"alpha"}

    # After reload, the new plugin is visible
    drivers_after = reload()
    assert set(drivers_after.keys()) == {"alpha", "beta"}


def test_result_is_cached_on_second_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_protocol_drivers() called twice with no reload in between must
    return the identical dict object (lru_cache hit)."""
    call_count = 0

    def _counting_iter(_group: str):
        nonlocal call_count
        call_count += 1
        return [_ep("alpha", lambda: _Alpha)]

    monkeypatch.setattr(plugin_loader, "_iter_entry_points", _counting_iter)

    load_protocol_drivers()
    load_protocol_drivers()

    assert call_count == 1, "Expected exactly one scan; cache should prevent a second"


def test_iter_entry_points_finds_builtin_modbus_drivers() -> None:
    """_iter_entry_points exercises the real importlib.metadata.entry_points()
    path and must find the two built-in Modbus drivers that are declared in
    the project's own pyproject.toml entry-points.

    This test covers the _iter_entry_points function body (lines that would
    otherwise be unreachable because other tests patch the function whole).
    """
    from protoskipper.core.plugin_loader import PROTOCOL_GROUP, _iter_entry_points

    eps = _iter_entry_points(PROTOCOL_GROUP)
    assert isinstance(eps, list)
    names = {ep.name for ep in eps}
    # The two built-in drivers are registered via pyproject.toml entry-points;
    # they must be present in any development install (pip install -e .).
    assert "modbus.tcp" in names
    assert "modbus.rtu" in names
