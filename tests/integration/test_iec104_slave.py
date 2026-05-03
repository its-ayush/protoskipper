# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Integration test: IEC 104 slave server (P4.C) against the master."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from protoskipper.builtin_drivers.iec104.asdu import (
    COT,
    BinaryCounter,
    TypeID,
)
from protoskipper.builtin_drivers.iec104.master import (
    Iec104MasterSession,
    MasterConfig,
)
from protoskipper.builtin_drivers.iec104.slave import (
    Iec104SlaveServer,
    SlaveConfig,
    SlavePoint,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _populated_slave() -> Iec104SlaveServer:
    srv = Iec104SlaveServer(SlaveConfig(host="127.0.0.1", port=0, ca=1))
    srv.add_points(
        [
            SlavePoint(ioa=1001, type_id=TypeID.M_SP_NA_1, value=False),
            SlavePoint(ioa=1002, type_id=TypeID.M_SP_NA_1, value=True),
            SlavePoint(ioa=4001, type_id=TypeID.M_ME_NC_1, value=230.5),
            SlavePoint(ioa=4002, type_id=TypeID.M_ME_NC_1, value=49.99),
            SlavePoint(
                ioa=7001,
                type_id=TypeID.M_IT_NA_1,
                value=BinaryCounter(count=12345, sequence=1),
            ),
            SlavePoint(ioa=2001, type_id=TypeID.M_SP_NA_1, value=False),
        ]
    )
    return srv


@pytest.fixture
def slave() -> Iterator[Iec104SlaveServer]:
    srv = _populated_slave()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _master_for(slave: Iec104SlaveServer) -> Iec104MasterSession:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    return Iec104MasterSession(cfg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_slave_starts_and_assigns_ephemeral_port() -> None:
    srv = Iec104SlaveServer(SlaveConfig(host="127.0.0.1", port=0))
    srv.start()
    try:
        assert srv.port > 0
    finally:
        srv.stop()


def test_master_connect_and_startdt(slave: Iec104SlaveServer) -> None:
    m = _master_for(slave)
    try:
        m.connect()
        assert m.started is True
        # Wait briefly for slave to register the connection.
        time.sleep(0.1)
        assert slave.client_count >= 1
    finally:
        m.close()


def test_master_general_interrogation(slave: Iec104SlaveServer) -> None:
    m = _master_for(slave)
    try:
        m.connect()
        replies = m.general_interrogation(timeout=5.0)
        # ACTCON + monitor groups + ACTTERM.
        assert any(r.cot is COT.ACTCON and r.type_id is TypeID.C_IC_NA_1 for r in replies)
        assert any(r.cot is COT.ACTTERM and r.type_id is TypeID.C_IC_NA_1 for r in replies)
        # Find the float measurement IOA=4001.
        floats = [obj for r in replies if r.type_id is TypeID.M_ME_NC_1 for obj in r.objects]
        ioas = {obj.ioa: obj.value for obj in floats}
        assert ioas[4001] == pytest.approx(230.5)
        assert ioas[4002] == pytest.approx(49.99)
        # And the single-points.
        sps = [obj for r in replies if r.type_id is TypeID.M_SP_NA_1 for obj in r.objects]
        sp_ioas = {obj.ioa: obj.value for obj in sps}
        assert sp_ioas[1001] is False
        assert sp_ioas[1002] is True
    finally:
        m.close()


def test_master_read_returns_current_value(slave: Iec104SlaveServer) -> None:
    m = _master_for(slave)
    try:
        m.connect()
        reply = m.read(ioa=4001, timeout=5.0)
        assert reply.type_id is TypeID.M_ME_NC_1
        assert reply.objects[0].value == pytest.approx(230.5)
    finally:
        m.close()


def test_master_single_command_updates_point(slave: Iec104SlaveServer) -> None:
    m = _master_for(slave)
    try:
        m.connect()
        reply = m.single_command(ioa=2001, on=True, timeout=5.0)
        assert reply.cot is COT.ACTCON
        # Slave should have flipped the point's monitor value.
        time.sleep(0.05)
        assert slave.get_point(2001).value is True
    finally:
        m.close()


def test_master_set_point_float_updates_point(slave: Iec104SlaveServer) -> None:
    m = _master_for(slave)
    try:
        m.connect()
        reply = m.set_point_float(ioa=4001, value=400.0, timeout=5.0)
        assert reply.cot is COT.ACTCON
        time.sleep(0.05)
        assert slave.get_point(4001).value == pytest.approx(400.0)
    finally:
        m.close()


def test_master_sbo_select_does_not_apply_then_execute_does(
    slave: Iec104SlaveServer,
) -> None:
    m = _master_for(slave)
    try:
        m.connect()
        # Select phase.
        m.single_command(ioa=2001, on=True, select=True, timeout=5.0)
        time.sleep(0.05)
        # Point should NOT be updated by select.
        assert slave.get_point(2001).value is False
        # Execute phase.
        m.single_command(ioa=2001, on=True, select=False, timeout=5.0)
        time.sleep(0.05)
        assert slave.get_point(2001).value is True
    finally:
        m.close()


def test_command_handler_can_reject(slave: Iec104SlaveServer) -> None:
    rejected: list[int] = []

    def deny_2001(req):
        if req.objects and req.objects[0].ioa == 2001:
            rejected.append(req.objects[0].ioa)
            return False
        return None

    slave.set_command_handler(deny_2001)
    m = _master_for(slave)
    try:
        m.connect()
        reply = m.single_command(ioa=2001, on=True, timeout=5.0)
        # Negative ACTCON.
        assert reply.cot is COT.ACTCON
        assert reply.negative is True
        # Point must be unchanged.
        time.sleep(0.05)
        assert slave.get_point(2001).value is False
    finally:
        m.close()
    assert rejected == [2001]


def test_clock_sync_replies_actcon(slave: Iec104SlaveServer) -> None:
    m = _master_for(slave)
    try:
        m.connect()
        reply = m.clock_sync(timeout=5.0)
        assert reply.type_id is TypeID.C_CS_NA_1
        assert reply.cot is COT.ACTCON
    finally:
        m.close()


def test_counter_interrogation_returns_counters(slave: Iec104SlaveServer) -> None:
    m = _master_for(slave)
    try:
        m.connect()
        replies = m.counter_interrogation(timeout=5.0)
        # ACTCON, M_IT_NA_1, ACTTERM.
        assert any(r.cot is COT.ACTCON and r.type_id is TypeID.C_CI_NA_1 for r in replies)
        assert any(r.cot is COT.ACTTERM and r.type_id is TypeID.C_CI_NA_1 for r in replies)
        counters = [obj for r in replies if r.type_id is TypeID.M_IT_NA_1 for obj in r.objects]
        ioas = {obj.ioa: obj.value for obj in counters}
        assert 7001 in ioas
        bc = ioas[7001]
        assert bc.count == 12345
    finally:
        m.close()


def test_spontaneous_event_broadcast_to_master(slave: Iec104SlaveServer) -> None:
    received: list = []
    cond = threading.Event()

    def listener(asdu):
        # Filter to single-point spontaneous events on IOA 1001.
        if asdu.cot is COT.SPONT and any(o.ioa == 1001 for o in asdu.objects):
            received.append(asdu)
            cond.set()

    m = _master_for(slave)
    m.set_spontaneous_listener(listener)
    try:
        m.connect()
        # Wait briefly for slave to register the started link.
        time.sleep(0.1)
        # Update a point on the slave -> spontaneous broadcast.
        slave.update(1001, True)
        assert cond.wait(timeout=2.0), "spontaneous event not received"
        assert received[0].objects[0].value is True
    finally:
        m.close()


def test_slave_supports_two_concurrent_masters(slave: Iec104SlaveServer) -> None:
    m1 = _master_for(slave)
    m2 = _master_for(slave)
    try:
        m1.connect()
        m2.connect()
        time.sleep(0.1)
        assert slave.client_count == 2
        # Both can read independently.
        r1 = m1.read(ioa=4001, timeout=5.0)
        r2 = m2.read(ioa=4001, timeout=5.0)
        assert r1.objects[0].value == pytest.approx(230.5)
        assert r2.objects[0].value == pytest.approx(230.5)
    finally:
        m1.close()
        m2.close()


def test_unknown_ioa_read_returns_negative(slave: Iec104SlaveServer) -> None:
    m = _master_for(slave)
    try:
        m.connect()
        # 9999 is not in the point list.
        reply = m.read(ioa=9999, timeout=5.0)
        # Expect the slave to reply with UNKNOWN_IOA.
        assert reply.cot is COT.UNKNOWN_IOA
        assert reply.negative is True
    finally:
        m.close()
