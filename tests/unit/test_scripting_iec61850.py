# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the IEC 61850 scripting namespace (P8.I.4)."""

from __future__ import annotations

import types

import pytest

from protoskipper.scripting.iec61850 import Session, make_iec61850_ns


class TestMakeIec61850Ns:
    def test_returns_namespace(self):
        ns = make_iec61850_ns()
        assert isinstance(ns, types.SimpleNamespace)

    def test_has_session_class(self):
        ns = make_iec61850_ns()
        # Session may be Session itself (allow_writes=True) or a safe subclass
        assert issubclass(ns.Session, Session)

    def test_has_goose_attr(self):
        ns = make_iec61850_ns()
        assert ns.Goose is not None

    def test_has_sv_attr(self):
        ns = make_iec61850_ns()
        assert ns.Sv is not None

    def test_has_scd_attr(self):
        ns = make_iec61850_ns()
        assert ns.Scd is not None

    def test_has_dissect_attr(self):
        ns = make_iec61850_ns()
        assert ns.dissect is not None

    def test_has_conformance_attr(self):
        ns = make_iec61850_ns()
        assert ns.conformance is not None

    def test_has_simulator_attr(self):
        ns = make_iec61850_ns()
        assert ns.simulator is not None

    def test_allow_writes_false_by_default(self):
        ns = make_iec61850_ns()
        # The Session subclass must enforce allow_writes=False
        s = ns.Session("192.168.1.1")
        assert s._allow_writes is False

    def test_scripting_ns_includes_iec61850(self):
        """The global scripting namespace factory injects iec61850."""
        from pathlib import Path

        from protoskipper.scripting import _make_namespace

        ns = _make_namespace(Path("/tmp/dummy.py"), [], allow_writes=False)
        assert "iec61850" in ns
        assert ns["iec61850"] is not None


class TestSession:
    def test_construction_defaults(self):
        s = Session("192.168.1.1")
        assert s._host == "192.168.1.1"
        assert s._port == 102
        assert s._allow_writes is False
        assert s._client is None

    def test_write_blocked_when_not_allowed(self):
        s = Session("192.168.1.1", allow_writes=False)
        with pytest.raises(PermissionError, match="allow_writes=True"):
            s.write("IED1LD0/MMXU1.TotW.mag.f", 42.0)

    def test_write_allowed_when_permitted(self):
        """write() forwards to client when allowed and client exists."""
        s = Session("192.168.1.1", allow_writes=True)
        calls: list[tuple] = []

        class _FakeClient:
            def write(self, ref, value, *, fc):  # noqa: ANN
                calls.append((ref, value, fc))

        s._client = _FakeClient()
        s.write("IED1LD0/MMXU1.TotW.mag.f", 99.5, fc="SP")
        assert calls == [("IED1LD0/MMXU1.TotW.mag.f", 99.5, "SP")]

    def test_read_requires_connection(self):
        s = Session("192.168.1.1")
        with pytest.raises(RuntimeError, match="not connected"):
            s.read("IED1LD0/MMXU1.TotW.mag.f")

    def test_read_forwards_to_client(self):
        s = Session("192.168.1.1")

        class _FakeClient:
            def read(self, ref, *, fc):  # noqa: ANN
                return f"{ref}@{fc}"

        s._client = _FakeClient()
        result = s.read("IED1LD0/MMXU1.TotW.mag.f", fc="MX")
        assert result == "IED1LD0/MMXU1.TotW.mag.f@MX"

    def test_close_clears_client(self):
        s = Session("192.168.1.1")

        class _FakeClient:
            def close(self):  # noqa: ANN
                pass

        s._client = _FakeClient()
        s.close()
        assert s._client is None

    def test_close_is_idempotent(self):
        s = Session("192.168.1.1")
        s.close()  # should not raise

    def test_context_manager_calls_close(self):
        closed: list[bool] = []

        class _FakeClient:
            def close(self):  # noqa: ANN
                closed.append(True)

        s = Session("192.168.1.1")
        s._client = _FakeClient()
        # Simulate __exit__ without a real connection
        s.__exit__(None, None, None)
        assert closed == [True]

    def test_get_server_directory_requires_connection(self):
        s = Session("192.168.1.1")
        with pytest.raises(RuntimeError, match="not connected"):
            s.get_server_directory()

    def test_get_server_directory_delegates(self):
        s = Session("192.168.1.1")

        class _FakeClient:
            def get_server_directory(self):  # noqa: ANN
                return ["LD1", "LD2"]

        s._client = _FakeClient()
        assert s.get_server_directory() == ["LD1", "LD2"]


class TestSvNs:
    def test_decode_frame_delegates(self):
        from protoskipper.scripting.iec61850 import _build_sv_ns

        sv = _build_sv_ns()
        # Minimal valid-ish frame — just checking delegation, not correctness
        result = sv.decode_frame(b"\xff" * 20)
        # Should return None (invalid frame) without raising
        assert result is None

    def test_decode_frame_with_valid_stub(self):
        from protoskipper.scripting.iec61850 import _build_sv_ns

        sv = _build_sv_ns()
        # Build a minimal SV Ethernet frame with correct ethertype 0x88BA
        dst = b"\x01\x0c\xcd\x04\x00\x00"
        src = b"\x00\x11\x22\x33\x44\x55"
        ethertype = b"\x88\xba"
        # Remaining bytes don't need to be valid; decode_sv_frame returns None
        frame = dst + src + ethertype + b"\x00" * 20
        result = sv.decode_frame(frame)
        # Either None or a dissection object — no exception
        assert result is None or hasattr(result, "no_asdu")


class TestScdNs:
    def test_parse_raises_on_missing_file(self):
        from protoskipper.scripting.iec61850 import _build_scd_ns

        scd = _build_scd_ns()
        with pytest.raises(Exception):  # noqa: B017  # FileNotFoundError or lxml error
            scd.parse("/nonexistent/path/file.scd")

    def test_scd_document_class_accessible(self):
        from protoskipper.scripting.iec61850 import _build_scd_ns

        scd = _build_scd_ns()
        cls = scd.SclDocument
        assert cls is not None
