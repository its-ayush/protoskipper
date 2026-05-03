# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Tiny in-process IEC 60870-5-104 slave for integration tests.

Implements *just enough* of the protocol to exercise the master state
machine: STARTDT/STOPDT, TESTFR, S-frame acks, station interrogation
(returns one M_ME_NC_1 ASDU then ACTTERM), single-command (replies with
ACTCON), clock sync (replies with ACTCON), read command (replies with
M_ME_NC_1 carrying a configured float).

This is **NOT** a production slave (see ``docs/IEC104_PLAN.md`` P4.C for
that). It exists solely to keep the master test suite hermetic.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from datetime import datetime, timezone

from protoskipper.builtin_drivers.iec104.apci import (
    FrameFormat,
    UType,
    build_i_frame,
    build_u_frame,
    parse_apdu,
    peek_apdu_length,
    seq_inc,
)
from protoskipper.builtin_drivers.iec104.asdu import (
    COT,
    Asdu,
    BinaryCounter,
    InformationObject,
    Quality,
    TypeID,
    decode_asdu,
    encode_asdu,
)


class MiniSlave:
    """Minimal IEC 104 slave bound to ``127.0.0.1:<ephemeral>``."""

    def __init__(self, ca: int = 1, value: float = 230.5) -> None:
        self.ca = ca
        self.value = value
        # Counter values returned for C_CI_NA_1 (counter interrogation).
        self.counters: dict[int, BinaryCounter] = {
            7001: BinaryCounter(count=12345, sequence=1),
            7002: BinaryCounter(count=67890, sequence=2),
        }
        # Last accepted set-point per IOA — tests can inspect.
        self.set_points: dict[int, object] = {}
        # Last accepted bitstring command per IOA.
        self.bitstrings: dict[int, int] = {}
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="iec104-mini-slave", daemon=True)
        self._client: socket.socket | None = None
        self._ns = 0
        self._nr = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()
        c = self._client
        if c is not None:
            with contextlib.suppress(OSError):
                c.close()
        self._thread.join(timeout=2.0)

    # ------------------------------------------------------------------

    def _serve(self) -> None:
        try:
            self._sock.settimeout(2.0)
            client, _ = self._sock.accept()
        except OSError:
            return
        self._client = client
        client.settimeout(0.5)
        buf = bytearray()
        started = False
        try:
            while not self._stop.is_set():
                try:
                    chunk = client.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                while True:
                    try:
                        total = peek_apdu_length(bytes(buf[:2]))
                    except Exception:
                        return
                    if total is None or len(buf) < total:
                        break
                    raw = bytes(buf[:total])
                    del buf[:total]
                    apdu = parse_apdu(raw)
                    if apdu.fmt is FrameFormat.U:
                        if apdu.utype is UType.STARTDT_ACT:
                            client.sendall(build_u_frame(UType.STARTDT_CON))
                            started = True
                        elif apdu.utype is UType.STOPDT_ACT:
                            client.sendall(build_u_frame(UType.STOPDT_CON))
                            started = False
                        elif apdu.utype is UType.TESTFR_ACT:
                            client.sendall(build_u_frame(UType.TESTFR_CON))
                    elif apdu.fmt is FrameFormat.S:
                        pass  # ignore peer S-acks
                    elif apdu.fmt is FrameFormat.I and started:
                        # Track received N(S).
                        self._nr = seq_inc(self._nr)
                        try:
                            asdu = decode_asdu(apdu.asdu)
                        except Exception:
                            continue
                        self._respond(client, asdu)
        finally:
            with contextlib.suppress(OSError):
                client.close()

    def _respond(self, client: socket.socket, req: Asdu) -> None:
        if req.type_id is TypeID.C_IC_NA_1:
            # ACTCON
            self._send_i(
                client,
                Asdu(
                    type_id=TypeID.C_IC_NA_1,
                    cot=COT.ACTCON,
                    ca=self.ca,
                    objects=[InformationObject(ioa=0, value=20)],
                ),
            )
            # one M_ME_NC_1 in INTROGEN
            self._send_i(
                client,
                Asdu(
                    type_id=TypeID.M_ME_NC_1,
                    cot=COT.INTROGEN,
                    ca=self.ca,
                    objects=[
                        InformationObject(ioa=4001, value=float(self.value), quality=Quality())
                    ],
                ),
            )
            # ACTTERM
            self._send_i(
                client,
                Asdu(
                    type_id=TypeID.C_IC_NA_1,
                    cot=COT.ACTTERM,
                    ca=self.ca,
                    objects=[InformationObject(ioa=0, value=20)],
                ),
            )
        elif req.type_id is TypeID.C_RD_NA_1:
            ioa = req.objects[0].ioa
            self._send_i(
                client,
                Asdu(
                    type_id=TypeID.M_ME_NC_1,
                    cot=COT.REQ,
                    ca=self.ca,
                    objects=[
                        InformationObject(ioa=ioa, value=float(self.value), quality=Quality())
                    ],
                ),
            )
        elif req.type_id in (TypeID.C_SC_NA_1, TypeID.C_DC_NA_1):
            # SBO commands: same ACTCON for both select and execute phases.
            # Real RTUs differentiate, but the mini-slave just confirms.
            self._send_i(
                client,
                Asdu(
                    type_id=req.type_id,
                    cot=COT.ACTCON,
                    ca=self.ca,
                    objects=req.objects,
                ),
            )
        elif req.type_id in (TypeID.C_SE_NA_1, TypeID.C_SE_NB_1, TypeID.C_SE_NC_1):
            # Record the set-point (only on execute phase, i.e. select=False).
            obj = req.objects[0]
            if not obj.select:
                self.set_points[obj.ioa] = obj.value
            self._send_i(
                client,
                Asdu(
                    type_id=req.type_id,
                    cot=COT.ACTCON,
                    ca=self.ca,
                    objects=req.objects,
                ),
            )
        elif req.type_id is TypeID.C_BO_NA_1:
            obj = req.objects[0]
            self.bitstrings[obj.ioa] = int(obj.value)
            self._send_i(
                client,
                Asdu(
                    type_id=TypeID.C_BO_NA_1,
                    cot=COT.ACTCON,
                    ca=self.ca,
                    objects=req.objects,
                ),
            )
        elif req.type_id is TypeID.C_CI_NA_1:
            # ACTCON
            self._send_i(
                client,
                Asdu(
                    type_id=TypeID.C_CI_NA_1,
                    cot=COT.ACTCON,
                    ca=self.ca,
                    objects=req.objects,
                ),
            )
            # M_IT_NA_1 with each counter
            self._send_i(
                client,
                Asdu(
                    type_id=TypeID.M_IT_NA_1,
                    cot=COT.REQCOGEN,
                    ca=self.ca,
                    objects=[
                        InformationObject(ioa=ioa, value=bcr) for ioa, bcr in self.counters.items()
                    ],
                ),
            )
            # ACTTERM
            self._send_i(
                client,
                Asdu(
                    type_id=TypeID.C_CI_NA_1,
                    cot=COT.ACTTERM,
                    ca=self.ca,
                    objects=req.objects,
                ),
            )
        elif req.type_id is TypeID.C_CS_NA_1:
            self._send_i(
                client,
                Asdu(
                    type_id=TypeID.C_CS_NA_1,
                    cot=COT.ACTCON,
                    ca=self.ca,
                    objects=[
                        InformationObject(
                            ioa=0,
                            value=datetime.now(tz=timezone.utc),
                            timestamp=datetime.now(tz=timezone.utc),
                        )
                    ],
                ),
            )

    def _send_i(self, client: socket.socket, asdu: Asdu) -> None:
        body = encode_asdu(asdu)
        frame = build_i_frame(send_seq=self._ns, recv_seq=self._nr, asdu=body)
        self._ns = seq_inc(self._ns)
        with contextlib.suppress(OSError):
            client.sendall(frame)
        # tiny pause so frames arrive separately and the master's recv loop ticks
        time.sleep(0.005)
