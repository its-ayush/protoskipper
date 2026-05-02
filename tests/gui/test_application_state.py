# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""GUI tests for ApplicationState.

Every public mutator is exercised to verify signal emission count, argument
contents, and state consistency. Edge cases (duplicates, unknown IDs, no-ops)
are explicitly covered.

100% line coverage of app_state.py is required by P0.B.3.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject

from protoskipper.core.driver import (
    DeviceRef,
    ObjectRef,
    Quality,
    ReadResult,
    SessionProfile,
    WriteIntent,
    WriteResult,
)
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.types import CapturedFrame, Direction, SessionId, new_session_id

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_device(address: str = "192.168.1.1") -> DeviceRef:
    return DeviceRef(protocol="modbus.tcp", address=address)


def _make_info(
    address: str = "192.168.1.1",
    profile: SessionProfile = SessionProfile.LAB,
) -> tuple[SessionId, SessionInfo]:
    sid = new_session_id()
    info = SessionInfo(
        session_id=sid,
        device=_make_device(address),
        profile=profile,
        operator="tester",
    )
    return sid, info


def _make_obj_ref(device: DeviceRef, obj_id: str = "holding:0") -> ObjectRef:
    return ObjectRef(device=device, object_id=obj_id, data_type="uint16")


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def state(qapp: QObject) -> ApplicationState:
    return ApplicationState()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeviceDiscovered:
    def test_emits_signal_on_first_discovery(self, qtbot: object, state: ApplicationState) -> None:
        device = _make_device()
        with qtbot.waitSignal(state.device_discovered, timeout=500):  # type: ignore[union-attr]
            state.record_device_discovered(device)

    def test_no_duplicate_signal_for_same_address(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        device = _make_device()
        state.record_device_discovered(device)
        signals: list[object] = []
        state.device_discovered.connect(signals.append)
        state.record_device_discovered(device)  # second call — must be silent
        assert signals == [], "Duplicate device must not re-emit device_discovered"

    def test_different_addresses_both_emit(self, qtbot: object, state: ApplicationState) -> None:
        signals: list[object] = []
        state.device_discovered.connect(signals.append)
        state.record_device_discovered(_make_device("10.0.0.1"))
        state.record_device_discovered(_make_device("10.0.0.2"))
        assert len(signals) == 2


class TestSessionLifecycle:
    def test_record_session_opened_adds_to_state(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        sid, info = _make_info()
        with qtbot.waitSignal(state.session_opened, timeout=500) as blocker:  # type: ignore[union-attr]
            state.record_session_opened(info)
        assert blocker.args[0] == sid
        stored = state.session(sid)
        assert stored is not None
        assert stored.is_open

    def test_record_session_closed_marks_closed_emits_signal(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        with qtbot.waitSignal(state.session_closed, timeout=500):  # type: ignore[union-attr]
            state.record_session_closed(sid)
        stored = state.session(sid)
        assert stored is not None
        assert not stored.is_open

    def test_record_session_closed_unknown_id_is_noop(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        signals: list[object] = []
        state.session_closed.connect(signals.append)
        state.record_session_closed(SessionId("no-such-id"))
        assert signals == [], "Unknown session_id must not emit session_closed"

    def test_record_session_failed_marks_closed_and_emits(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        with qtbot.waitSignal(state.session_failed, timeout=500) as blocker:  # type: ignore[union-attr]
            state.record_session_failed(sid, "timeout")
        assert blocker.args[0] == sid
        assert blocker.args[1] == "timeout"
        assert not state.session(sid).is_open  # type: ignore[union-attr]

    def test_record_session_failed_unknown_id_still_emits(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        """session_failed must emit even for unknown IDs (open never completed)."""
        sid = SessionId("unknown")
        with qtbot.waitSignal(state.session_failed, timeout=500):  # type: ignore[union-attr]
            state.record_session_failed(sid, "boom")


class TestObjectsAndValues:
    def test_record_objects_enumerated_updates_info(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        obj = _make_obj_ref(info.device)
        with qtbot.waitSignal(state.objects_enumerated, timeout=500):  # type: ignore[union-attr]
            state.record_objects_enumerated(sid, [obj])
        assert state.session(sid).objects == [obj]  # type: ignore[union-attr]

    def test_record_objects_enumerated_unknown_session_no_signal(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        signals: list[object] = []
        state.objects_enumerated.connect(signals.append)
        state.record_objects_enumerated(SessionId("gone"), [])
        assert signals == []

    def test_record_read_completed_updates_last_values(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        obj = _make_obj_ref(info.device, "holding:0")
        from datetime import datetime

        result = ReadResult(
            object_ref=obj,
            value=42,
            quality=Quality.GOOD,
            timestamp=datetime.now(),
        )
        with qtbot.waitSignal(state.read_completed, timeout=500):  # type: ignore[union-attr]
            state.record_read_completed(sid, result)
        vals = state.session(sid).last_values  # type: ignore[union-attr]
        assert vals["holding:0"] is result


class TestWriteFlows:
    def test_record_write_intent_prepared_emits(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        obj = _make_obj_ref(info.device)
        intent = WriteIntent(
            object_ref=obj,
            requested_value=1,
            encoded_bytes=b"\x00\x01",
            description="test write",
        )
        with qtbot.waitSignal(state.write_intent_prepared, timeout=500):  # type: ignore[union-attr]
            state.record_write_intent_prepared(sid, intent)

    def test_record_write_completed_emits(self, qtbot: object, state: ApplicationState) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        obj = _make_obj_ref(info.device)
        intent = WriteIntent(
            object_ref=obj,
            requested_value=1,
            encoded_bytes=b"\x00\x01",
            description="test write",
        )
        result = WriteResult(
            intent=intent, success=True, timestamp=__import__("datetime").datetime.now()
        )
        with qtbot.waitSignal(state.write_completed, timeout=500):  # type: ignore[union-attr]
            state.record_write_completed(sid, result)

    def test_record_write_denied_emits(self, qtbot: object, state: ApplicationState) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        obj = _make_obj_ref(info.device)
        intent = WriteIntent(
            object_ref=obj,
            requested_value=1,
            encoded_bytes=b"\x00\x01",
            description="test write",
        )
        with qtbot.waitSignal(state.write_denied, timeout=500):  # type: ignore[union-attr]
            state.record_write_denied(sid, intent)


class TestWatchlist:
    def test_add_to_watchlist_emits_changed(self, qtbot: object, state: ApplicationState) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        obj = _make_obj_ref(info.device)
        with qtbot.waitSignal(state.watchlist_changed, timeout=500):  # type: ignore[union-attr]
            result = state.add_to_watchlist(sid, obj)
        assert result is True
        assert (sid, obj) in state.watchlist()

    def test_add_duplicate_returns_false_no_signal(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        obj = _make_obj_ref(info.device)
        state.add_to_watchlist(sid, obj)
        signals: list[object] = []
        state.watchlist_changed.connect(signals.append)
        result = state.add_to_watchlist(sid, obj)
        assert result is False
        assert signals == [], "Duplicate add must not re-emit watchlist_changed"

    def test_remove_from_watchlist_emits_changed(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        obj = _make_obj_ref(info.device)
        state.add_to_watchlist(sid, obj)
        with qtbot.waitSignal(state.watchlist_changed, timeout=500):  # type: ignore[union-attr]
            result = state.remove_from_watchlist(sid, obj)
        assert result is True
        assert (sid, obj) not in state.watchlist()

    def test_remove_nonexistent_returns_false_no_signal(
        self, qtbot: object, state: ApplicationState
    ) -> None:
        sid, info = _make_info()
        state.record_session_opened(info)
        obj = _make_obj_ref(info.device)
        signals: list[object] = []
        state.watchlist_changed.connect(signals.append)
        result = state.remove_from_watchlist(sid, obj)
        assert result is False
        assert signals == []


class TestMiscSignals:
    def test_record_frame_captured_emits(self, qtbot: object, state: ApplicationState) -> None:
        from datetime import datetime

        sid = new_session_id()
        frame = CapturedFrame(
            session_id=sid,
            timestamp=datetime.now(),
            direction=Direction.TX,
            payload=b"\x01\x02",
        )
        with qtbot.waitSignal(state.frame_captured, timeout=500):  # type: ignore[union-attr]
            state.record_frame_captured(frame)

    def test_record_error_emits_error_raised(self, qtbot: object, state: ApplicationState) -> None:
        with qtbot.waitSignal(state.error_raised, timeout=500) as blocker:  # type: ignore[union-attr]
            state.record_error("read", "timeout")
        assert blocker.args == ["read", "timeout"]

    def test_sessions_accessor_returns_all(self, qtbot: object, state: ApplicationState) -> None:
        sid1, info1 = _make_info("10.0.0.1")
        sid2, info2 = _make_info("10.0.0.2")
        state.record_session_opened(info1)
        state.record_session_opened(info2)
        ids = [s.session_id for s in state.sessions()]
        assert sid1 in ids
        assert sid2 in ids

    def test_discovered_devices_returns_all(self, qtbot: object, state: ApplicationState) -> None:
        d1 = _make_device("10.0.0.1")
        d2 = _make_device("10.0.0.2")
        state.record_device_discovered(d1)
        state.record_device_discovered(d2)
        addrs = [d.address for d in state.discovered_devices()]
        assert "10.0.0.1" in addrs
        assert "10.0.0.2" in addrs
