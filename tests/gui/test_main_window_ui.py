# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Tests for P0.D GUI polish items in MainWindow.

P0.D.1 — Status bar shows live audit row count.
P0.D.2 — Toolbar profile chip reflects active session's profile with theme colour.
P0.D.3 — Disconnect action enables/disables based on session state.
P0.D.4 — MainWindow becomes visible and focused on macOS within 1 second.

All tests drive MainWindow through its ApplicationState signals rather than
clicking through dialogs, so no Modbus simulator is required here.  The full
end-to-end integration path is covered by test_main_window_smoke.py.
"""

from __future__ import annotations

import datetime
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QLabel

from protoskipper.core.driver import (
    Access,
    DeviceRef,
    ObjectRef,
    Quality,
    ReadResult,
    SessionProfile,
)
from protoskipper.gui.main_window import MainWindow
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.types import SessionId
from protoskipper.gui.theme import active_theme

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_window(qtbot: object, audit_dir: Path) -> MainWindow:
    """Create a MainWindow whose audit directory points to *audit_dir*."""
    with patch(
        "protoskipper.gui.main_window._default_audit_dir",
        return_value=audit_dir,
    ):
        window = MainWindow()
    qtbot.addWidget(window)  # type: ignore[union-attr]
    return window


def _make_device(address: str = "127.0.0.1:502") -> DeviceRef:
    return DeviceRef(protocol="modbus.tcp", address=address, label="test-dev")


def _make_session_info(
    session_id: str = "sess-1",
    address: str = "127.0.0.1:502",
    profile: SessionProfile = SessionProfile.LAB,
    is_open: bool = True,
) -> SessionInfo:
    return SessionInfo(
        session_id=SessionId(session_id),
        device=_make_device(address),
        profile=profile,
        operator="tester",
        is_open=is_open,
    )


@pytest.fixture()
def audit_dir() -> Path:  # type: ignore[return]
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture(autouse=True, scope="module")
def _mark_welcome_shown(qapp: object) -> Iterator[None]:
    """Ensure MainWindow._check_first_run returns early in tests.

    On CI (fresh environment) QSettings has no value for ``shown_welcome``
    so the one-time welcome dialog would call ``dlg.exec()`` and block.
    We set the flag in QSettings before the first window is created and
    restore the previous state afterwards.
    """
    from PySide6.QtCore import QSettings

    settings = QSettings("DataSailors", "ProtoSkipper")
    had_value = settings.contains("shown_welcome")
    original = settings.value("shown_welcome", False, bool)
    settings.setValue("shown_welcome", True)
    settings.sync()
    yield
    if had_value:
        settings.setValue("shown_welcome", original)
    else:
        settings.remove("shown_welcome")
    settings.sync()


# ---------------------------------------------------------------------------
# P0.D.1 — Audit row count in status bar
# ---------------------------------------------------------------------------


class TestAuditRowCount:
    def test_initial_label_shows_zero(self, qtbot: object, audit_dir: Path) -> None:
        window = _make_window(qtbot, audit_dir)
        label: QLabel = window._audit_row_label  # type: ignore[attr-defined]
        assert label.text() == "Audit: 0 rows"

    def test_label_increments_on_each_signal(self, qtbot: object, audit_dir: Path) -> None:
        window = _make_window(qtbot, audit_dir)
        state: ApplicationState = window._state  # type: ignore[attr-defined]

        for n in range(1, 4):
            state.audit_row_appended.emit()
            assert window._audit_row_label.text() == f"Audit: {n} rows"  # type: ignore[attr-defined]

    def test_read_does_not_increment(self, qtbot: object, audit_dir: Path) -> None:
        """Reads are not audited in v1; the counter must not move."""
        window = _make_window(qtbot, audit_dir)
        state: ApplicationState = window._state  # type: ignore[attr-defined]
        device = _make_device()
        info = _make_session_info()
        state.record_session_opened(info)

        # Simulate a read completion — must NOT touch audit_row_count.
        ref = ObjectRef(
            device=device,
            object_id="holding:0",
            data_type="uint16",
            access=Access.READ_ONLY,
        )
        result = ReadResult(
            object_ref=ref,
            value=42,
            quality=Quality.GOOD,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        state.record_read_completed(info.session_id, result)

        assert window._audit_row_count == 0  # type: ignore[attr-defined]
        assert window._audit_row_label.text() == "Audit: 0 rows"  # type: ignore[attr-defined]

    def test_write_increments_by_two(self, qtbot: object, audit_dir: Path) -> None:
        """write_authorization + write_committed = 2 audit rows per write."""
        window = _make_window(qtbot, audit_dir)
        state: ApplicationState = window._state  # type: ignore[attr-defined]

        # Simulate the worker emitting audit_row_written twice (authorize + commit).
        state.audit_row_appended.emit()
        state.audit_row_appended.emit()

        assert window._audit_row_count == 2  # type: ignore[attr-defined]
        assert window._audit_row_label.text() == "Audit: 2 rows"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# P0.D.2 — Toolbar profile chip
# ---------------------------------------------------------------------------


class TestProfileChip:
    def test_no_session_shows_default_text(self, qtbot: object, audit_dir: Path) -> None:
        window = _make_window(qtbot, audit_dir)
        chip: QLabel = window._profile_chip  # type: ignore[attr-defined]
        assert chip.text() == "No active session"

    def test_lab_profile_chip_text(self, qtbot: object, audit_dir: Path) -> None:
        window = _make_window(qtbot, audit_dir)
        window._update_profile_chip(SessionProfile.LAB)  # type: ignore[attr-defined]
        chip: QLabel = window._profile_chip  # type: ignore[attr-defined]
        assert chip.text() == "LAB"

    def test_commissioning_profile_chip_text(self, qtbot: object, audit_dir: Path) -> None:
        window = _make_window(qtbot, audit_dir)
        window._update_profile_chip(SessionProfile.COMMISSIONING)  # type: ignore[attr-defined]
        assert window._profile_chip.text() == "COMMISSIONING"  # type: ignore[attr-defined]

    def test_production_profile_chip_text(self, qtbot: object, audit_dir: Path) -> None:
        window = _make_window(qtbot, audit_dir)
        window._update_profile_chip(SessionProfile.PRODUCTION)  # type: ignore[attr-defined]
        assert window._profile_chip.text() == "PRODUCTION"  # type: ignore[attr-defined]

    def test_chip_colour_matches_theme(self, qtbot: object, audit_dir: Path) -> None:
        window = _make_window(qtbot, audit_dir)
        theme = active_theme()
        for profile in SessionProfile:
            window._update_profile_chip(profile)  # type: ignore[attr-defined]
            expected_colour = theme.profile_color(profile).name()
            # The stylesheet should contain the expected hex colour.
            style = window._profile_chip.styleSheet()  # type: ignore[attr-defined]
            assert expected_colour in style, (
                f"Profile {profile} chip stylesheet {style!r} "
                f"missing expected colour {expected_colour}"
            )

    def test_chip_resets_to_no_active_session_on_close(
        self, qtbot: object, audit_dir: Path
    ) -> None:
        window = _make_window(qtbot, audit_dir)
        state: ApplicationState = window._state  # type: ignore[attr-defined]
        info = _make_session_info()
        state.record_session_opened(info)
        # Simulate user selecting that session.
        window._on_session_selected(str(info.session_id))  # type: ignore[attr-defined]
        assert window._profile_chip.text() == "LAB"  # type: ignore[attr-defined]

        # Now close the session; chip should reset.
        state.record_session_closed(info.session_id)
        assert window._profile_chip.text() == "No active session"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# P0.D.3 — Disconnect button enablement
# ---------------------------------------------------------------------------


class TestDisconnectButton:
    def test_disconnect_disabled_before_any_session(self, qtbot: object, audit_dir: Path) -> None:
        window = _make_window(qtbot, audit_dir)
        assert not window._action_disconnect.isEnabled()  # type: ignore[attr-defined]

    def test_disconnect_enabled_after_session_selected(
        self, qtbot: object, audit_dir: Path
    ) -> None:
        window = _make_window(qtbot, audit_dir)
        state: ApplicationState = window._state  # type: ignore[attr-defined]
        info = _make_session_info(is_open=True)
        state.record_session_opened(info)

        window._on_session_selected(str(info.session_id))  # type: ignore[attr-defined]

        assert window._action_disconnect.isEnabled()  # type: ignore[attr-defined]

    def test_disconnect_disabled_after_session_closes(self, qtbot: object, audit_dir: Path) -> None:
        window = _make_window(qtbot, audit_dir)
        state: ApplicationState = window._state  # type: ignore[attr-defined]
        info = _make_session_info(is_open=True)
        state.record_session_opened(info)

        # Select the session → button enabled.
        window._on_session_selected(str(info.session_id))  # type: ignore[attr-defined]
        assert window._action_disconnect.isEnabled()  # type: ignore[attr-defined]

        # Close the session → button must automatically disable.
        state.record_session_closed(info.session_id)
        assert not window._action_disconnect.isEnabled()  # type: ignore[attr-defined]

    def test_disconnect_disabled_for_already_closed_session(
        self, qtbot: object, audit_dir: Path
    ) -> None:
        window = _make_window(qtbot, audit_dir)
        state: ApplicationState = window._state  # type: ignore[attr-defined]
        info = _make_session_info(is_open=False)  # already closed
        state.record_session_opened(info)
        info.is_open = False  # mark closed before selection

        window._on_session_selected(str(info.session_id))  # type: ignore[attr-defined]

        assert not window._action_disconnect.isEnabled()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# P0.D.4 — Window visible on macOS
# ---------------------------------------------------------------------------


def test_window_becomes_visible_within_1s(qtbot: object, audit_dir: Path) -> None:
    """MainWindow must become visible (raise_ + activateWindow already called
    in app.py; here we just confirm the widget itself shows within 1 second)."""
    window = _make_window(qtbot, audit_dir)
    window.show()
    with qtbot.waitExposed(window, timeout=1_000):  # type: ignore[union-attr]
        pass
    assert window.isVisible()
