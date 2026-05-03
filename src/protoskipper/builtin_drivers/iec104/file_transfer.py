# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""IEC 60870-5-104 file transfer services (§P4.B.8 / §P4.C.5).

Protocol overview (IEC 60870-5-101 / 104 section 7.3.5)
---------------------------------------------------------
File transfer uses seven ASDU types (TypeID 120-126):

  F_FR_NA_1 (120)  — File Ready        slave→master: "I have this file"
  F_SR_NA_1 (121)  — Section Ready     slave→master: "next section begins"
  F_SC_NA_1 (122)  — Call / Select     master→slave: directory / select / call
  F_LS_NA_1 (123)  — Last seg/section  slave→master: "section/file ended"
  F_AF_NA_1 (124)  — Ack File/Section  master→slave: "I received section/file"
  F_SG_NA_1 (125)  — Segment           slave→master: data chunk ≤ 249 bytes
  F_DR_TA_1 (126)  — Directory         slave→master: list of files

Download sequence
-----------------
1. Master sends  F_SC_NA_1 (call_type=0 = call_directory) IOA=0 to browse.
2. Slave replies  F_DR_TA_1 entries (one ASDU per file, or batched).
3. Master sends  F_SC_NA_1 (call_type=1 = select_file) IOA=<name_ioa>.
4. Slave replies  F_FR_NA_1 IOA=<name_ioa>, length=<total_bytes>.
5. For each section N:
   a. Slave sends F_SR_NA_1 IOA=<name_ioa>, section=N, length=<sec_bytes>.
   b. Master sends F_SC_NA_1 (call_type=3 = call_section) section=N.
   c. Slave sends one or more F_SG_NA_1 segments (≤ 249 bytes payload each).
   d. Slave sends F_LS_NA_1 (last_qualifier=1) to terminate the section.
   e. Master sends F_AF_NA_1 (ack_type=1) to acknowledge the section.
6. Slave sends F_LS_NA_1 (last_qualifier=0) to mark file complete.
7. Master sends F_AF_NA_1 (ack_type=0) to acknowledge the file.

This implementation uses a single section per file (the standard allows
multiple sections; we simplify to 1 for the test harness).

Constants
---------
MAX_SEGMENT_PAYLOAD = 249  (6-byte APCI + 6-byte ASDU header + 249 = 261 < 255+6)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from protoskipper.builtin_drivers.iec104.asdu import (
    COT,
    Asdu,
    InformationObject,
    TypeID,
)

__all__ = [
    "MAX_SEGMENT_PAYLOAD",
    "DirectoryEntry",
    "FileDownloadError",
    "FileTransferAborted",
    "build_ack_file",
    "build_ack_section",
    "build_call_directory",
    "build_call_section",
    "build_directory_entry",
    "build_file_ready",
    "build_last_section",
    "build_section_ready",
    "build_segment",
    "build_select_file",
    "parse_ack",
    "parse_directory_entries",
    "parse_file_ready",
    "parse_last_section",
    "parse_section_ready",
    "parse_segment",
]

MAX_SEGMENT_PAYLOAD = 238  # IEC 104: 249 max ASDU - 6 hdr - 3 IOA - 2 elem hdr = 238

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FileDownloadError(OSError):
    """Raised when the slave rejects or aborts a file transfer."""


class FileTransferAborted(FileDownloadError):
    """Raised when a NAK or unexpected ASDU terminates the transfer."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DirectoryEntry:
    """One entry from an F_DR_TA_1 (directory) response."""

    ioa: int
    name: str  # up to 8 ASCII characters (IEC 101 convention)
    length: int  # file length in bytes
    creation_time: datetime | None = None
    status: int = 0  # file status bits


# ---------------------------------------------------------------------------
# Master → Slave builders
# ---------------------------------------------------------------------------


def build_call_directory(ca: int) -> Asdu:
    """F_SC_NA_1: call directory (call_type=0, IOA=0)."""
    return Asdu(
        type_id=TypeID.F_SC_NA_1,
        cot=COT.REQ,
        ca=ca,
        objects=[
            InformationObject(ioa=0, value=0)  # call_type=0 in value field
        ],
    )


def build_select_file(ca: int, name_ioa: int) -> Asdu:
    """F_SC_NA_1: select file (call_type=1)."""
    return Asdu(
        type_id=TypeID.F_SC_NA_1,
        cot=COT.REQ,
        ca=ca,
        objects=[InformationObject(ioa=name_ioa, value=1)],
    )


def build_call_section(ca: int, name_ioa: int, section: int) -> Asdu:
    """F_SC_NA_1: call section (call_type=3, section number in extra)."""
    return Asdu(
        type_id=TypeID.F_SC_NA_1,
        cot=COT.REQ,
        ca=ca,
        objects=[InformationObject(ioa=name_ioa, value=3, quality=section)],
    )


def build_ack_section(ca: int, name_ioa: int) -> Asdu:
    """F_AF_NA_1: acknowledge section (ack_type=1)."""
    return Asdu(
        type_id=TypeID.F_AF_NA_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=name_ioa, value=1)],
    )


def build_ack_file(ca: int, name_ioa: int) -> Asdu:
    """F_AF_NA_1: acknowledge file (ack_type=0)."""
    return Asdu(
        type_id=TypeID.F_AF_NA_1,
        cot=COT.ACT,
        ca=ca,
        objects=[InformationObject(ioa=name_ioa, value=0)],
    )


# ---------------------------------------------------------------------------
# Slave → Master builders
# ---------------------------------------------------------------------------


def build_file_ready(ca: int, name_ioa: int, length: int) -> Asdu:
    """F_FR_NA_1: file ready — announces a file and its total byte count."""
    return Asdu(
        type_id=TypeID.F_FR_NA_1,
        cot=COT.ACTCON,
        ca=ca,
        objects=[InformationObject(ioa=name_ioa, value=length)],
    )


def build_section_ready(ca: int, name_ioa: int, section: int, length: int) -> Asdu:
    """F_SR_NA_1: section ready — announces the length of a section."""
    return Asdu(
        type_id=TypeID.F_SR_NA_1,
        cot=COT.ACTCON,
        ca=ca,
        objects=[InformationObject(ioa=name_ioa, value=length, quality=section)],
    )


def build_segment(ca: int, name_ioa: int, section: int, payload: bytes) -> Asdu:
    """F_SG_NA_1: one data segment (payload ≤ MAX_SEGMENT_PAYLOAD bytes)."""
    if len(payload) > MAX_SEGMENT_PAYLOAD:
        raise ValueError(f"Segment payload {len(payload)} > {MAX_SEGMENT_PAYLOAD}")
    return Asdu(
        type_id=TypeID.F_SG_NA_1,
        cot=COT.SPONT,
        ca=ca,
        objects=[InformationObject(ioa=name_ioa, value=payload, quality=section)],
    )


def build_last_section(ca: int, name_ioa: int, section: int, *, file_done: bool = False) -> Asdu:
    """F_LS_NA_1: last section (last_qualifier=1) or last file (last_qualifier=0).

    ``file_done=True`` → last_qualifier=0 (file complete).
    ``file_done=False`` → last_qualifier=1 (section complete).
    """
    return Asdu(
        type_id=TypeID.F_LS_NA_1,
        cot=COT.SPONT,
        ca=ca,
        objects=[InformationObject(ioa=name_ioa, value=0 if file_done else 1, quality=section)],
    )


def build_directory_entry(ca: int, entry: DirectoryEntry) -> Asdu:
    """F_DR_TA_1: one directory entry ASDU."""
    return Asdu(
        type_id=TypeID.F_DR_TA_1,
        cot=COT.REQ,
        ca=ca,
        objects=[
            InformationObject(
                ioa=entry.ioa,
                value=entry.name,
                quality=entry.length,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Parsers — extract fields from received ASDUs
# ---------------------------------------------------------------------------


def parse_directory_entries(asdu: Asdu) -> list[DirectoryEntry]:
    """Parse F_DR_TA_1 → list of DirectoryEntry."""
    entries: list[DirectoryEntry] = []
    for obj in asdu.objects:
        entries.append(
            DirectoryEntry(
                ioa=obj.ioa,
                name=str(obj.value) if obj.value is not None else "",
                length=int(obj.quality) if obj.quality is not None else 0,
            )
        )
    return entries


def parse_file_ready(asdu: Asdu) -> tuple[int, int]:
    """Parse F_FR_NA_1 → (name_ioa, total_length)."""
    obj = asdu.objects[0]
    return (obj.ioa, int(obj.value) if obj.value is not None else 0)


def parse_section_ready(asdu: Asdu) -> tuple[int, int, int]:
    """Parse F_SR_NA_1 → (name_ioa, section, section_length)."""
    obj = asdu.objects[0]
    return (
        obj.ioa,
        int(obj.quality) if obj.quality is not None else 0,
        int(obj.value) if obj.value is not None else 0,
    )


def parse_segment(asdu: Asdu) -> tuple[int, int, bytes]:
    """Parse F_SG_NA_1 → (name_ioa, section, payload)."""
    obj = asdu.objects[0]
    payload = obj.value if isinstance(obj.value, bytes) else b""
    return (obj.ioa, int(obj.quality) if obj.quality is not None else 0, payload)


def parse_last_section(asdu: Asdu) -> tuple[int, int, bool]:
    """Parse F_LS_NA_1 → (name_ioa, section, file_done).

    ``file_done`` is True when last_qualifier==0 (file complete).
    """
    obj = asdu.objects[0]
    file_done = (int(obj.value) if obj.value is not None else 1) == 0
    return (obj.ioa, int(obj.quality) if obj.quality is not None else 0, file_done)


def parse_ack(asdu: Asdu) -> tuple[int, int]:
    """Parse F_AF_NA_1 → (name_ioa, ack_type). ack_type 0=file, 1=section."""
    obj = asdu.objects[0]
    return (obj.ioa, int(obj.value) if obj.value is not None else 0)
