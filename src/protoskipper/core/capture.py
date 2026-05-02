# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Per-session protocol-frame capture buffer.

Architecture
------------
:class:`RingBufferCaptureSink` is a concrete :class:`~protoskipper.core.driver.CaptureSink`
implementation backed by a fixed-size ``collections.deque``.  When the buffer is
full the oldest frame is silently evicted (FIFO ring semantics).

:meth:`RingBufferCaptureSink.flush_to` serialises the current buffer contents to
a simple binary file using the following per-frame layout::

    [8 bytes  ] UNIX timestamp as a big-endian IEEE 754 double
    [1 byte   ] direction flag: 0x01 = TX, 0x02 = RX
    [4 bytes  ] payload length N, big-endian uint32
    [N bytes  ] raw payload bytes

This format is intentionally simple so a future Phase-2 exporter can convert
it to pcapng without needing any schema migration.  A 4-byte magic header and
version byte are written at the start of every file so readers can detect the
format.

Thread safety
-------------
:meth:`write_frame` and :meth:`flush_to` both hold a :class:`threading.Lock`
for the duration of their operation, so callers on different threads (e.g. the
worker thread appending frames while the GUI thread requests a flush) are safe.
"""

from __future__ import annotations

import struct
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# File format constants
_MAGIC = b"PSCAP"  # ProtoSkipper CAPture
_VERSION = b"\x01"
_DIR_TX: int = 0x01
_DIR_RX: int = 0x02

# Header struct: double timestamp + uint8 direction + uint32 payload length
_FRAME_HEADER_FMT = ">dBI"
_FRAME_HEADER_SIZE = struct.calcsize(_FRAME_HEADER_FMT)


@dataclass(frozen=True)
class CapturedFrame:
    """One protocol frame captured from the wire."""

    timestamp: datetime
    direction: str  #: "tx" | "rx"
    payload: bytes
    metadata: Mapping[str, Any] | None = None


class RingBufferCaptureSink:
    """A bounded in-memory capture sink implementing :class:`~protoskipper.core.driver.CaptureSink`.

    Parameters
    ----------
    max_frames:
        Maximum number of frames to retain.  When the buffer is full the
        oldest frame is evicted to make room for the new one.  Defaults to
        10 000.
    """

    def __init__(self, max_frames: int = 10_000) -> None:
        if max_frames < 1:
            raise ValueError(f"max_frames must be >= 1, got {max_frames}")
        self._max_frames = max_frames
        self._buf: deque[CapturedFrame] = deque(maxlen=max_frames)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # CaptureSink protocol
    # ------------------------------------------------------------------

    def write_frame(
        self,
        timestamp: datetime,
        direction: str,
        payload: bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Append a captured frame to the ring buffer.

        If the buffer is already at ``max_frames`` capacity the oldest frame
        is silently discarded (deque maxlen semantics).
        """
        frame = CapturedFrame(
            timestamp=timestamp,
            direction=direction,
            payload=payload,
            metadata=metadata,
        )
        with self._lock:
            self._buf.append(frame)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def frame_count(self) -> int:
        """Current number of frames in the buffer."""
        with self._lock:
            return len(self._buf)

    def flush_to(self, path: Path) -> int:
        """Write all buffered frames to *path* and return the frame count written.

        The buffer is **not** cleared after flushing so the data is still
        available for a second flush or inspection.  The file at *path* is
        created (or overwritten) atomically via a sibling temp file.

        File layout::

            [MAGIC (5 bytes)] [VERSION (1 byte)] [frames...]

        Each frame is serialised as::

            [timestamp: big-endian f64] [direction: u8] [len: big-endian u32] [payload]

        Returns
        -------
        int
            Number of frames written.
        """
        with self._lock:
            snapshot = list(self._buf)

        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp_path.open("wb") as fh:
                fh.write(_MAGIC)
                fh.write(_VERSION)
                for frame in snapshot:
                    dir_byte = _DIR_TX if frame.direction == "tx" else _DIR_RX
                    header = struct.pack(
                        _FRAME_HEADER_FMT,
                        frame.timestamp.timestamp(),
                        dir_byte,
                        len(frame.payload),
                    )
                    fh.write(header)
                    fh.write(frame.payload)
            tmp_path.replace(path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        return len(snapshot)

    def read_frames(self, path: Path) -> list[CapturedFrame]:
        """Read frames previously written by :meth:`flush_to`.

        This is a convenience helper for tests and the CLI; the GUI uses a
        live model that reads from the in-memory buffer directly.

        Raises
        ------
        ValueError
            If the file magic or version does not match.
        """
        with path.open("rb") as fh:
            magic = fh.read(len(_MAGIC))
            if magic != _MAGIC:
                raise ValueError(f"Not a ProtoSkipper capture file: {path}")
            version = fh.read(1)
            if version != _VERSION:
                raise ValueError(f"Unsupported capture version {version!r}: {path}")

            frames: list[CapturedFrame] = []
            while True:
                header_bytes = fh.read(_FRAME_HEADER_SIZE)
                if not header_bytes:
                    break
                if len(header_bytes) < _FRAME_HEADER_SIZE:
                    raise ValueError(f"Truncated frame header in {path}")
                ts_f64, dir_byte, payload_len = struct.unpack(_FRAME_HEADER_FMT, header_bytes)
                payload = fh.read(payload_len)
                if len(payload) < payload_len:
                    raise ValueError(f"Truncated frame payload in {path}")
                direction = "tx" if dir_byte == _DIR_TX else "rx"
                frames.append(
                    CapturedFrame(
                        timestamp=datetime.fromtimestamp(ts_f64),
                        direction=direction,
                        payload=payload,
                    )
                )
        return frames
