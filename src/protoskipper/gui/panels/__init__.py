# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Reusable panels for the ProtoSkipper main window."""

from protoskipper.gui.panels.device_tree import DeviceTreePanel
from protoskipper.gui.panels.iec104_bench import Iec104BenchPanel
from protoskipper.gui.panels.iec104_interrogation import Iec104InterrogationPanel
from protoskipper.gui.panels.iec104_soe import SoePanel
from protoskipper.gui.panels.iec104_timesync import Iec104TimeSyncPanel
from protoskipper.gui.panels.object_browser import ObjectBrowserPanel
from protoskipper.gui.panels.packet_view import PacketViewPanel
from protoskipper.gui.panels.routing import RoutingPanel
from protoskipper.gui.panels.schedule_editor import ScheduleEditorPanel
from protoskipper.gui.panels.scripting_console import ScriptingConsolePanel
from protoskipper.gui.panels.session_status import SessionStatusPanel
from protoskipper.gui.panels.watchlist import WatchlistPanel

__all__ = [
    "DeviceTreePanel",
    "Iec104BenchPanel",
    "Iec104InterrogationPanel",
    "Iec104TimeSyncPanel",
    "ObjectBrowserPanel",
    "PacketViewPanel",
    "RoutingPanel",
    "ScheduleEditorPanel",
    "ScriptingConsolePanel",
    "SessionStatusPanel",
    "SoePanel",
    "WatchlistPanel",
]
