# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet MS/TP (Master-Slave / Token-Passing) transport scaffold (P7.D).

MS/TP operates over RS-485 serial hardware (ASHRAE Annex F).  This module
provides a skeleton ``MSTPSession`` that documents the token-passing protocol
constants and state machine.  All methods raise ``NotImplementedError`` until
RS-485 hardware support is integrated.

Requires ``pyserial`` for physical access.  Simulation can be performed via a
virtual null-modem pair (``socat``).

Usage::

    from protoskipper.builtin_drivers.bacnet.mstp import MSTPSession

    sess = MSTPSession(
        port="/dev/ttyUSB0",
        mac=5,
        baud=76800,
    )
    # sess.open()  ← raises NotImplementedError
"""

from __future__ import annotations

from enum import IntEnum

__all__ = ["MSTP_CONSTANTS", "MSTPSession", "MSTPState"]

_NOT_IMPLEMENTED_MSG = (
    "BACnet MS/TP transport is not yet implemented. "
    "RS-485 hardware access requires pyserial and a platform-specific RS-485 driver."
)

# ---------------------------------------------------------------------------
# Protocol constants (ASHRAE 135 Annex F)
# ---------------------------------------------------------------------------

MSTP_CONSTANTS: dict[str, int] = {
    # Preamble
    "PREAMBLE_1": 0x55,
    "PREAMBLE_2": 0xFF,
    # Frame types (Table F-3)
    "FRAME_TYPE_TOKEN": 0x00,
    "FRAME_TYPE_POLL_FOR_MASTER": 0x01,
    "FRAME_TYPE_REPLY_TO_POLL_FOR_MASTER": 0x02,
    "FRAME_TYPE_TEST_REQUEST": 0x03,
    "FRAME_TYPE_TEST_RESPONSE": 0x04,
    "FRAME_TYPE_BACNET_DATA_EXPECTING_REPLY": 0x05,
    "FRAME_TYPE_BACNET_DATA_NOT_EXPECTING_REPLY": 0x06,
    "FRAME_TYPE_REPLY_POSTPONED": 0x07,
    # Timing constants (in milliseconds)
    "T_FRAME_ABORT": 20,  # ms
    "T_NO_TOKEN": 500,  # ms
    "T_REPLY_DELAY": 250,  # ms
    "T_SLOT": 10,  # ms
    "T_USAGE_TIMEOUT": 20,  # ms
    "N_MAX_INFO_FRAMES": 5,
    "N_MAX_MASTER": 127,
    "N_MIN_OCTETS": 4,
}


class MSTPState(IntEnum):
    """Master-node state machine states (ASHRAE Annex F §9.3)."""

    INITIALIZE = 0
    IDLE = 1
    USE_TOKEN = 2
    WAIT_FOR_REPLY = 3
    DONE_WITH_TOKEN = 4
    PASS_TOKEN = 5
    NO_TOKEN = 6
    POLL_FOR_MASTER = 7
    ANSWER_DATA_REQUEST = 8


class MSTPSession:
    """Stub for a BACnet MS/TP serial session.

    Parameters
    ----------
    port:
        Serial port device path (e.g. ``/dev/ttyUSB0``, ``COM3``).
    mac:
        MS/TP MAC address for this node (0-127).
    baud:
        Baud rate.  Standard values: 9600, 19200, 38400, 57600, 76800, 115200.
    data_bits, parity, stop_bits:
        Serial framing.  MS/TP requires 8N1.
    max_info_frames:
        Maximum number of data frames to send per token hold.
    """

    def __init__(
        self,
        port: str,
        *,
        mac: int = 1,
        baud: int = 76800,
        data_bits: int = 8,
        parity: str = "N",
        stop_bits: int = 1,
        max_info_frames: int = 5,
    ) -> None:
        if not 0 <= mac <= 127:
            raise ValueError(f"MS/TP MAC address must be 0-127, got {mac}")
        if baud not in {9600, 19200, 38400, 57600, 76800, 115200}:
            raise ValueError(f"Non-standard baud rate {baud}; ensure device supports it")
        self.port = port
        self.mac = mac
        self.baud = baud
        self.data_bits = data_bits
        self.parity = parity
        self.stop_bits = stop_bits
        self.max_info_frames = max_info_frames
        self.state = MSTPState.INITIALIZE

    def open(self) -> None:
        """Open the serial port and begin token-passing."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def close(self) -> None:
        """Release the token and close the serial port."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def send_frame(self, frame_type: int, dest: int, data: bytes = b"") -> None:
        """Transmit an MS/TP frame to *dest*."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def read_property(self, device_mac: int, object_id: str, prop: str) -> object:
        """ReadProperty via MS/TP routing."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def who_is(self, low: int = 0, high: int = 127) -> list[dict[str, object]]:
        """Broadcast Who-Is on the MS/TP segment."""
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def __repr__(self) -> str:
        return (
            f"<MSTPSession port={self.port!r} mac={self.mac} baud={self.baud} "
            f"state={self.state.name}>"
        )
