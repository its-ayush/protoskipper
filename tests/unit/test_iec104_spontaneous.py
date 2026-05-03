# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for IEC 104 slave SpontaneousEventGenerator (P4.C.3)."""

from __future__ import annotations

import time

import pytest

from protoskipper.builtin_drivers.iec104.asdu import Quality, TypeID
from protoskipper.builtin_drivers.iec104.slave import (
    EventSpec,
    Iec104SlaveServer,
    SlaveConfig,
    SlavePoint,
    SpontaneousEventGenerator,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server_with_points() -> Iec104SlaveServer:
    """Create a slave server with two registered points (not started)."""
    srv = Iec104SlaveServer(SlaveConfig(port=0, ca=1))
    srv.add_point(SlavePoint(ioa=1001, type_id=TypeID.M_SP_NA_1, value=False, quality=Quality()))
    srv.add_point(SlavePoint(ioa=4001, type_id=TypeID.M_ME_NC_1, value=0.0, quality=Quality()))
    return srv


# ---------------------------------------------------------------------------
# EventSpec validation
# ---------------------------------------------------------------------------


def test_event_spec_invalid_interval_raises() -> None:
    srv = _make_server_with_points()
    gen = SpontaneousEventGenerator(srv)
    with pytest.raises(ValueError, match="interval must be > 0"):
        gen.add(EventSpec(ioa=1001, interval=0.0))


def test_event_spec_negative_interval_raises() -> None:
    srv = _make_server_with_points()
    gen = SpontaneousEventGenerator(srv)
    with pytest.raises(ValueError, match="interval must be > 0"):
        gen.add(EventSpec(ioa=1001, interval=-1.0))


# ---------------------------------------------------------------------------
# Generator lifecycle
# ---------------------------------------------------------------------------


def test_generator_start_stop_no_specs() -> None:
    """Starting and stopping a generator with no specs must not hang."""
    srv = _make_server_with_points()
    gen = SpontaneousEventGenerator(srv)
    gen.start()
    assert gen._running  # type: ignore[attr-defined]
    gen.stop()
    assert not gen._running  # type: ignore[attr-defined]


def test_generator_context_manager() -> None:
    srv = _make_server_with_points()
    with SpontaneousEventGenerator(srv) as gen:
        assert gen._running  # type: ignore[attr-defined]
    assert not gen._running  # type: ignore[attr-defined]


def test_generator_double_start_is_idempotent() -> None:
    srv = _make_server_with_points()
    gen = SpontaneousEventGenerator(srv)
    gen.start()
    thread1 = gen._thread  # type: ignore[attr-defined]
    gen.start()  # second start — should be no-op
    thread2 = gen._thread  # type: ignore[attr-defined]
    assert thread1 is thread2
    gen.stop()


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------


def test_generator_calls_server_update_for_boolean_toggle() -> None:
    """Generator with a toggle updater must call server.update() at least once."""
    srv = _make_server_with_points()

    updates: list[tuple[int, object]] = []

    original_update = srv.update

    def tracking_update(ioa: int, value: object, **kwargs: object) -> None:
        updates.append((ioa, value))
        original_update(ioa, value, **kwargs)  # type: ignore[arg-type]

    srv.update = tracking_update  # type: ignore[method-assign]

    gen = SpontaneousEventGenerator(srv)
    gen.add(EventSpec(ioa=1001, interval=0.02, updater=lambda v: not v))

    with gen:
        # Wait long enough for at least 2 emissions.
        deadline = time.monotonic() + 1.0
        while len(updates) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert len(updates) >= 2, f"Expected ≥2 updates, got {len(updates)}"
    assert all(ioa == 1001 for ioa, _ in updates)
    # Values must alternate True / False
    values = [v for _, v in updates]
    for i in range(len(values) - 1):
        assert values[i] != values[i + 1], "Values should toggle"


def test_generator_sawtooth_float_updater() -> None:
    """Sawtooth updater increments float; generator emits correct sequence."""
    srv = _make_server_with_points()

    emitted: list[float] = []

    original_update = srv.update

    def tracking_update(ioa: int, value: object, **kwargs: object) -> None:
        if ioa == 4001:
            emitted.append(float(value))  # type: ignore[arg-type]
        original_update(ioa, value, **kwargs)  # type: ignore[arg-type]

    srv.update = tracking_update  # type: ignore[method-assign]

    gen = SpontaneousEventGenerator(srv)
    gen.add(EventSpec(ioa=4001, interval=0.02, updater=lambda v: round((v + 0.1) % 1.0, 6)))

    with gen:
        deadline = time.monotonic() + 1.0
        while len(emitted) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert len(emitted) >= 3
    assert abs(emitted[0] - 0.1) < 1e-5
    assert abs(emitted[1] - 0.2) < 1e-5


def test_generator_no_updater_repeats_current_value() -> None:
    """When updater is None the same value should be emitted repeatedly."""
    srv = _make_server_with_points()

    emitted: list[object] = []
    original_update = srv.update

    def tracking_update(ioa: int, value: object, **kwargs: object) -> None:
        if ioa == 1001:
            emitted.append(value)
        original_update(ioa, value, **kwargs)  # type: ignore[arg-type]

    srv.update = tracking_update  # type: ignore[method-assign]

    gen = SpontaneousEventGenerator(srv)
    gen.add(EventSpec(ioa=1001, interval=0.02))  # no updater

    with gen:
        deadline = time.monotonic() + 1.0
        while len(emitted) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert len(emitted) >= 2
    assert all(v is False for v in emitted)


def test_generator_unregistered_ioa_is_skipped() -> None:
    """Spec for an IOA not in the server's point store must not crash."""
    srv = _make_server_with_points()

    gen = SpontaneousEventGenerator(srv)
    gen.add(EventSpec(ioa=9999, interval=0.02))  # not registered

    with gen:
        time.sleep(0.1)  # let a few ticks fire without crashing


# ---------------------------------------------------------------------------
# Remove / clear
# ---------------------------------------------------------------------------


def test_generator_remove_stops_emission() -> None:
    """After remove(), no more updates should be emitted for that IOA."""
    srv = _make_server_with_points()
    updates: list[int] = []

    original_update = srv.update

    def tracking_update(ioa: int, value: object, **kwargs: object) -> None:
        updates.append(ioa)
        original_update(ioa, value, **kwargs)  # type: ignore[arg-type]

    srv.update = tracking_update  # type: ignore[method-assign]

    gen = SpontaneousEventGenerator(srv)
    gen.add(EventSpec(ioa=1001, interval=0.02, updater=lambda v: not v))

    with gen:
        # Wait for at least one emission
        deadline = time.monotonic() + 1.0
        while not updates and time.monotonic() < deadline:
            time.sleep(0.01)
        gen.remove(1001)
        count_after_remove = len(updates)
        time.sleep(0.1)  # give it time to NOT emit more
        # Should not have emitted many more
        assert len(updates) <= count_after_remove + 2


def test_generator_clear_removes_all_specs() -> None:
    srv = _make_server_with_points()
    gen = SpontaneousEventGenerator(srv)
    gen.add(EventSpec(ioa=1001, interval=0.1))
    gen.add(EventSpec(ioa=4001, interval=0.1))
    assert len(gen._specs) == 2  # type: ignore[attr-defined]
    gen.clear()
    assert len(gen._specs) == 0  # type: ignore[attr-defined]
