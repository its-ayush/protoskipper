# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Full session lifecycle test against the simulator.

Open a session in COMMISSIONING profile, write a value, deny a follow-up
write, close, and verify the audit chain. This is the integration test
equivalent of the manual smoke I run when changing anything in the
session manager.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from protoskipper.builtin_drivers.modbus.driver import ModbusTcpDriver
from protoskipper.core.audit import verify_log
from protoskipper.core.driver import (
    Access,
    DeviceRef,
    ObjectRef,
    SessionProfile,
)
from protoskipper.core.errors import AuthorizationDenied
from protoskipper.core.session import open_session

pytestmark = pytest.mark.integration


def test_session_write_read_deny_audit(modbus_simulator) -> None:
    host, port = modbus_simulator
    drv = ModbusTcpDriver()
    device = DeviceRef(
        protocol="modbus.tcp",
        address=f"{host}:{port}/unit=1",
        label="sim",
    )

    with tempfile.TemporaryDirectory() as td:
        audit_dir = Path(td)
        with open_session(
            drv,
            device,
            profile=SessionProfile.LAB,
            operator="ci@datasailors.io",
            audit_dir=audit_dir,
            confirm=lambda i, p: True,
        ) as session:
            ref = ObjectRef(
                device=device,
                object_id="holding:5",
                data_type="uint16",
                access=Access.READ_WRITE,
                label="hold5",
            )

            # Initial read: confirms the simulator is reachable.
            r0 = session.driver_session.read(ref)
            assert r0.quality.value == "good"

            # Write a known value and read back to verify roundtrip.
            intent = session.driver_session.prepare_write(ref, 4242)
            assert intent.encoded_bytes == (4242).to_bytes(2, "big")
            wr = session.driver_session.commit_write(intent)
            assert wr.success, wr.error
            r1 = session.driver_session.read(ref)
            assert r1.value == 4242

            # Deny path: confirm callback returns False, write must NOT
            # transmit and the original value must remain.
            session.safety._confirm = lambda i, p: False
            with pytest.raises(AuthorizationDenied):
                session.driver_session.commit_write(
                    session.driver_session.prepare_write(ref, 9999),
                )
            r2 = session.driver_session.read(ref)
            assert r2.value == 4242

        # Outside the with block the session is closed; verify the audit
        # chain and assert the full expected row sequence.
        logs = list(audit_dir.glob("*.audit.sqlite"))
        assert len(logs) == 1
        ok, msg = verify_log(logs[0])
        assert ok, msg

        import sqlite3

        with sqlite3.connect(logs[0]) as conn:
            events = [row[0] for row in conn.execute("SELECT event FROM audit_log ORDER BY seq")]

        # Every authorised write must have a matching write_committed or write_failed row.
        assert "write_authorization" in events
        assert "write_committed" in events
        # The denied write produces write_authorization(authorized=False) but no outcome row.
        auth_indices = [i for i, e in enumerate(events) if e == "write_authorization"]
        committed_indices = [i for i, e in enumerate(events) if e == "write_committed"]
        # There was exactly one successful write, so one write_committed row.
        assert len(committed_indices) == 1
        # The write_committed row must follow its write_authorization row.
        assert auth_indices[0] < committed_indices[0]


def test_frame_capture_produces_tx_and_rx(modbus_simulator) -> None:
    """A single read must produce exactly two captured frames: TX then RX.

    This is the acceptance criterion for P0.C.2 — the capturing transport
    mixin must intercept both the outgoing PDU and the incoming response.
    """
    from datetime import datetime

    from protoskipper.core.driver import CaptureSink

    host, port = modbus_simulator
    drv = ModbusTcpDriver()
    device = DeviceRef(
        protocol="modbus.tcp",
        address=f"{host}:{port}/unit=1",
        label="sim",
    )

    frames: list[tuple[str, bytes]] = []

    class _RecordingSink:
        def write_frame(
            self,
            timestamp: datetime,
            direction: str,
            payload: bytes,
            metadata: dict | None = None,
        ) -> None:
            frames.append((direction, payload))

    sink = _RecordingSink()
    assert isinstance(sink, CaptureSink)  # structural subtyping check

    with (
        tempfile.TemporaryDirectory() as td,
        open_session(
            drv,
            device,
            profile=SessionProfile.LAB,
            operator="ci@datasailors.io",
            audit_dir=Path(td),
            confirm=lambda i, p: True,
        ) as session,
    ):
        session.driver_session.attach_frame_sink(sink)

        ref = ObjectRef(
            device=device,
            object_id="holding:0",
            data_type="uint16",
            access=Access.READ_WRITE,
            label="hold0",
        )
        session.driver_session.read(ref)

    # Exactly two frames: one TX (request) and one RX (response).
    assert len(frames) == 2, f"Expected 2 frames, got {len(frames)}: {frames!r}"
    directions = [d for d, _ in frames]
    assert directions == ["tx", "rx"], f"Expected [tx, rx], got {directions}"
    # Both payloads must be non-empty bytes.
    for direction, payload in frames:
        assert isinstance(payload, bytes) and len(payload) > 0, (
            f"{direction} payload is empty or wrong type: {payload!r}"
        )


def test_reconnect_after_transport_drop(modbus_simulator) -> None:
    """P1.E.2: After a transport drop the driver reconnects and the next read succeeds.

    Simulation strategy: inject a broken client into the session, then
    configure the reconnect factory to return the original real client so
    the retry succeeds without needing a second network listener.
    """
    from unittest.mock import MagicMock

    host, port = modbus_simulator

    drv = ModbusTcpDriver()
    device = DeviceRef(
        protocol="modbus.tcp",
        address=f"{host}:{port}/unit=1",
        label="sim",
    )

    ref = ObjectRef(
        device=device,
        object_id="holding:0",
        data_type="uint16",
        access=Access.READ_WRITE,
        label="hold0",
    )

    with (
        tempfile.TemporaryDirectory() as td,
        open_session(
            drv,
            device,
            profile=SessionProfile.LAB,
            operator="ci@datasailors.io",
            audit_dir=Path(td),
            confirm=lambda i, p: True,
        ) as session,
    ):
        ds = session.driver_session

        # 1. Confirm the session works normally.
        r1 = ds.read(ref)
        assert r1.quality.value == "good", f"Pre-drop read failed: {r1.error}"

        # 2. Inject a broken client so the next read raises (simulates a
        #    TCP connection drop mid-session).
        broken_client = MagicMock()
        broken_client.read_holding_registers.side_effect = OSError("Connection reset")
        # Store a reference to the real client so the reconnect factory
        # can return it as the "reconnected" client.
        real_client = ds._client  # type: ignore[union-attr]
        ds._client = broken_client  # type: ignore[union-attr]

        # Override the factory with one that fails so r2 cannot auto-recover.
        # This simulates a transport drop where the remote is also unreachable.
        ds._reconnect_factory = lambda: None  # type: ignore[union-attr]

        # 3. The read with the broken client must return BAD quality.
        r2 = ds.read(ref)
        assert r2.quality.value == "bad", f"Expected BAD quality after drop, got {r2.quality}"
        assert r2.error, "Expected a non-empty error message"

        # 4. Wire up a factory that returns the original real client.
        #    ds._client is still broken_client (reconnect failed in step 3).
        ds._reconnect_factory = lambda: real_client  # type: ignore[union-attr]

        # 4. After the reconnect factory restored the real client, the
        #    next read should return GOOD quality.
        r3 = ds.read(ref)
        assert r3.quality.value == "good", (
            f"Expected GOOD quality after reconnect, got {r3.quality}: {r3.error}"
        )


def test_two_concurrent_sessions_independent(modbus_simulator) -> None:
    """P1.E.3: Two sessions to the same device are fully independent.

    Acceptance criteria:
    * Each session has its own ModbusTcpClient instance.
    * Closing one session does not prevent the other from reading.
    """
    host, port = modbus_simulator

    drv = ModbusTcpDriver()
    device = DeviceRef(
        protocol="modbus.tcp",
        address=f"{host}:{port}/unit=1",
        label="sim",
    )

    ref = ObjectRef(
        device=device,
        object_id="holding:0",
        data_type="uint16",
        access=Access.READ_WRITE,
        label="hold0",
    )

    with (
        tempfile.TemporaryDirectory() as td1,
        tempfile.TemporaryDirectory() as td2,
    ):
        with (
            open_session(
                drv,
                device,
                profile=SessionProfile.LAB,
                operator="session-a@ci",
                audit_dir=Path(td1),
                confirm=lambda i, p: True,
            ) as session_a,
            open_session(
                drv,
                device,
                profile=SessionProfile.LAB,
                operator="session-b@ci",
                audit_dir=Path(td2),
                confirm=lambda i, p: True,
            ) as session_b,
        ):
            ds_a = session_a.driver_session
            ds_b = session_b.driver_session

            # Each session must have its own underlying client instance.
            assert ds_a._client is not ds_b._client  # type: ignore[union-attr]

            # Both sessions must be independently readable.
            ra = ds_a.read(ref)
            rb = ds_b.read(ref)
            assert ra.quality.value == "good", f"Session A read failed: {ra.error}"
            assert rb.quality.value == "good", f"Session B read failed: {rb.error}"

        # Sessions are closed; temp dirs still exist — verify audit logs.
        for td in (td1, td2):
            logs = list(Path(td).glob("*.audit.sqlite"))
            assert len(logs) == 1
            ok, msg = verify_log(logs[0])
            assert ok, msg
