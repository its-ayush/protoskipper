# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Shared types used by the GUI service layer.

Kept separate from :mod:`protoskipper.core` so the core package does not pick
up Qt dependencies, and separate from individual panels so the types can be
shared without introducing cyclic imports.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import NewType

# A SessionId is just a wrapped UUID4 hex; it is not the underlying core
# Session object reference. The GUI uses it as a stable handle that survives
# across thread hops without leaking a reference to the worker's Session.
SessionId = NewType("SessionId", str)


def new_session_id() -> SessionId:
    return SessionId(uuid.uuid4().hex)


class Direction(str, Enum):
    """Direction of a captured protocol frame, from the master's perspective."""

    TX = "tx"  #: ProtoSkipper -> device
    RX = "rx"  #: device -> ProtoSkipper


@dataclass(frozen=True)
class CapturedFrame:
    """A single protocol frame captured during a session.

    Frames flow from drivers into the per-session capture buffer and from
    there into :attr:`ApplicationState.frame_captured` for the packet view.
    Drivers populate ``decoded`` if they can; the packet view falls back on
    raw hex if not.
    """

    session_id: SessionId
    timestamp: datetime
    direction: Direction
    payload: bytes
    decoded: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
