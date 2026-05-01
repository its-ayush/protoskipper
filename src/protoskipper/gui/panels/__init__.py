# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Reusable panels for the ProtoSkipper main window."""

from protoskipper.gui.panels.device_tree import DeviceTreePanel
from protoskipper.gui.panels.object_browser import ObjectBrowserPanel
from protoskipper.gui.panels.packet_view import PacketViewPanel
from protoskipper.gui.panels.session_status import SessionStatusPanel
from protoskipper.gui.panels.watchlist import WatchlistPanel

__all__ = [
    "DeviceTreePanel",
    "ObjectBrowserPanel",
    "PacketViewPanel",
    "SessionStatusPanel",
    "WatchlistPanel",
]
