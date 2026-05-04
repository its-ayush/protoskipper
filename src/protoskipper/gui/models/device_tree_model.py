# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""DeviceTreeModel - a QAbstractItemModel for the left-pane device tree.

Hierarchy::

    Protocols (root)
    ├── Modbus TCP
    │   ├── 10.0.0.5 [discovered]
    │   └── 10.0.0.5/unit=1 [SESSION sid] [LAB]
    │       ├── holding:0 (uint16, RW)
    │       ├── holding:1 (uint16, RW)
    │       └── ...
    ├── BACnet/IP
    │   └── (no devices yet)
    └── ...

Items expose a UserRole carrying the underlying object (a string protocol
id, a DeviceRef, a SessionInfo, or an ObjectRef) so panels can react to
selection without re-inferring the type from the display string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtGui import QFont

from protoskipper.core.driver import DeviceRef, ObjectRef, SessionProfile
from protoskipper.core.plugin_loader import load_protocol_drivers
from protoskipper.gui.services.app_state import ApplicationState, SessionInfo
from protoskipper.gui.services.types import SessionId
from protoskipper.gui.theme import active_theme

# Custom roles - keep above Qt::UserRole.
ROLE_NODE_KIND = Qt.UserRole + 1
ROLE_PAYLOAD = Qt.UserRole + 2

KIND_PROTOCOL = "protocol"
KIND_DEVICE = "device"
KIND_SESSION = "session"
KIND_OBJECT = "object"
# IEC 61850: intermediate group nodes (LD, LN, DO) in the device tree.
# Payload is an IecGroupInfo; no individual leaf tags are inserted.
KIND_IEC_GROUP = "iec_group"


@dataclass
class IecGroupInfo:
    """Payload for KIND_IEC_GROUP tree nodes.

    Carries enough context for the IEC 61850 Browser panel to filter its
    table when the user clicks on a group node.
    """

    session_id: str
    ld_inst: str = ""
    ln_ref: str = ""
    do_name: str = ""

    @property
    def level(self) -> str:  # "ld" | "ln" | "do"
        if self.do_name:
            return "do"
        if self.ln_ref:
            return "ln"
        return "ld"


@dataclass
class _Node:
    kind: str
    payload: Any
    label: str
    parent: _Node | None = None
    children: list[_Node] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = []

    def row(self) -> int:
        if self.parent is None:
            return 0
        return self.parent.children.index(self)


class DeviceTreeModel(QAbstractItemModel):
    """Reactive tree model wired to ApplicationState signals."""

    def __init__(self, state: ApplicationState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self._root = _Node(kind="root", payload=None, label="root")
        self._protocol_nodes: dict[str, _Node] = {}
        self._device_nodes: dict[str, _Node] = {}  # key = "protocol|address"
        self._session_nodes: dict[SessionId, _Node] = {}

        self._populate_protocols()
        self._connect_signals()

    # ---- population -------------------------------------------------------

    def _populate_protocols(self) -> None:
        """Add a child of root for every loaded protocol driver."""
        for protocol_id, cls in sorted(load_protocol_drivers().items()):
            label = getattr(cls, "DISPLAY_NAME", protocol_id)
            node = _Node(kind=KIND_PROTOCOL, payload=protocol_id, label=label, parent=self._root)
            self._root.children.append(node)
            self._protocol_nodes[protocol_id] = node

    def _connect_signals(self) -> None:
        s = self._state
        s.device_discovered.connect(self._on_device_discovered)
        s.session_opened.connect(self._on_session_opened)
        s.session_closed.connect(self._on_session_closed)
        s.objects_enumerated.connect(self._on_objects_enumerated)
        s.iec_tags_loaded.connect(self._on_iec_tags_loaded)

    # ---- signal handlers --------------------------------------------------

    def _on_device_discovered(self, device: DeviceRef) -> None:
        proto_node = self._protocol_nodes.get(device.protocol)
        if proto_node is None:
            return  # unknown protocol, ignore
        key = f"{device.protocol}|{device.address}"
        if key in self._device_nodes:
            return

        parent_index = self._index_of(proto_node)
        row = len(proto_node.children)
        self.beginInsertRows(parent_index, row, row)
        node = _Node(kind=KIND_DEVICE, payload=device, label=str(device), parent=proto_node)
        proto_node.children.append(node)
        self._device_nodes[key] = node
        self.endInsertRows()

    def _on_session_opened(
        self,
        session_id: str,
        device: DeviceRef,
        profile: SessionProfile,
    ) -> None:
        proto_node = self._protocol_nodes.get(device.protocol)
        if proto_node is None:
            return

        info = self._state.session(SessionId(session_id))
        if info is None:
            return

        parent_index = self._index_of(proto_node)
        row = len(proto_node.children)
        self.beginInsertRows(parent_index, row, row)
        node = _Node(
            kind=KIND_SESSION,
            payload=info,
            label=f"{device.label or device.address}  [{profile.value.upper()}]",
            parent=proto_node,
        )
        proto_node.children.append(node)
        self._session_nodes[SessionId(session_id)] = node
        self.endInsertRows()

    def _on_session_closed(self, session_id: str) -> None:
        node = self._session_nodes.get(SessionId(session_id))
        if node is None or node.parent is None:
            return
        node.label = node.label + "  (closed)"
        idx = self._index_of(node)
        self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.FontRole])

    def _on_objects_enumerated(self, session_id: str, objects: list[ObjectRef]) -> None:
        # IEC 61850 objects are handled separately via _on_iec_tags_loaded;
        # skip them here so they are not dumped as a flat list.
        info = self._state.session(SessionId(session_id))
        if info and info.device.protocol.startswith("iec61850"):
            return

        node = self._session_nodes.get(SessionId(session_id))
        if node is None:
            return
        # Replace children with the enumerated objects.
        parent_index = self._index_of(node)
        if node.children:
            self.beginRemoveRows(parent_index, 0, len(node.children) - 1)
            node.children.clear()
            self.endRemoveRows()
        if objects:
            self.beginInsertRows(parent_index, 0, len(objects) - 1)
            for obj in objects:
                child = _Node(
                    kind=KIND_OBJECT,
                    payload=obj,
                    label=f"{obj.label or obj.object_id}  ({obj.data_type})",
                    parent=node,
                )
                node.children.append(child)
            self.endInsertRows()

    def _on_iec_tags_loaded(self, session_id: str, objects: list[ObjectRef]) -> None:
        """Insert LD→LN→DO group nodes for an IEC 61850 session."""
        node = self._session_nodes.get(SessionId(session_id))
        if node is None:
            return
        parent_index = self._index_of(node)
        if node.children:
            self.beginRemoveRows(parent_index, 0, len(node.children) - 1)
            node.children.clear()
            self.endRemoveRows()
        if not objects:
            return

        # Build LD→LN→DO hierarchy from metadata.
        # Use ordered dicts to preserve SCL order.
        from collections import OrderedDict

        ld_map: dict[str, dict[str, set[str]]] = OrderedDict()
        for obj in objects:
            m = obj.metadata
            ld = str(m.get("ld_inst", ""))
            ln = str(m.get("ln_ref", ""))
            do = str(m.get("do_name", ""))
            if ld not in ld_map:
                ld_map[ld] = OrderedDict()
            if ln not in ld_map[ld]:
                ld_map[ld][ln] = set()
            if do:
                ld_map[ld][ln].add(do)

        ld_nodes_to_insert = len(ld_map)
        if ld_nodes_to_insert == 0:
            return
        self.beginInsertRows(parent_index, 0, ld_nodes_to_insert - 1)
        for ld_name, ln_map in ld_map.items():
            ld_group = IecGroupInfo(session_id=session_id, ld_inst=ld_name)
            ld_node = _Node(
                kind=KIND_IEC_GROUP,
                payload=ld_group,
                label=ld_name,
                parent=node,
            )
            node.children.append(ld_node)
            for ln_name, dos in ln_map.items():
                ln_group = IecGroupInfo(session_id=session_id, ld_inst=ld_name, ln_ref=ln_name)
                ln_node = _Node(
                    kind=KIND_IEC_GROUP,
                    payload=ln_group,
                    label=ln_name,
                    parent=ld_node,
                )
                ld_node.children.append(ln_node)
                for do_name in sorted(dos):
                    do_group = IecGroupInfo(
                        session_id=session_id,
                        ld_inst=ld_name,
                        ln_ref=ln_name,
                        do_name=do_name,
                    )
                    do_node = _Node(
                        kind=KIND_IEC_GROUP,
                        payload=do_group,
                        label=do_name,
                        parent=ln_node,
                    )
                    ln_node.children.append(do_node)
        self.endInsertRows()

    # ---- QAbstractItemModel API ------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        node = parent.internalPointer() if parent.isValid() else self._root
        return len(node.children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 1

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if column != 0:
            return QModelIndex()
        parent_node = parent.internalPointer() if parent.isValid() else self._root
        if row < 0 or row >= len(parent_node.children):
            return QModelIndex()
        return self.createIndex(row, column, parent_node.children[row])

    def parent(self, index: QModelIndex) -> QModelIndex:  # type: ignore[override]
        if not index.isValid():
            return QModelIndex()
        node: _Node = index.internalPointer()
        if node.parent is None or node.parent is self._root:
            return QModelIndex()
        return self.createIndex(node.parent.row(), 0, node.parent)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        node: _Node = index.internalPointer()

        if role == Qt.DisplayRole:
            return node.label
        if role == ROLE_NODE_KIND:
            return node.kind
        if role == ROLE_PAYLOAD:
            return node.payload
        if role == Qt.FontRole:
            font = QFont()
            if node.kind == KIND_PROTOCOL:
                font.setBold(True)
            elif node.kind == KIND_SESSION:
                font.setItalic(False)
                if isinstance(node.payload, SessionInfo) and not node.payload.is_open:
                    font.setStrikeOut(True)
            return font
        if role == Qt.ForegroundRole and node.kind == KIND_SESSION:
            info = node.payload
            if isinstance(info, SessionInfo):
                return active_theme().profile_color(info.profile)
        if role == Qt.ToolTipRole:
            if node.kind == KIND_PROTOCOL:
                cls = load_protocol_drivers().get(node.payload)
                return getattr(cls, "DESCRIPTION", "") if cls else ""
            if node.kind == KIND_DEVICE:
                return f"Discovered: {node.payload}"
            if node.kind == KIND_SESSION:
                info = node.payload
                if isinstance(info, SessionInfo):
                    return (
                        f"Session {info.session_id}\n"
                        f"Profile: {info.profile.value}\n"
                        f"Operator: {info.operator}"
                    )
            if node.kind == KIND_OBJECT:
                obj = node.payload
                return (
                    f"{obj.object_id}\n"
                    f"Type: {obj.data_type}, Access: {obj.access.value}\n"
                    f"Unit: {obj.unit or '(none)'}"
                )
            if node.kind == KIND_IEC_GROUP:
                g = node.payload
                if g.do_name:
                    return f"DO: {g.ld_inst}/{g.ln_ref}.{g.do_name}"
                if g.ln_ref:
                    return f"Logical Node: {g.ld_inst}/{g.ln_ref}"
                return f"Logical Device: {g.ld_inst}"
        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole
    ) -> Any:
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and section == 0:
            return "Devices"
        return None

    # ---- helpers ---------------------------------------------------------

    def _index_of(self, node: _Node) -> QModelIndex:
        if node is self._root:
            return QModelIndex()
        return self.createIndex(node.row(), 0, node)
