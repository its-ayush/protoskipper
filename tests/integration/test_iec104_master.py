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
