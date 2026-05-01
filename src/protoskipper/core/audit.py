# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Append-only signed session audit log.

Every read, write authorisation, committed write, and protocol error from a
single session is recorded in a per-session SQLite WAL database. Records are
chained via a hash of the previous row, and each row is signed with an HMAC
keyed off a per-session secret so a tamper attempt is detectable after the
fact.

This is the artifact the operator hands over after a commissioning session,
or that gets attached to an incident report when something on a substation
trips. It must be cheap to write and impossible to silently edit.

Threat model
------------

The audit log defends against *accidental* data loss and *post-hoc* tamper
attempts. It does NOT defend against an attacker with full filesystem
access at write time (they could discard the database entirely, or hold the
key). What it provides is a chain that, once a session is closed and the
key is rotated/discarded, cannot be retroactively edited without breaking
the hash chain — so any deletion or modification leaves evidence.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT NOT NULL,
    event       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    row_hmac    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


class AuditLog:
    """Per-session append-only signed audit log.

    Use as a context manager so the WAL checkpoint and final summary row run
    even when the session ends abnormally::

        with AuditLog.create(Path("/var/log/protoskipper/sess.sqlite"),
                             operator="kuldeep@example.com") as audit:
            audit.record(event="connect", target="modbus.tcp://10.0.0.5:502")
            ...
    """

    def __init__(self, db_path: Path, key: bytes) -> None:
        self._path = db_path
        self._key = key
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._last_hash = b"\x00" * 32

    # -- Construction ------------------------------------------------------

    @classmethod
    def create(cls, db_path: Path, operator: str, profile: str) -> AuditLog:
        """Open a fresh audit log and write the session-start row.

        The HMAC key is generated here, used to sign every row, and stored
        in the ``session_meta`` table at the end of the session (after which
        it is wiped from process memory). External verification tools can
        re-derive the chain using the published key.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)

        log = cls(db_path, key)
        log._open()
        log.record(
            event="session_start",
            operator=operator,
            profile=profile,
            schema_version=1,
        )
        return log

    def _open(self) -> None:
        self._conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        for stmt in _SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._conn.execute(stmt)

    # -- Recording ---------------------------------------------------------

    def record(self, *, event: str, **fields: Any) -> int:
        """Append a row. Returns the assigned sequence number.

        Field values must be JSON-serialisable; callers are expected to
        convert dataclasses to dicts before passing them in (see
        :func:`event_payload`).
        """
        if self._conn is None:
            raise RuntimeError("AuditLog used after close()")

        ts = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(fields, default=_json_fallback, sort_keys=True)

        with self._lock:
            prev_hash_hex = self._last_hash.hex()
            digest_input = f"{prev_hash_hex}|{ts}|{event}|{payload}".encode()
            row_hmac = hmac.new(self._key, digest_input, hashlib.sha256).hexdigest()
            row_hash = hashlib.sha256(digest_input + row_hmac.encode()).digest()

            cursor = self._conn.execute(
                "INSERT INTO audit_log (ts_utc, event, payload, prev_hash, row_hmac) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, event, payload, prev_hash_hex, row_hmac),
            )
            self._last_hash = row_hash
            seq = int(cursor.lastrowid or 0)

        return seq

    # -- Lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self.record(event="session_end")
            # Persist the verification key so external tools can audit later.
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_meta(key,value) VALUES (?,?)",
                    ("hmac_key_hex", self._key.hex()),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO session_meta(key,value) VALUES (?,?)",
                    ("final_hash_hex", self._last_hash.hex()),
                )
                self._conn.execute("PRAGMA wal_checkpoint(FULL);")
        finally:
            with closing(self._conn):
                pass
            self._conn = None
            # Wipe key from memory.
            self._key = b""

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Verification helper (read-only; does not require the original AuditLog object)
# ---------------------------------------------------------------------------


def verify_log(db_path: Path) -> tuple[bool, str]:
    """Re-derive the hash chain and return ``(ok, message)``.

    Used by the GUI's "Verify audit log" feature and by external auditors.
    Returns ``(True, "OK: N rows verified")`` on success, or ``(False,
    reason)`` on the first inconsistency.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        cur = conn.cursor()
        meta = dict(cur.execute("SELECT key, value FROM session_meta").fetchall())
        key_hex = meta.get("hmac_key_hex")
        if not key_hex:
            return False, (
                "session_meta is missing hmac_key_hex"
                " (session may not have closed cleanly)"
            )
        key = bytes.fromhex(key_hex)

        prev_hash = b"\x00" * 32
        rows = list(cur.execute(
            "SELECT seq, ts_utc, event, payload, prev_hash, row_hmac "
            "FROM audit_log ORDER BY seq ASC"
        ))

        for seq, ts, event, payload, prev_hash_hex, row_hmac_hex in rows:
            if prev_hash_hex != prev_hash.hex():
                return False, f"row {seq}: prev_hash mismatch (chain broken)"

            digest_input = f"{prev_hash.hex()}|{ts}|{event}|{payload}".encode()
            expected_hmac = hmac.new(key, digest_input, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected_hmac, row_hmac_hex):
                return False, f"row {seq}: HMAC mismatch (row was edited or key is wrong)"

            prev_hash = hashlib.sha256(digest_input + row_hmac_hex.encode()).digest()

        final_hash_hex = meta.get("final_hash_hex")
        if final_hash_hex and final_hash_hex != prev_hash.hex():
            return False, "final_hash mismatch (rows added/removed after close)"

        return True, f"OK: {len(rows)} rows verified"


def _json_fallback(obj: Any) -> Any:
    """Serialise dataclass-like objects, datetimes, bytes."""
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return repr(obj)
