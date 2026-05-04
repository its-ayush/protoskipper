# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for the IEC 104 scripting façade.

These run fully offline — no real connections are made.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from protoskipper.builtin_drivers.iec104.asdu import TypeID
from protoskipper.builtin_drivers.iec104.scripting import (
    Fuzzer,
    MasterSession,
    PcapReader,
    SlaveServer,
    _resolve_type_id,
)

# ---------------------------------------------------------------------------
# _resolve_type_id
# ---------------------------------------------------------------------------


def test_resolve_type_id_from_string() -> None:
    assert _resolve_type_id("M_SP_NA_1") is TypeID.M_SP_NA_1


def test_resolve_type_id_from_int() -> None:
    assert _resolve_type_id(1) is TypeID.M_SP_NA_1


def test_resolve_type_id_from_enum() -> None:
    assert _resolve_type_id(TypeID.M_SP_NA_1) is TypeID.M_SP_NA_1


def test_resolve_type_id_invalid_string() -> None:
    with pytest.raises(ValueError, match="Unknown TypeID"):
        _resolve_type_id("INVALID_TYPE")


def test_resolve_type_id_invalid_int() -> None:
    with pytest.raises(ValueError, match="Unknown TypeID"):
        _resolve_type_id(9999)


# ---------------------------------------------------------------------------
# MasterSession context manager (no real connection)
# ---------------------------------------------------------------------------


def test_master_session_connect_and_close_via_context_manager() -> None:
    """MasterSession.__enter__/__exit__ must call connect() and close()."""
    mock_inner = MagicMock()
    mock_inner.started = True

    with patch(
        "protoskipper.builtin_drivers.iec104.scripting.Iec104MasterSession",
        return_value=mock_inner,
    ) as mock_class:
        ms = MasterSession("127.0.0.1", allow_writes=True)
        with ms:
            mock_class.assert_called_once()
            mock_inner.connect.assert_called_once()

        mock_inner.close.assert_called_once()


def test_master_session_command_requires_allow_writes() -> None:
    """command() must raise PermissionError when allow_writes=False."""
    ms = MasterSession("127.0.0.1", allow_writes=False)
    with pytest.raises(PermissionError, match="allow_writes"):
        ms.command(100, "C_SC_NA_1", value=True)


def test_master_session_no_connection_raises() -> None:
    """Calling gi() before connect() must raise RuntimeError."""
    ms = MasterSession("127.0.0.1", allow_writes=True)
    with pytest.raises(RuntimeError, match="Not connected"):
        ms.gi()


def test_master_session_subscribe_requires_connection() -> None:
    """subscribe() delegates to set_spontaneous_listener, which requires connection."""
    ms = MasterSession("127.0.0.1", allow_writes=True)
    cb = MagicMock()
    # subscribe() requires an active session
    with pytest.raises(RuntimeError, match="Not connected"):
        ms.subscribe(cb)


# ---------------------------------------------------------------------------
# SlaveServer production guard
# ---------------------------------------------------------------------------


def test_slave_server_start_blocked_in_production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """start() must raise RuntimeError when PROTOSKIPPER_PROFILE=PRODUCTION."""
    monkeypatch.setenv("PROTOSKIPPER_PROFILE", "PRODUCTION")
    ss = SlaveServer(port=0)
    with pytest.raises(RuntimeError, match="PRODUCTION"):
        ss.start()


def test_slave_server_start_allowed_with_allow_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start() must proceed when allow_in_production=True (guard bypassed)."""
    monkeypatch.delenv("PROTOSKIPPER_PROFILE", raising=False)
    mock_server = MagicMock()

    with patch(
        "protoskipper.builtin_drivers.iec104.scripting.Iec104SlaveServer",
        return_value=mock_server,
    ):
        ss = SlaveServer(port=0, allow_in_production=True)
        ss.start()
        mock_server.start.assert_called_once()


# ---------------------------------------------------------------------------
# PcapReader.summarize
# ---------------------------------------------------------------------------


def test_pcap_reader_summarize_delegates_to_summarize_pcap(
    tmp_path: pytest.TempPathFactory,
) -> None:
    dummy = tmp_path / "dummy.pcap"  # type: ignore[operator]
    dummy.write_bytes(b"")

    with patch(
        "protoskipper.builtin_drivers.iec104.scripting.summarize_pcap",
        return_value={"I": 5, "S": 2},
    ) as mock_fn:
        result = PcapReader.summarize(dummy)
        mock_fn.assert_called_once()
        assert result == {"I": 5, "S": 2}


def test_pcap_reader_iter_wraps_iter_iec104_frames(tmp_path: pytest.TempPathFactory) -> None:
    dummy = tmp_path / "test.pcap"  # type: ignore[operator]
    dummy.write_bytes(b"")
    frame = MagicMock()

    with patch(
        "protoskipper.builtin_drivers.iec104.scripting.iter_iec104_frames",
        return_value=iter([frame]),
    ):
        frames = list(PcapReader(dummy))
        assert frames == [frame]


# ---------------------------------------------------------------------------
# Fuzzer (offline, fast)
# ---------------------------------------------------------------------------


def test_fuzzer_codec_roundtrip_no_crashes() -> None:
    report = Fuzzer.codec_roundtrip(iterations=50, seed=42)
    assert report.iterations == 50
    assert report.unexpected_errors == 0


def test_fuzzer_asdu_codec_no_crashes() -> None:
    report = Fuzzer.asdu_codec(iterations=50, seed=7)
    assert report.iterations == 50
    assert report.unexpected_errors == 0


def test_fuzzer_apdu_mutation_no_crashes() -> None:
    report = Fuzzer.apdu_mutation(iterations=50, seed=1)
    assert report.iterations == 50


# ---------------------------------------------------------------------------
# scripting/__init__.py: _make_iec104_ns
# ---------------------------------------------------------------------------


def test_make_iec104_ns_allow_writes_false_restricts_slave() -> None:
    from protoskipper.scripting import _make_iec104_ns

    ns = _make_iec104_ns(allow_writes=False)
    assert ns is not None

    # The SlaveServer returned must raise on start()
    srv = ns.SlaveServer(port=0)  # type: ignore[union-attr]
    with pytest.raises(PermissionError, match="--allow-writes"):
        srv.start()


def test_make_iec104_ns_allow_writes_false_master_default() -> None:
    from protoskipper.scripting import _make_iec104_ns

    ns = _make_iec104_ns(allow_writes=False)
    assert ns is not None

    ms = ns.MasterSession("127.0.0.1")  # type: ignore[union-attr]
    # Must not be allowed to issue commands by default
    with pytest.raises(PermissionError, match="allow_writes"):
        ms.command(100, "C_SC_NA_1", value=True)


def test_make_iec104_ns_allow_writes_true_exposes_classes() -> None:
    from protoskipper.scripting import _make_iec104_ns

    ns = _make_iec104_ns(allow_writes=True)
    assert ns is not None
    assert isinstance(ns, types.SimpleNamespace)
    assert hasattr(ns, "MasterSession")
    assert hasattr(ns, "SlaveServer")
    assert hasattr(ns, "PcapReader")
    assert hasattr(ns, "Fuzzer")
