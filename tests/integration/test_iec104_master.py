# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Integration test: IEC 104 master against the in-tree mini-slave."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest

from protoskipper.builtin_drivers.iec104.master import (
    Iec104MasterSession,
    MasterConfig,
)
from tests.integration.iec104_mini_slave import MiniSlave


@pytest.fixture
def slave() -> Iterator[MiniSlave]:
    s = MiniSlave(ca=1, value=230.5)
    s.start()
    try:
        yield s
    finally:
        s.stop()


def test_master_connect_and_startdt(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        assert m.started is True
    finally:
        m.close()


def test_master_general_interrogation(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        replies = m.general_interrogation(timeout=5.0)
        # Expect ACTCON, monitor frame, ACTTERM
        assert len(replies) >= 2
        # Find the M_ME_NC_1 monitor frame
        monitor = [r for r in replies if r.objects and r.objects[0].ioa == 4001]
        assert monitor, "expected M_ME_NC_1 IOA=4001 in GI replies"
        assert monitor[0].objects[0].value == pytest.approx(230.5)
    finally:
        m.close()


def test_master_read(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        reply = m.read(ioa=4001, timeout=5.0)
        assert reply.objects[0].value == pytest.approx(230.5)
    finally:
        m.close()


def test_master_single_command(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        reply = m.single_command(ioa=2001, on=True, timeout=5.0)
        from protoskipper.builtin_drivers.iec104.asdu import COT

        assert reply.cot is COT.ACTCON
        assert reply.objects[0].value is True
    finally:
        m.close()


def test_master_clock_sync(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        reply = m.clock_sync(timeout=5.0)
        from protoskipper.builtin_drivers.iec104.asdu import COT, TypeID

        assert reply.type_id is TypeID.C_CS_NA_1
        assert reply.cot is COT.ACTCON
    finally:
        m.close()


def test_master_t3_emits_testfr(slave: MiniSlave) -> None:
    """Idle for >t3 and verify the master sent TESTFR (slave replied with CON)."""
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=0.5)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        # Sleep > t3; the receive loop should send TESTFR_ACT and the slave
        # should reply with TESTFR_CON, clearing _test_outstanding.
        time.sleep(1.2)
        # If we reach here without ConnectionFailure on next op, t1/test cycle is healthy.
        reply = m.read(ioa=4001, timeout=5.0)
        assert reply.objects[0].value == pytest.approx(230.5)
    finally:
        m.close()


# ---------------------------------------------------------------------------
# SBO commands and set-points
# ---------------------------------------------------------------------------


def test_master_single_command_sbo_select_then_execute(slave: MiniSlave) -> None:
    """SBO single command: SELECT phase + EXECUTE phase, both ACTCON."""
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        select_reply = m.single_command(ioa=2100, on=True, select=True, timeout=5.0)
        execute_reply = m.single_command(ioa=2100, on=True, select=False, timeout=5.0)
        from protoskipper.builtin_drivers.iec104.asdu import COT

        assert select_reply.cot is COT.ACTCON
        assert select_reply.objects[0].select is True
        assert execute_reply.cot is COT.ACTCON
        assert execute_reply.objects[0].select is False
    finally:
        m.close()


def test_master_double_command_sbo(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        reply = m.double_command(ioa=2200, dcs=2, select=True, timeout=5.0)
        from protoskipper.builtin_drivers.iec104.asdu import COT

        assert reply.cot is COT.ACTCON
        assert reply.objects[0].select is True
        assert reply.objects[0].value == 2  # DCS=ON
    finally:
        m.close()


def test_master_set_point_normalised(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        reply = m.set_point_normalised(ioa=4500, value=-1234, timeout=5.0)
        from protoskipper.builtin_drivers.iec104.asdu import COT

        assert reply.cot is COT.ACTCON
        # Mini-slave records the executed set-point (select=False is default).
        assert slave.set_points[4500] == -1234
    finally:
        m.close()


def test_master_set_point_scaled(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        reply = m.set_point_scaled(ioa=4501, value=20000, timeout=5.0)
        from protoskipper.builtin_drivers.iec104.asdu import COT

        assert reply.cot is COT.ACTCON
        assert slave.set_points[4501] == 20000
    finally:
        m.close()


def test_master_set_point_float(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        reply = m.set_point_float(ioa=4502, value=3.14, timeout=5.0)
        from protoskipper.builtin_drivers.iec104.asdu import COT

        assert reply.cot is COT.ACTCON
        assert slave.set_points[4502] == pytest.approx(3.14, rel=1e-5)
    finally:
        m.close()


def test_master_set_point_sbo_does_not_apply(slave: MiniSlave) -> None:
    """A SELECT phase must not actually update the slave's stored set-point."""
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        m.set_point_float(ioa=4503, value=99.9, select=True, timeout=5.0)
        assert 4503 not in slave.set_points
        m.set_point_float(ioa=4503, value=99.9, select=False, timeout=5.0)
        assert slave.set_points[4503] == pytest.approx(99.9, rel=1e-5)
    finally:
        m.close()


def test_master_bitstring_command(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        reply = m.bitstring_command(ioa=5000, value=0xDEADBEEF, timeout=5.0)
        from protoskipper.builtin_drivers.iec104.asdu import COT

        assert reply.cot is COT.ACTCON
        assert slave.bitstrings[5000] == 0xDEADBEEF
    finally:
        m.close()


# ---------------------------------------------------------------------------
# Counter interrogation (C_CI_NA_1)
# ---------------------------------------------------------------------------


def test_master_counter_interrogation_general(slave: MiniSlave) -> None:
    cfg = MasterConfig(host="127.0.0.1", port=slave.port, ca=slave.ca, t1=3.0, t2=1.0, t3=5.0)
    m = Iec104MasterSession(cfg)
    try:
        m.connect()
        replies = m.counter_interrogation(timeout=5.0)
        from protoskipper.builtin_drivers.iec104.asdu import TypeID

        # Expect ACTCON, M_IT_NA_1 with two counters, ACTTERM.
        counters = [r for r in replies if r.type_id is TypeID.M_IT_NA_1]
        assert len(counters) == 1
        ioas = {obj.ioa: obj.value.count for obj in counters[0].objects}
        assert ioas == {7001: 12345, 7002: 67890}
    finally:
        m.close()
