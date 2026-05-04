# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""BACnet/SC PCAP dissection with TLS decryption via SSLKEYLOGFILE (P7.G.3).

BACnet/SC traffic is carried over WebSocket (RFC 6455) secured with TLS 1.2 or
1.3.  Standard PCAP capture therefore contains *encrypted* WebSocket frames.
When the operator sets ``SSLKEYLOGFILE`` before capturing (or their TLS
implementation exports it), the resulting NSS key-log file can be used to
decrypt the traffic and expose the BVLC-SC / NPDU / APDU layers.

Two dissection back-ends are supported:

1. **pyshark** (``pip install pyshark``) — delegates decryption to the local
   ``tshark`` installation which natively honours ``SSLKEYLOGFILE``.  Recommended.
2. **Raw scaffold** — parses WebSocket framing from a pre-decrypted byte stream
   (e.g. a separate TLS-stripped pcap produced by ``ssldump`` / ``editcap``).
   The actual TLS record-layer decryption raises :class:`TLSDecryptionUnavailable`
   unless ``pyshark`` is installed.

BVLC-SC header format (ASHRAE Annex YY / draft 135-2016t)::

    0         1         2         3
    01234567  01234567  01234567  01234567
    function  flags     length(2) message-id(2) [optional-header-entries ...]

Usage::

    from protoskipper.builtin_drivers.bacnet.pcap_sc import ScPcapDissector, KeyLogFile

    kl = KeyLogFile.load("sslkeys.log")
    with ScPcapDissector("capture.pcapng", key_log=kl) as d:
        for frame in d.frames():
            print(frame.bvlc_sc_function, frame.npdu_raw.hex())

    # Without a key-log (pre-decrypted / plain-text WebSocket test capture):
    with ScPcapDissector("plaintext.pcap") as d:
        for frame in d.frames():
            print(frame)
"""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "KeyLogFile",
    "ScBACnetFrame",
    "ScPcapDissector",
    "TLSDecryptionUnavailable",
]

_logger = logging.getLogger(__name__)

# BACnet/SC default port (Annex YY)
BACNET_SC_PORT = 47808  # same UDP port as BACnet/IP; WebSocket distinguishes

# BVLC-SC function codes (ASHRAE Annex YY, Table YY-1)
BVLC_SC_FUNCTIONS: dict[int, str] = {
    0x00: "BVLC-SC-Result",
    0x01: "Encapsulated-NPDU",
    0x02: "Address-Resolution",
    0x03: "Address-Resolution-ACK",
    0x04: "Advertisement",
    0x05: "Advertisement-Solicitation",
    0x06: "Connect-Request",
    0x07: "Connect-Accept",
    0x08: "Disconnect-Request",
    0x09: "Disconnect-ACK",
    0x0A: "Heartbeat-Request",
    0x0B: "Heartbeat-ACK",
    0x0C: "Proprietary-Message",
}

# WebSocket opcode names (RFC 6455)
WS_OPCODES: dict[int, str] = {
    0x0: "continuation",
    0x1: "text",
    0x2: "binary",
    0x8: "close",
    0x9: "ping",
    0xA: "pong",
}

# NSS Key Log entry labels (RFC 8448 / NSS convention)
_KEYLOG_LABELS = frozenset(
    [
        "CLIENT_RANDOM",
        "CLIENT_HANDSHAKE_TRAFFIC_SECRET",
        "SERVER_HANDSHAKE_TRAFFIC_SECRET",
        "CLIENT_TRAFFIC_SECRET_0",
        "SERVER_TRAFFIC_SECRET_0",
        "EXPORTER_SECRET",
        "EARLY_TRAFFIC_SECRET",
    ]
)


class TLSDecryptionUnavailable(RuntimeError):
    """Raised when TLS decryption is requested but ``pyshark`` / ``tshark`` is unavailable."""


# ---------------------------------------------------------------------------
# NSS Key Log File
# ---------------------------------------------------------------------------


@dataclass
class KeyLogEntry:
    """One entry from an NSS key-log file."""

    label: str
    client_random: str  # hex string
    secret: str  # hex string


@dataclass
class KeyLogFile:
    """Parsed NSS SSLKEYLOGFILE.

    See https://www.ietf.org/archive/id/draft-ietf-tls-keylogfile-00.txt
    """

    entries: list[KeyLogEntry] = field(default_factory=list)
    path: str = ""

    @classmethod
    def load(cls, path: str | Path) -> KeyLogFile:
        """Parse an NSS key-log file.

        Parameters
        ----------
        path:
            Path to the key-log file written by OpenSSL (``SSLKEYLOGFILE``),
            Firefox, Chrome, etc.

        Returns
        -------
        KeyLogFile
            Parsed entries.  Comments (lines beginning with ``#``) and blank
            lines are ignored.  Unknown labels are also ignored.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Key log file not found: {p}")

        entries: list[KeyLogEntry] = []
        line_re = re.compile(r"^(\w+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s*$")
        for lineno, raw_line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            m = line_re.match(line)
            if m is None:
                _logger.debug("Key log line %d: unrecognised format, skipping", lineno)
                continue
            label, client_random, secret = m.group(1), m.group(2), m.group(3)
            if label not in _KEYLOG_LABELS:
                _logger.debug("Key log line %d: unknown label %r, skipping", lineno, label)
                continue
            entries.append(KeyLogEntry(label=label, client_random=client_random, secret=secret))

        kl = cls(entries=entries, path=str(p))
        _logger.info("Loaded %d key-log entries from %s", len(entries), p)
        return kl

    def lookup(self, client_random: str) -> list[KeyLogEntry]:
        """Return all entries whose ``client_random`` matches (case-insensitive)."""
        needle = client_random.lower()
        return [e for e in self.entries if e.client_random.lower() == needle]

    def __len__(self) -> int:
        return len(self.entries)

    def __repr__(self) -> str:
        return f"KeyLogFile(entries={len(self.entries)}, path={self.path!r})"


# ---------------------------------------------------------------------------
# Dissected frame dataclass
# ---------------------------------------------------------------------------


@dataclass
class ScBACnetFrame:
    """Dissected BACnet/SC frame (BVLC-SC layer and above).

    Attributes
    ----------
    timestamp:
        Capture timestamp (seconds since epoch).
    src, dst:
        Source / destination ``host:port`` strings.
    ws_opcode:
        WebSocket opcode name (``"binary"`` for BACnet/SC data frames).
    ws_opcode_code:
        Raw WebSocket opcode integer.
    bvlc_sc_function:
        BVLC-SC function name from :data:`BVLC_SC_FUNCTIONS`.
    bvlc_sc_function_code:
        Raw BVLC-SC function code byte.
    bvlc_sc_flags:
        BVLC-SC header flags byte.
    message_id:
        BVLC-SC message identifier (2-byte big-endian field).
    npdu_raw:
        Raw bytes of the NPDU payload (after the BVLC-SC header), or empty.
    apdu_type:
        APDU type name from the NPDU payload, or empty string.
    apdu_type_code:
        Raw APDU type code, or -1 if unavailable.
    service:
        BACnet service name (from the APDU), or empty string.
    decrypted:
        ``True`` if the payload was decrypted using a key-log file or was
        already plaintext; ``False`` if returned as encrypted/opaque bytes.
    raw:
        Raw WebSocket application data bytes.
    """

    timestamp: float = 0.0
    src: str = ""
    dst: str = ""
    ws_opcode: str = ""
    ws_opcode_code: int = -1
    bvlc_sc_function: str = ""
    bvlc_sc_function_code: int = -1
    bvlc_sc_flags: int = 0
    message_id: int = -1
    npdu_raw: bytes = field(default_factory=bytes)
    apdu_type: str = ""
    apdu_type_code: int = -1
    service: str = ""
    decrypted: bool = False
    raw: bytes = field(default_factory=bytes)

    def __repr__(self) -> str:
        dec = "dec" if self.decrypted else "enc"
        return (
            f"<ScBACnetFrame t={self.timestamp:.3f} {self.src}→{self.dst} "
            f"bvlc={self.bvlc_sc_function!r} svc={self.service!r} [{dec}]>"
        )


# ---------------------------------------------------------------------------
# Dissector
# ---------------------------------------------------------------------------


class ScPcapDissector:
    """Dissect BACnet/SC frames from a PCAP/PCAPNG capture file.

    Parameters
    ----------
    path:
        Path to the capture file.
    key_log:
        Parsed :class:`KeyLogFile` for TLS decryption.  If ``None`` the
        dissector attempts to process only plaintext WebSocket traffic.
    port:
        TCP port that carries BACnet/SC WebSocket traffic (default 47808).
    strict:
        If ``True``, raise on parse errors.  If ``False`` (default), skip them.

    Notes
    -----
    Full TLS record-layer decryption requires the ``pyshark`` package
    (``pip install pyshark``) and a working ``tshark`` installation.  Without
    it, the dissector parses WebSocket framing from *unencrypted* streams only.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        key_log: KeyLogFile | None = None,
        port: int = BACNET_SC_PORT,
        strict: bool = False,
    ) -> None:
        self._path = Path(path)
        self._key_log = key_log
        self._port = port
        self._strict = strict
        self._pyshark: Any = None  # lazy-imported

    def __enter__(self) -> ScPcapDissector:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def frames(self) -> list[ScBACnetFrame]:
        """Return all dissected BACnet/SC frames.

        Uses ``pyshark`` + TLS key log if available; otherwise attempts
        WebSocket-only dissection of plaintext captures.

        Raises
        ------
        TLSDecryptionUnavailable
            If the capture appears to be TLS-encrypted and neither ``pyshark``
            nor a pre-decrypted stream is available.
        """
        if self._key_log is not None:
            return self._frames_via_pyshark()
        return self._frames_plaintext()

    def count(self) -> int:
        """Return the number of dissected BACnet/SC frames."""
        return len(self.frames())

    # ------------------------------------------------------------------
    # pyshark back-end
    # ------------------------------------------------------------------

    def _frames_via_pyshark(self) -> list[ScBACnetFrame]:
        """Dissect using pyshark + tshark TLS decryption."""
        try:
            import pyshark  # type: ignore[import-untyped]

            self._pyshark = pyshark
        except ImportError as exc:
            raise TLSDecryptionUnavailable(
                "TLS decryption requires pyshark: pip install pyshark. "
                "Alternatively, pre-decrypt the capture with Wireshark or ssldump."
            ) from exc

        if self._key_log is None or not self._key_log.entries:
            raise TLSDecryptionUnavailable("No key-log entries provided for decryption.")

        key_log_path = Path(self._key_log.path)
        if not key_log_path.exists():
            raise TLSDecryptionUnavailable(
                f"Key log file {key_log_path} not found; cannot decrypt."
            )

        override_prefs = {
            "tls.keylog_file": str(key_log_path),
        }
        capture = self._pyshark.FileCapture(
            str(self._path),
            display_filter=f"websocket and tcp.port=={self._port}",
            override_prefs=override_prefs,
        )
        results: list[ScBACnetFrame] = []
        try:
            for pkt in capture:
                frame = self._parse_pyshark_packet(pkt)
                if frame is not None:
                    results.append(frame)
        finally:
            capture.close()
        return results

    def _parse_pyshark_packet(self, pkt: Any) -> ScBACnetFrame | None:
        """Convert a pyshark packet into a :class:`ScBACnetFrame`."""
        try:
            ts = float(pkt.sniff_timestamp)
            src = str(getattr(pkt, "ip", pkt).src) if hasattr(pkt, "ip") else ""
            dst = str(getattr(pkt, "ip", pkt).dst) if hasattr(pkt, "ip") else ""
            ws_layer = pkt.websocket
            opcode_code = int(getattr(ws_layer, "websocket_websocket_opcode", -1))
            opcode = WS_OPCODES.get(opcode_code, f"0x{opcode_code:X}")
            payload_hex: str = str(getattr(ws_layer, "websocket_websocket_payload", "") or "")
            payload = bytes.fromhex(payload_hex.replace(":", "")) if payload_hex else b""
        except Exception:
            if self._strict:
                raise
            return None

        frame = ScBACnetFrame(
            timestamp=ts,
            src=src,
            dst=dst,
            ws_opcode=opcode,
            ws_opcode_code=opcode_code,
            raw=payload,
            decrypted=True,
        )
        if payload and opcode_code == 0x2:  # binary frame → BACnet/SC
            self._parse_bvlc_sc(payload, frame)
        return frame

    # ------------------------------------------------------------------
    # Plaintext WebSocket back-end
    # ------------------------------------------------------------------

    def _frames_plaintext(self) -> list[ScBACnetFrame]:
        """Parse plaintext (non-TLS) WebSocket frames from a PCAP."""
        try:
            import dpkt  # type: ignore[import-untyped]
        except ImportError as exc:
            raise TLSDecryptionUnavailable(
                "Plaintext WebSocket parsing requires dpkt: pip install dpkt"
            ) from exc

        results: list[ScBACnetFrame] = []
        # Reassemble TCP streams keyed by (src_ip:port, dst_ip:port)
        streams: dict[tuple[str, str], bytes] = {}

        with open(self._path, "rb") as f:
            try:
                capture = dpkt.pcapng.Reader(f)
            except Exception:
                f.seek(0)
                capture = dpkt.pcap.Reader(f)

            for ts, raw_pkt in capture:
                try:
                    eth = dpkt.ethernet.Ethernet(raw_pkt)
                    if not isinstance(eth.data, dpkt.ip.IP):
                        continue
                    ip = eth.data
                    if not isinstance(ip.data, dpkt.tcp.TCP):
                        continue
                    tcp = ip.data
                    if tcp.dport != self._port and tcp.sport != self._port:
                        continue

                    src = f"{self._fmt_ip(bytes(ip.src))}:{tcp.sport}"
                    dst = f"{self._fmt_ip(bytes(ip.dst))}:{tcp.dport}"
                    key = (src, dst)
                    payload = bytes(tcp.data)
                    if not payload:
                        continue

                    # Accumulate stream data
                    streams[key] = streams.get(key, b"") + payload

                    # Try to parse WebSocket frames from accumulated buffer
                    buf = streams[key]
                    consumed, frames_parsed = self._parse_ws_frames(buf, ts, src, dst)
                    results.extend(frames_parsed)
                    streams[key] = buf[consumed:]

                except Exception:
                    if self._strict:
                        raise

        return results

    def _parse_ws_frames(
        self,
        buf: bytes,
        ts: float,
        src: str,
        dst: str,
    ) -> tuple[int, list[ScBACnetFrame]]:
        """Parse as many complete WebSocket frames as possible from *buf*.

        Returns ``(bytes_consumed, frames)``.
        """
        frames: list[ScBACnetFrame] = []
        offset = 0

        while offset < len(buf):
            if len(buf) - offset < 2:
                break
            b0 = buf[offset]
            b1 = buf[offset + 1]
            fin = (b0 & 0x80) != 0  # noqa: F841  (reserved for reassembly)
            opcode = b0 & 0x0F
            masked = (b1 & 0x80) != 0
            length = b1 & 0x7F

            header_len = 2
            if length == 126:
                if len(buf) - offset < 4:
                    break
                (length,) = struct.unpack(">H", buf[offset + 2 : offset + 4])
                header_len = 4
            elif length == 127:
                if len(buf) - offset < 10:
                    break
                (length,) = struct.unpack(">Q", buf[offset + 2 : offset + 10])
                header_len = 10

            if masked:
                header_len += 4

            total = header_len + length
            if len(buf) - offset < total:
                break

            payload = buf[offset + header_len : offset + total]
            if masked:
                mask = buf[offset + header_len - 4 : offset + header_len]
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            frame = ScBACnetFrame(
                timestamp=ts,
                src=src,
                dst=dst,
                ws_opcode=WS_OPCODES.get(opcode, f"0x{opcode:X}"),
                ws_opcode_code=opcode,
                raw=payload,
                decrypted=False,  # plaintext capture, not "decrypted" per se
            )
            if opcode == 0x2 and payload:  # binary — attempt BVLC-SC
                self._parse_bvlc_sc(payload, frame)

            frames.append(frame)
            offset += total

        return offset, frames

    # ------------------------------------------------------------------
    # BVLC-SC / NPDU parser
    # ------------------------------------------------------------------

    def _parse_bvlc_sc(self, data: bytes, frame: ScBACnetFrame) -> None:
        """Parse the BVLC-SC fixed header (6 bytes minimum).

        Structure (Annex YY)::

            Byte 0: BVLC function
            Byte 1: control flags
            Bytes 2-3: message length (big-endian, includes 4-byte BVLC-SC fixed header)
            Bytes 4-5: message ID (big-endian)
            [Optional header entries follow, then NPDU payload]
        """
        if len(data) < 6:
            return
        func = data[0]
        flags = data[1]
        bvlc_len = struct.unpack(">H", data[2:4])[0]  # noqa: F841
        msg_id = struct.unpack(">H", data[4:6])[0]

        frame.bvlc_sc_function_code = func
        frame.bvlc_sc_function = BVLC_SC_FUNCTIONS.get(func, f"0x{func:02X}")
        frame.bvlc_sc_flags = flags
        frame.message_id = msg_id

        # Optional header entries: skip them (bit 0 of flags = "more header entries")
        offset = 6
        while (flags & 0x01) and offset + 3 <= len(data):
            opt_type = data[offset]  # noqa: F841
            opt_len = struct.unpack(">H", data[offset + 1 : offset + 3])[0]
            offset += 3 + opt_len
            # Each option's "more" flag is in bit 0 of opt_type
            # For simplicity treat as opaque and stop after one
            break

        # Remaining bytes are NPDU (for Encapsulated-NPDU messages)
        if func == 0x01 and len(data) > offset:
            frame.npdu_raw = data[offset:]
            self._parse_npdu_apdu(data[offset:], frame)

    def _parse_npdu_apdu(self, data: bytes, frame: ScBACnetFrame) -> None:
        """Parse NPDU flags and extract the APDU type and service name."""
        # Import the APDU tables from sibling module to avoid duplication
        try:
            from protoskipper.builtin_drivers.bacnet.pcap import (
                APDU_TYPES,
                CONFIRMED_SERVICES,
                UNCONFIRMED_SERVICES,
            )
        except ImportError:
            return

        if len(data) < 2:
            return
        npdu_flags = data[1]
        offset = 2

        # Skip DNET / DADR / SNET / SADR / hop-count if present
        if npdu_flags & 0x20:  # DNET
            if offset + 2 > len(data):
                return
            offset += 3
            if offset <= len(data):
                dlen = data[offset - 1]
                offset += dlen
        if npdu_flags & 0x08:  # SNET
            if offset + 2 > len(data):
                return
            offset += 3
            if offset <= len(data):
                slen = data[offset - 1]
                offset += slen
        if npdu_flags & 0x20:
            offset += 1  # hop count

        if npdu_flags & 0x80:
            return  # network-layer message, no APDU

        if len(data) <= offset:
            return

        apdu = data[offset:]
        pdu_type = (apdu[0] >> 4) & 0x0F
        frame.apdu_type_code = pdu_type
        frame.apdu_type = APDU_TYPES.get(pdu_type, f"type-{pdu_type}")

        if pdu_type == 0 and len(apdu) >= 4:
            service = apdu[3]
            frame.service = CONFIRMED_SERVICES.get(service, f"confirmed-{service}")
        elif pdu_type == 1 and len(apdu) >= 2:
            service = apdu[1]
            frame.service = UNCONFIRMED_SERVICES.get(service, f"unconfirmed-{service}")
        elif pdu_type in {2, 3} and len(apdu) >= 3:
            service = apdu[2]
            frame.service = CONFIRMED_SERVICES.get(service, f"ack-service-{service}")

    @staticmethod
    def _fmt_ip(addr: bytes) -> str:
        return ".".join(str(b) for b in addr)
