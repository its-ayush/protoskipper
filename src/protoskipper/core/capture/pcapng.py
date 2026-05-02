# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""pcapng writer for ProtoSkipper captured frames.

File layout
-----------
A pcapng file written by this module contains exactly three block types:

1. **Section Header Block** (type ``0x0A0D0D0A``) — one per file, carries the
   byte-order magic, version, and a ProtoSkipper application comment.
2. **Interface Description Block** (type ``0x00000001``) — one per file, with
   link type ``147`` (``LINKTYPE_USER0``), reserved for user-defined protocols.
3. **Custom Block** (type ``0x00000BAD``, copyable) — one per captured frame.

Custom Block body layout
~~~~~~~~~~~~~~~~~~~~~~~~~
All multibyte integers inside the Custom Block body are little-endian (matching
the enclosing pcapng byte order)::

    [4]  PEN (Private Enterprise Number) — 0x00004453 ("DS" = DataSailors)
    --- Custom data ---
    [8]  timestamp: UNIX epoch as little-endian float64
    [1]  direction: 0x01 = TX, 0x02 = RX
    [2]  protocol_id length (little-endian uint16)
    [N]  protocol_id (UTF-8)
    [4]  payload length (little-endian uint32)
    [P]  raw payload bytes
    [0-3] zero-padding to align the block to a 32-bit boundary

Spec compliance
~~~~~~~~~~~~~~~
The file structure conforms to the pcapng specification (RFC 9443):

* Byte-order magic ``0x1A2B3C4D`` in the SHB signals little-endian byte order.
* Every block carries the block total length at both start and end.
* All block lengths are multiples of 4 (padded as required).
* Unknown custom blocks MUST be tolerated by conforming readers; Wireshark
  handles them silently.

Replay
~~~~~~
:func:`read_frames` re-reads a file written by :func:`write_pcapng`, yielding
:class:`~protoskipper.core.capture.CapturedFrame` objects in order.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from protoskipper.core.capture import CapturedFrame

# ---------------------------------------------------------------------------
# Block type constants (pcapng spec, RFC 9443)
# ---------------------------------------------------------------------------
_BT_SHB: int = 0x0A0D0D0A  # Section Header Block
_BT_IDB: int = 0x00000001  # Interface Description Block
_BT_CUSTOM: int = 0x00000BAD  # Custom Block (copyable)

_BYTE_ORDER_MAGIC: int = 0x1A2B3C4D  # LE sentinel in SHB
_VERSION_MAJOR: int = 1
_VERSION_MINOR: int = 0

# Link type 147 = LINKTYPE_USER0; safe for user-defined protocols.
_LINKTYPE_USER0: int = 147

# Private Enterprise Number — 0x00004453 = "DS" (DataSailors, placeholder).
# An official IANA PEN should replace this before public release.
_PEN: int = 0x00004453

_DIR_TX: int = 0x01
_DIR_RX: int = 0x02

_APP_COMMENT = b"ProtoSkipper capture\x00"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pad4(n: int) -> int:
    """Return *n* rounded up to the nearest multiple of 4."""
    return (n + 3) & ~3


def _block(block_type: int, body: bytes) -> bytes:
    """Wrap *body* in a pcapng block envelope (type + length + body + length).

    Pads body to a 4-byte boundary before computing length.
    """
    padded = body + b"\x00" * (_pad4(len(body)) - len(body))
    total_len = 4 + 4 + len(padded) + 4  # type + len + body + len
    hdr = struct.pack("<II", block_type, total_len)
    footer = struct.pack("<I", total_len)
    return hdr + padded + footer


def _shb() -> bytes:
    """Build a Section Header Block."""
    body = struct.pack(
        "<IHH",
        _BYTE_ORDER_MAGIC,
        _VERSION_MAJOR,
        _VERSION_MINOR,
    )
    body += struct.pack("<q", -1)  # section length = -1 (unspecified)
    # Append a block comment option (opt_code=1) with the application name.
    opt_body = _app_comment_option()
    body += opt_body
    # opt_endofopt (code=0, length=0)
    body += struct.pack("<HH", 0, 0)
    return _block(_BT_SHB, body)


def _app_comment_option() -> bytes:
    """Build an opt_comment TLV carrying the application name."""
    padded_val = _APP_COMMENT + b"\x00" * (_pad4(len(_APP_COMMENT)) - len(_APP_COMMENT))
    return struct.pack("<HH", 1, len(_APP_COMMENT)) + padded_val


def _idb() -> bytes:
    """Build an Interface Description Block (LINKTYPE_USER0, no options)."""
    body = struct.pack(
        "<HHI",
        _LINKTYPE_USER0,
        0,  # Reserved
        65535,  # SnapLen — capture full frames
    )
    # opt_endofopt
    body += struct.pack("<HH", 0, 0)
    return _block(_BT_IDB, body)


def _custom_block(frame: CapturedFrame, protocol_id: str) -> bytes:
    """Serialise one :class:`CapturedFrame` as a pcapng Custom Block."""
    pid_bytes = protocol_id.encode("utf-8")
    dir_byte = _DIR_TX if frame.direction == "tx" else _DIR_RX

    # Custom data (after the PEN)
    custom_data = struct.pack("<dBH", frame.timestamp.timestamp(), dir_byte, len(pid_bytes))
    custom_data += pid_bytes
    custom_data += struct.pack("<I", len(frame.payload))
    custom_data += frame.payload

    body = struct.pack("<I", _PEN) + custom_data
    return _block(_BT_CUSTOM, body)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_pcapng(
    frames: Sequence[CapturedFrame],
    path: Path,
    protocol_id: str = "unknown",
) -> int:
    """Write *frames* to a pcapng file at *path*.

    The file is created atomically via a sibling temp path; existing files
    at *path* are replaced.

    Parameters
    ----------
    frames:
        Captured frames to serialise, in order.
    path:
        Destination file path.
    protocol_id:
        Short protocol identifier embedded in each Custom Block (e.g.
        ``"modbus.tcp"``).  Stored as UTF-8.

    Returns
    -------
    int
        Number of frames written.
    """
    tmp = path.with_suffix(path.suffix + ".pcaptmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(_shb())
            fh.write(_idb())
            for frame in frames:
                fh.write(_custom_block(frame, protocol_id))
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return len(frames)


def read_pcapng(path: Path) -> list[CapturedFrame]:
    """Read frames from a pcapng file written by :func:`write_pcapng`.

    Only Custom Blocks whose PEN matches :data:`_PEN` are decoded; all other
    block types are skipped silently (conforming to the pcapng spec).

    Raises
    ------
    ValueError
        If the file does not start with a valid Section Header Block or uses
        an unsupported byte order.
    """
    frames: list[CapturedFrame] = []

    with path.open("rb") as fh:
        raw = fh.read()

    if len(raw) < 12:
        raise ValueError(f"File too short to be a valid pcapng file: {path}")

    # The very first block MUST be an SHB.
    first_type = struct.unpack_from("<I", raw, 0)[0]
    if first_type != _BT_SHB:
        raise ValueError(
            f"Not a valid pcapng file (expected SHB type {_BT_SHB:#010x}, "
            f"got {first_type:#010x}): {path}"
        )

    pos = 0
    total = len(raw)
    byte_order_verified = False

    while pos < total:
        if pos + 8 > total:
            break  # Truncated — stop gracefully

        block_type, block_len = struct.unpack_from("<II", raw, pos)

        if block_type == _BT_SHB:
            # Verify byte-order magic
            if pos + 12 > total:
                raise ValueError(f"Truncated SHB in {path}")
            bom = struct.unpack_from("<I", raw, pos + 8)[0]
            if bom != _BYTE_ORDER_MAGIC:
                raise ValueError(
                    f"Unsupported byte order (magic={bom:#010x}) in {path}; "
                    "only little-endian pcapng files are supported."
                )
            byte_order_verified = True

        elif block_type == _BT_CUSTOM:
            if not byte_order_verified:
                raise ValueError(f"Custom Block before SHB in {path}")
            body_start = pos + 8
            body_end = pos + block_len - 4  # excluding trailing length field
            body = raw[body_start:body_end]

            if len(body) < 4:
                pos += block_len
                continue

            pen = struct.unpack_from("<I", body, 0)[0]
            if pen != _PEN:
                pos += block_len
                continue  # Different PEN — skip

            # Decode custom data
            offset = 4
            if offset + 11 > len(body):
                pos += block_len
                continue

            ts_f64, dir_byte, pid_len = struct.unpack_from("<dBH", body, offset)
            offset += 11  # 8 + 1 + 2

            if offset + pid_len > len(body):
                pos += block_len
                continue

            direction = "tx" if dir_byte == _DIR_TX else "rx"
            offset += pid_len  # skip protocol_id (used by caller metadata)

            if offset + 4 > len(body):
                pos += block_len
                continue

            payload_len = struct.unpack_from("<I", body, offset)[0]
            offset += 4

            payload = body[offset : offset + payload_len]
            frames.append(
                CapturedFrame(
                    timestamp=datetime.fromtimestamp(ts_f64),
                    direction=direction,
                    payload=payload,
                )
            )

        pos += block_len

    return frames
