# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Dialogs used by the ProtoSkipper main window."""

from protoskipper.gui.dialogs.add_register import AddRegisterDialog
from protoskipper.gui.dialogs.audit_view import AuditViewDialog
from protoskipper.gui.dialogs.iec104_command import Iec104CommandDialog
from protoskipper.gui.dialogs.iec104_conformance import Iec104ConformanceDialog
from protoskipper.gui.dialogs.iec104_diff import Iec104DiffDialog
from protoskipper.gui.dialogs.iec104_fuzzer import Iec104FuzzerDialog
from protoskipper.gui.dialogs.iec104_pcap_viewer import Iec104PcapViewerDialog
from protoskipper.gui.dialogs.iec104_slave import NewSlaveDialog, SlaveRequest
from protoskipper.gui.dialogs.new_connection import NewConnectionDialog
from protoskipper.gui.dialogs.preferences import PreferencesDialog
from protoskipper.gui.dialogs.probe_network import ProbeNetworkDialog, ProbeSelection
from protoskipper.gui.dialogs.safety_confirm import SafetyConfirmDialog
from protoskipper.gui.dialogs.update_checker import UpdateCheckerDialog
from protoskipper.gui.dialogs.write_dialog import WriteDialog

__all__ = [
    "AddRegisterDialog",
    "AuditViewDialog",
    "Iec104CommandDialog",
    "Iec104ConformanceDialog",
    "Iec104DiffDialog",
    "Iec104FuzzerDialog",
    "Iec104PcapViewerDialog",
    "NewConnectionDialog",
    "NewSlaveDialog",
    "PreferencesDialog",
    "ProbeNetworkDialog",
    "ProbeSelection",
    "SafetyConfirmDialog",
    "SlaveRequest",
    "UpdateCheckerDialog",
    "WriteDialog",
]
