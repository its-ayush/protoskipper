# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Tests for DeviceTreeModel.

Covers: empty-state (protocol roots), after-discovery, after-session-open,
after-objects-enumerated, after-session-closed.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QModelIndex, QObject, Qt

from protoskipper.core.driver import DeviceRef, ObjectRef, SessionProfile
from protoskipper.gui.models.device_tree_model import (
    KIND_PROTOCOL,
    KIND_SESSION,
    ROLE_NODE_KIND,
    ROLE_PAYLOAD,
    DeviceTreeModel,
)
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.types import SessionId, new_session_id

pytestmark = pytest.mark.gui


def _make_device(protocol: str = "modbus.tcp", address: str = "192.168.1.1") -> DeviceRef:
    return DeviceRef(protocol=protocol, address=address)


def _open_session(state: ApplicationState, device: DeviceRef) -> SessionId:
    sid = new_session_id()
    info = SessionInfo(
        session_id=sid,
        device=device,
        profile=SessionProfile.LAB,
        operator="tester",
    )
    state.record_session_opened(info)
    return sid


@pytest.fixture()
def state(qapp: QObject) -> ApplicationState:
    return ApplicationState()


def test_root_has_protocol_children(state: ApplicationState) -> None:
    """On construction the tree must have at least one protocol node (modbus.tcp)."""
    model = DeviceTreeModel(state)
    root_count = model.rowCount(QModelIndex())
    assert root_count >= 1, "Expected at least one protocol node"


def test_device_discovered_adds_node(state: ApplicationState) -> None:
    model = DeviceTreeModel(state)
    device = _make_device()
    state.record_device_discovered(device)
    # Find the protocol node whose key is "modbus.tcp"
    proto_idx = None
    for row in range(model.rowCount(QModelIndex())):
        idx = model.index(row, 0, QModelIndex())
        is_protocol = model.data(idx, ROLE_NODE_KIND) == KIND_PROTOCOL
        if is_protocol and model.data(idx, ROLE_PAYLOAD) == "modbus.tcp":
            proto_idx = idx
            break
    assert proto_idx is not None, "Could not find modbus.tcp protocol node"
    assert model.rowCount(proto_idx) >= 1


def test_session_opened_adds_session_node(state: ApplicationState) -> None:
    model = DeviceTreeModel(state)
    device = _make_device()
    _open_session(state, device)
    # Find the session node by kind role
    session_found = False
    for p_row in range(model.rowCount(QModelIndex())):
        p_idx = model.index(p_row, 0, QModelIndex())
        for c_row in range(model.rowCount(p_idx)):
            c_idx = model.index(c_row, 0, p_idx)
            if model.data(c_idx, ROLE_NODE_KIND) == KIND_SESSION:
                session_found = True
                break
    assert session_found, "Expected to find a session node in the tree"


def test_session_closed_updates_label(qtbot: object, state: ApplicationState) -> None:
    model = DeviceTreeModel(state)
    device = _make_device()
    sid = _open_session(state, device)
    with qtbot.waitSignal(model.dataChanged, timeout=500):  # type: ignore[union-attr]
        state.record_session_closed(sid)
    # Find session node and check label contains "closed"
    for p_row in range(model.rowCount(QModelIndex())):
        p_idx = model.index(p_row, 0, QModelIndex())
        for c_row in range(model.rowCount(p_idx)):
            c_idx = model.index(c_row, 0, p_idx)
            if model.data(c_idx, ROLE_NODE_KIND) == KIND_SESSION:
                label = model.data(c_idx, Qt.DisplayRole) or ""
                assert "closed" in label.lower(), f"Expected 'closed' in label, got {label!r}"


def test_objects_enumerated_adds_object_children(state: ApplicationState) -> None:
    model = DeviceTreeModel(state)
    device = _make_device()
    sid = _open_session(state, device)
    objs = [
        ObjectRef(device=device, object_id="holding:0", data_type="uint16"),
        ObjectRef(device=device, object_id="holding:1", data_type="uint16"),
    ]
    state.record_objects_enumerated(sid, objs)
    # Find the session node and check its children
    for p_row in range(model.rowCount(QModelIndex())):
        p_idx = model.index(p_row, 0, QModelIndex())
        for c_row in range(model.rowCount(p_idx)):
            c_idx = model.index(c_row, 0, p_idx)
            if model.data(c_idx, ROLE_NODE_KIND) == KIND_SESSION:
                assert model.rowCount(c_idx) == 2
                return
    pytest.fail("Session node not found in tree")
