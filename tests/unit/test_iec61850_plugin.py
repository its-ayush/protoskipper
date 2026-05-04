# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for the IEC 61850 plugin.

Covers: P8.A.1 / P8.B.2 / P8.B.3 / P8.B.4 / P8.B.5 / P8.B.6 contract.

Tests verify that:
* The plugin package imports cleanly.
* ``Iec61850MmsDriver`` satisfies the ``ProtocolDriver`` contract.
* ``protoskipper.core.plugin_loader`` discovers ``iec61850.mms`` when the
  plugin is installed.
* ``parse_address`` accepts valid addresses and rejects invalid ones.
* ``discover`` is a generator that yields nothing (scaffold stage).
* ``connect`` (P8.B.2):
  - raises ``ImportError`` (with build instructions) when pyiec61850 is absent.
  - raises ``ConnectionFailure`` when MMS Initiate is refused.
  - returns a live :class:`Iec61850MmsSession` on success.
* :class:`Iec61850MmsSession` exposes ``negotiated_pdu_size`` and
  ``peer_implementation``; ``close()`` delegates to ``MmsClient.close()``.
* ``enumerate_objects`` (P8.B.3):
  - yields nothing when the session has no active client.
  - walks LD -> LN -> DO via MmsClient directory methods.
  - skips branches where a directory call raises ``MmsDirectoryError``.
  - emits ``ObjectRef`` with ``object_id = "LD/LN.DO"`` and ``data_type = "do"``.
* ``read`` (P8.B.4):
  - DO-level refs: tries FC_MX -> FC_ST -> FC_SP; reads q and t sub-attrs.
  - DA-level refs with ``[FC]`` suffix: single call, quality=UNKNOWN.
  - Maps quality bits to Quality enum (GOOD/BAD/UNCERTAIN/SIMULATED).
  - Returns BAD ReadResult (no raise) on all-FC-fail or no client.
* ``prepare_write`` / ``commit_write`` (P8.B.5):
  - ``prepare_write`` encodes bool/int/float; raises EncodingError for other types.
  - ``commit_write`` calls ``require_write_authorization``; denied -> no transmission.
  - Supports all four control models (direct-normal, direct-enhanced, SBO-normal,
    SBO-enhanced) by delegating to ``MmsClient.write_control``.
  - ``record_write_outcome`` is called exactly once for every attempted transmission.
  - Returns failure WriteResult (no raise) on operate failure, with AddCause in metadata.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_plugin_loader() -> None:
    """Clear the LRU cache so re-discovery picks up the installed plugin."""
    from protoskipper.core import plugin_loader

    plugin_loader.reload()


# ---------------------------------------------------------------------------
# P8.A.1 — plugin discovery
# ---------------------------------------------------------------------------


class TestIec61850PluginDiscovery:
    """Plugin registers correctly in the entry-point group."""

    def test_package_imports(self) -> None:
        import protoskipper_iec61850  # noqa: F401

    def test_driver_class_importable(self) -> None:
        from protoskipper_iec61850.driver import Iec61850MmsDriver  # noqa: F401

    def test_protocol_id(self) -> None:
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        assert Iec61850MmsDriver.PROTOCOL_ID == "iec61850.mms"

    def test_display_name(self) -> None:
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        assert "61850" in Iec61850MmsDriver.DISPLAY_NAME

    def test_plugin_loader_discovers_iec61850(self) -> None:
        _reload_plugin_loader()
        from protoskipper.core.plugin_loader import load_protocol_drivers

        drivers = load_protocol_drivers()
        assert "iec61850.mms" in drivers

    def test_discovered_driver_is_correct_class(self) -> None:
        _reload_plugin_loader()
        from protoskipper.core.driver import ProtocolDriver
        from protoskipper.core.plugin_loader import load_protocol_drivers

        # load_protocol_drivers returns dict[str, type[ProtocolDriver]] (classes, not instances)
        drivers = load_protocol_drivers()
        cls = drivers["iec61850.mms"]
        assert issubclass(cls, ProtocolDriver)
        assert cls.PROTOCOL_ID == "iec61850.mms"
        assert cls.__name__ == "Iec61850MmsDriver"

    def test_driver_is_protocol_driver_subclass(self) -> None:
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        from protoskipper.core.driver import ProtocolDriver

        assert issubclass(Iec61850MmsDriver, ProtocolDriver)


# ---------------------------------------------------------------------------
# P8.A.1 — parse_address
# ---------------------------------------------------------------------------


class TestParseAddress:
    @pytest.fixture
    def driver(self):  # type: ignore[no-untyped-def]
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        return Iec61850MmsDriver()

    def test_host_only(self, driver) -> None:  # type: ignore[no-untyped-def]
        ref = driver.parse_address("192.168.1.10")
        assert ref.protocol == "iec61850.mms"
        assert "192.168.1.10:102" in ref.address

    def test_host_and_port(self, driver) -> None:  # type: ignore[no-untyped-def]
        ref = driver.parse_address("10.0.0.5:102")
        assert "10.0.0.5:102" in ref.address

    def test_custom_port(self, driver) -> None:  # type: ignore[no-untyped-def]
        ref = driver.parse_address("10.0.0.5:10102")
        assert "10102" in ref.address

    def test_with_query_params(self, driver) -> None:  # type: ignore[no-untyped-def]
        ref = driver.parse_address("10.0.0.5:102?edition=2.1")
        assert "edition=2.1" in ref.address

    def test_label_is_host(self, driver) -> None:  # type: ignore[no-untyped-def]
        ref = driver.parse_address("myied.local:102")
        assert ref.label == "myied.local"

    def test_port_zero_invalid(self, driver) -> None:  # type: ignore[no-untyped-def]
        from protoskipper.core.errors import EncodingError

        with pytest.raises(EncodingError):
            driver.parse_address("10.0.0.1:0")

    def test_port_too_large_invalid(self, driver) -> None:  # type: ignore[no-untyped-def]
        from protoskipper.core.errors import EncodingError

        with pytest.raises(EncodingError):
            driver.parse_address("10.0.0.1:99999")


# ---------------------------------------------------------------------------
# P8.A.1 — discover (scaffold: yields nothing)
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_discover_yields_nothing(self) -> None:
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        driver = Iec61850MmsDriver()
        results = list(driver.discover("192.168.1.0/24"))
        assert results == []


# ---------------------------------------------------------------------------
# P8.B.2 — connect
# ---------------------------------------------------------------------------


class TestConnect:
    @pytest.fixture
    def driver(self):  # type: ignore[no-untyped-def]
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        return Iec61850MmsDriver()

    @pytest.fixture
    def device_ref(self, driver):  # type: ignore[no-untyped-def]
        return driver.parse_address("10.0.0.1")

    @pytest.fixture
    def safety(self):  # type: ignore[no-untyped-def]
        from protoskipper.core.driver import SafetyContext, SessionProfile

        return SafetyContext(
            profile=SessionProfile.LAB,
            confirm_callback=lambda i, p: True,
            audit_callback=lambda **_kw: None,
        )

    def test_connect_raises_import_error_when_library_absent(
        self, driver, device_ref, safety
    ) -> None:
        """When pyiec61850 is not installed, connect propagates ImportError."""
        from unittest.mock import patch

        with (
            patch(
                "protoskipper_iec61850._mms_client._require_pyiec61850",
                side_effect=ImportError("pyiec61850 not installed"),
            ),
            pytest.raises(ImportError, match="pyiec61850"),
        ):
            driver.connect(device_ref, safety)

    def test_connect_raises_connection_failure_on_mms_error(
        self, driver, device_ref, safety
    ) -> None:
        """MmsConnectError from MmsClient is re-raised as ConnectionFailure."""
        from unittest.mock import MagicMock, patch

        from protoskipper_iec61850._mms_client import MmsConnectError

        from protoskipper.core.errors import ConnectionFailure

        mock_client = MagicMock()
        mock_client.connect.side_effect = MmsConnectError("connection refused", error_code=32768)
        with (
            patch("protoskipper_iec61850.driver.MmsClient", return_value=mock_client),
            pytest.raises(ConnectionFailure, match="connection refused"),
        ):
            driver.connect(device_ref, safety)

    def test_connect_returns_session_on_success(self, driver, device_ref, safety) -> None:
        """Successful connect returns an Iec61850MmsSession with client properties."""
        from unittest.mock import MagicMock, patch

        from protoskipper_iec61850.driver import Iec61850MmsSession

        mock_client = MagicMock()
        mock_client.connect.return_value = None
        mock_client.negotiated_pdu_size = 65000
        mock_client.peer_implementation = "libiec61850/1.6"

        with patch("protoskipper_iec61850.driver.MmsClient", return_value=mock_client):
            session = driver.connect(device_ref, safety)

        assert isinstance(session, Iec61850MmsSession)
        assert session.negotiated_pdu_size == 65000
        assert session.peer_implementation == "libiec61850/1.6"

    def test_connect_uses_host_and_port_from_device_ref(self, driver, safety) -> None:
        """MmsClient is instantiated with the host and port from DeviceRef."""
        from unittest.mock import MagicMock, patch

        mock_client = MagicMock()
        mock_client.connect.return_value = None
        mock_client.negotiated_pdu_size = 0
        mock_client.peer_implementation = ""

        device_ref = driver.parse_address("10.0.0.5:1002")
        with patch("protoskipper_iec61850.driver.MmsClient", return_value=mock_client) as mock_cls:
            driver.connect(device_ref, safety)

        mock_cls.assert_called_once_with("10.0.0.5", 1002)


# ---------------------------------------------------------------------------
# P8.B.2 / P8.B.3-P8.B.5 — session stubs and close
# ---------------------------------------------------------------------------


class TestSessionStubs:
    @pytest.fixture
    def session(self):  # type: ignore[no-untyped-def]
        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext, SessionProfile

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = SafetyContext(
            profile=SessionProfile.LAB,
            confirm_callback=lambda i, p: True,
            audit_callback=lambda **_kw: None,
        )
        return Iec61850MmsSession(device=device, safety=safety)  # no client

    def test_enumerate_objects_yields_nothing_without_client(self, session) -> None:  # type: ignore[no-untyped-def]
        """No client -> enumerate_objects is a no-op generator."""
        assert list(session.enumerate_objects()) == []

    def test_read_no_client_returns_bad(self, session) -> None:  # type: ignore[no-untyped-def]
        """read() without an active client returns BAD quality, never raises."""
        from protoskipper.core.driver import ObjectRef, Quality

        ref = ObjectRef(
            device=session.device,
            object_id="LD0/MMXU1.A",
            data_type="do",
        )
        result = session.read(ref)
        assert result.quality == Quality.BAD
        assert result.error is not None
        assert result.value is None

    def test_prepare_write_bool_intent(self, session) -> None:  # type: ignore[no-untyped-def]
        from protoskipper.core.driver import ObjectRef, WriteIntent

        ref = ObjectRef(
            device=session.device,
            object_id="LD0/XCBR1.Pos",
            data_type="do",
        )
        intent = session.prepare_write(ref, True)
        assert isinstance(intent, WriteIntent)
        assert intent.requested_value is True
        assert isinstance(intent.encoded_bytes, bytes) and len(intent.encoded_bytes) > 0

    def test_commit_write_denied_returns_failure(self, session) -> None:  # type: ignore[no-untyped-def]
        """commit_write denied by safety -> failure WriteResult, no record_write_outcome."""
        from unittest.mock import MagicMock

        from protoskipper.core.driver import ObjectRef, SafetyContext

        mock_safety = MagicMock(spec=SafetyContext)
        mock_safety.require_write_authorization.return_value = False
        session.safety = mock_safety

        ref = ObjectRef(device=session.device, object_id="LD0/XCBR1.Pos", data_type="do")
        intent = session.prepare_write(ref, True)
        result = session.commit_write(intent)
        assert result.success is False
        mock_safety.record_write_outcome.assert_not_called()

    def test_close_is_noop_without_client(self, session) -> None:  # type: ignore[no-untyped-def]
        session.close()  # must not raise

    def test_close_delegates_to_client(self) -> None:
        """close() calls MmsClient.close() exactly once."""
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext, SessionProfile

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = SafetyContext(
            profile=SessionProfile.LAB,
            confirm_callback=lambda i, p: True,
            audit_callback=lambda **_kw: None,
        )
        mock_client = MagicMock()
        session = Iec61850MmsSession(device=device, safety=safety, client=mock_client)
        session.close()
        mock_client.close.assert_called_once()

    def test_double_close_is_safe(self) -> None:
        """close() called twice does not call MmsClient.close() a second time."""
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext, SessionProfile

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = SafetyContext(
            profile=SessionProfile.LAB,
            confirm_callback=lambda i, p: True,
            audit_callback=lambda **_kw: None,
        )
        mock_client = MagicMock()
        session = Iec61850MmsSession(device=device, safety=safety, client=mock_client)
        session.close()
        session.close()
        mock_client.close.assert_called_once()

    def test_negotiated_pdu_size_without_client(self, session) -> None:  # type: ignore[no-untyped-def]
        assert session.negotiated_pdu_size == 0

    def test_peer_implementation_without_client(self, session) -> None:  # type: ignore[no-untyped-def]
        assert session.peer_implementation == ""


# ---------------------------------------------------------------------------
# P8.B.3 - enumerate_objects
# ---------------------------------------------------------------------------


class TestEnumerateObjects:
    """enumerate_objects() walks LD->LN->DO via MmsClient directory methods."""

    @pytest.fixture
    def session_with_client(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext, SessionProfile

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = SafetyContext(
            profile=SessionProfile.LAB,
            confirm_callback=lambda i, p: True,
            audit_callback=lambda **_kw: None,
        )
        mock_client = MagicMock()
        return Iec61850MmsSession(device=device, safety=safety, client=mock_client)

    def test_yields_do_refs_for_full_tree(self, session_with_client) -> None:
        """Happy path: LD0 has LLN0+MMXU1; each has one DO."""
        from protoskipper.core.driver import ObjectRef

        client = session_with_client._client
        client.get_server_directory.return_value = ["LD0"]
        client.get_logical_device_directory.return_value = ["LLN0", "MMXU1"]
        client.get_logical_node_directory.side_effect = [
            ["Mod"],  # LLN0 DOs
            ["A"],  # MMXU1 DOs
        ]

        refs = list(session_with_client.enumerate_objects())

        assert len(refs) == 2
        assert all(isinstance(r, ObjectRef) for r in refs)
        assert refs[0].object_id == "LD0/LLN0.Mod"
        assert refs[1].object_id == "LD0/MMXU1.A"

    def test_object_ref_fields(self, session_with_client) -> None:
        """Yielded ObjectRef has data_type='do', access=READ_ONLY, label=object_id."""
        from protoskipper.core.driver import Access

        client = session_with_client._client
        client.get_server_directory.return_value = ["LD0"]
        client.get_logical_device_directory.return_value = ["MMXU1"]
        client.get_logical_node_directory.return_value = ["Mod"]

        (ref,) = list(session_with_client.enumerate_objects())

        assert ref.data_type == "do"
        assert ref.access == Access.READ_ONLY
        assert ref.label == "LD0/MMXU1.Mod"
        assert ref.device is session_with_client.device

    def test_multiple_logical_devices(self, session_with_client) -> None:
        """Objects from multiple LDs are all yielded."""
        client = session_with_client._client
        client.get_server_directory.return_value = ["LD0", "CTRL"]
        client.get_logical_device_directory.side_effect = [["LLN0"], ["LLN0"]]
        client.get_logical_node_directory.side_effect = [["Mod"], ["Mod"]]

        refs = list(session_with_client.enumerate_objects())

        ids = [r.object_id for r in refs]
        assert "LD0/LLN0.Mod" in ids
        assert "CTRL/LLN0.Mod" in ids

    def test_skips_ld_on_directory_error(self, session_with_client) -> None:
        """MmsDirectoryError from get_logical_device_directory skips that LD."""
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client = session_with_client._client
        client.get_server_directory.return_value = ["LD_BAD", "LD_OK"]
        client.get_logical_device_directory.side_effect = [
            MmsDirectoryError("timeout", error_code=14),
            ["LLN0"],
        ]
        client.get_logical_node_directory.return_value = ["Mod"]

        refs = list(session_with_client.enumerate_objects())  # must not raise

        assert len(refs) == 1
        assert refs[0].object_id == "LD_OK/LLN0.Mod"

    def test_skips_ln_on_directory_error(self, session_with_client) -> None:
        """MmsDirectoryError from get_logical_node_directory skips that LN."""
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client = session_with_client._client
        client.get_server_directory.return_value = ["LD0"]
        client.get_logical_device_directory.return_value = ["LLN0", "MMXU1"]
        client.get_logical_node_directory.side_effect = [
            MmsDirectoryError("access denied", error_code=11),
            ["A"],
        ]

        refs = list(session_with_client.enumerate_objects())  # must not raise

        assert len(refs) == 1
        assert refs[0].object_id == "LD0/MMXU1.A"

    def test_returns_empty_on_server_directory_error(self, session_with_client) -> None:
        """MmsDirectoryError from get_server_directory -> empty iterator."""
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client = session_with_client._client
        client.get_server_directory.side_effect = MmsDirectoryError("not connected", error_code=1)

        refs = list(session_with_client.enumerate_objects())  # must not raise

        assert refs == []

    def test_ln_ref_format(self, session_with_client) -> None:
        """LN functional reference passed to get_logical_node_directory is 'LD/LN'."""
        client = session_with_client._client
        client.get_server_directory.return_value = ["LD0"]
        client.get_logical_device_directory.return_value = ["MMXU1"]
        client.get_logical_node_directory.return_value = ["A"]

        list(session_with_client.enumerate_objects())

        client.get_logical_node_directory.assert_called_once_with(
            "LD0/MMXU1",
            0,  # ACSI_CLASS_DATA_OBJECT = 0
        )


# ---------------------------------------------------------------------------
# P8.B.4 - read with quality and timestamp
# ---------------------------------------------------------------------------


class TestRead:
    """read() maps MmsDecodedValue fields to ReadResult quality/timestamp."""

    @pytest.fixture
    def session(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext, SessionProfile

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = SafetyContext(
            profile=SessionProfile.LAB,
            confirm_callback=lambda i, p: True,
            audit_callback=lambda **_kw: None,
        )
        mock_client = MagicMock()
        return Iec61850MmsSession(device=device, safety=safety, client=mock_client)

    @pytest.fixture
    def do_ref(self, session):  # type: ignore[no-untyped-def]
        from protoskipper.core.driver import ObjectRef

        return ObjectRef(device=session.device, object_id="LD0/MMXU1.A", data_type="do")

    def _decoded(self, **kwargs):  # type: ignore[no-untyped-def]
        from protoskipper_iec61850._mms_client import MmsDecodedValue

        return MmsDecodedValue(value=3.14, **kwargs)

    # -- DO-level reads -------------------------------------------------------

    def test_do_good_quality(self, session, do_ref) -> None:
        session._client.read_do_with_meta.return_value = self._decoded(
            quality_validity=0, is_substituted=False, is_test=False, timestamp_ms=0
        )
        from protoskipper.core.driver import Quality

        result = session.read(do_ref)
        assert result.quality == Quality.GOOD
        assert result.value == 3.14
        assert result.error is None

    def test_do_invalid_quality_returns_bad(self, session, do_ref) -> None:
        session._client.read_do_with_meta.return_value = self._decoded(
            quality_validity=1, is_substituted=False, is_test=False, timestamp_ms=0
        )
        from protoskipper.core.driver import Quality

        assert session.read(do_ref).quality == Quality.BAD

    def test_do_questionable_quality_returns_uncertain(self, session, do_ref) -> None:
        session._client.read_do_with_meta.return_value = self._decoded(
            quality_validity=3, is_substituted=False, is_test=False, timestamp_ms=0
        )
        from protoskipper.core.driver import Quality

        assert session.read(do_ref).quality == Quality.UNCERTAIN

    def test_do_substituted_returns_simulated(self, session, do_ref) -> None:
        session._client.read_do_with_meta.return_value = self._decoded(
            quality_validity=0, is_substituted=True, is_test=False, timestamp_ms=0
        )
        from protoskipper.core.driver import Quality

        assert session.read(do_ref).quality == Quality.SIMULATED

    def test_do_timestamp_from_ms(self, session, do_ref) -> None:
        """timestamp_ms=1_000_000 ms (1000 s after epoch) produces correct datetime."""
        import datetime as dt

        session._client.read_do_with_meta.return_value = self._decoded(
            quality_validity=0, is_substituted=False, is_test=False, timestamp_ms=1_000_000
        )
        result = session.read(do_ref)
        expected = dt.datetime(1970, 1, 1, 0, 16, 40, tzinfo=dt.timezone.utc)
        assert result.timestamp == expected

    def test_do_zero_ts_falls_back_to_now(self, session, do_ref) -> None:
        """timestamp_ms=0 -> timestamp is approximately now."""
        import datetime as dt

        session._client.read_do_with_meta.return_value = self._decoded(
            quality_validity=0, is_substituted=False, is_test=False, timestamp_ms=0
        )
        before = dt.datetime.now(tz=dt.timezone.utc)
        result = session.read(do_ref)
        after = dt.datetime.now(tz=dt.timezone.utc)
        assert before <= result.timestamp <= after

    def test_do_fc_fallback_mx_then_st(self, session, do_ref) -> None:
        """When FC_MX fails, read() retries with FC_ST."""
        from protoskipper_iec61850._mms_client import MmsDecodedValue, MmsDirectoryError

        session._client.read_do_with_meta.side_effect = [
            MmsDirectoryError("object not found", error_code=17),
            MmsDecodedValue(value=True, quality_validity=0),
        ]
        from protoskipper.core.driver import Quality

        result = session.read(do_ref)
        assert result.quality == Quality.GOOD
        assert result.value is True
        assert session._client.read_do_with_meta.call_count == 2

    def test_do_all_fc_fail_returns_bad(self, session, do_ref) -> None:
        """All three FC attempts fail -> BAD quality ReadResult, no exception."""
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        session._client.read_do_with_meta.side_effect = MmsDirectoryError("timeout", error_code=14)
        from protoskipper.core.driver import Quality

        result = session.read(do_ref)
        assert result.quality == Quality.BAD
        assert result.error is not None
        assert result.value is None
        assert session._client.read_do_with_meta.call_count == 3  # MX, ST, SP

    # -- DA-level reads with [FC] suffix -------------------------------------

    def test_da_explicit_fc_reads_single_attribute(self, session) -> None:
        """object_id with [MX] suffix -> read_object called once, quality=UNKNOWN."""
        from protoskipper_iec61850._mms_client import FC_MX, MmsDecodedValue

        from protoskipper.core.driver import ObjectRef, Quality

        ref = ObjectRef(
            device=session.device,
            object_id="LD0/MMXU1.A.phsA.cVal.mag.f[MX]",
            data_type="float32",
        )
        session._client.read_object.return_value = MmsDecodedValue(value=230.5)
        result = session.read(ref)
        assert result.quality == Quality.UNKNOWN
        assert result.value == 230.5
        session._client.read_object.assert_called_once_with("LD0/MMXU1.A.phsA.cVal.mag.f", FC_MX)
        session._client.read_do_with_meta.assert_not_called()

    def test_da_explicit_fc_error_returns_bad(self, session) -> None:
        """MmsDirectoryError on a DA-level read -> BAD quality, no exception."""
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        from protoskipper.core.driver import ObjectRef, Quality

        ref = ObjectRef(
            device=session.device,
            object_id="LD0/MMXU1.A.phsA.cVal.mag.f[ST]",
            data_type="float32",
        )
        session._client.read_object.side_effect = MmsDirectoryError("access denied", error_code=11)
        result = session.read(ref)
        assert result.quality == Quality.BAD
        assert result.error is not None


# ---------------------------------------------------------------------------
# P8.B.5 — prepare_write / commit_write
# ---------------------------------------------------------------------------


class TestPrepareWrite:
    """prepare_write encodes the ctlVal with no I/O."""

    @pytest.fixture
    def session(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = MagicMock(spec=SafetyContext)
        return Iec61850MmsSession(device=device, safety=safety, client=MagicMock())

    @pytest.fixture
    def ctrl_ref(self, session):  # type: ignore[no-untyped-def]
        from protoskipper.core.driver import ObjectRef

        return ObjectRef(device=session.device, object_id="LD0/XCBR1.Pos", data_type="do")

    def test_bool_value_returns_intent(self, session, ctrl_ref) -> None:
        from protoskipper.core.driver import WriteIntent

        intent = session.prepare_write(ctrl_ref, True)
        assert isinstance(intent, WriteIntent)
        assert intent.object_ref is ctrl_ref
        assert intent.requested_value is True
        assert isinstance(intent.encoded_bytes, bytes) and len(intent.encoded_bytes) > 0
        assert isinstance(intent.description, str)

    def test_int_value_returns_intent(self, session, ctrl_ref) -> None:
        intent = session.prepare_write(ctrl_ref, 1)
        assert intent.requested_value == 1

    def test_float_value_returns_intent(self, session, ctrl_ref) -> None:
        intent = session.prepare_write(ctrl_ref, 1.5)
        assert intent.requested_value == 1.5

    def test_unsupported_type_raises_encoding_error(self, session, ctrl_ref) -> None:
        from protoskipper.core.errors import EncodingError

        with pytest.raises(EncodingError):
            session.prepare_write(ctrl_ref, "open")


class TestCommitWrite:
    """commit_write drives the four control models via MmsClient.write_control."""

    @pytest.fixture
    def session(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = MagicMock(spec=SafetyContext)
        safety.require_write_authorization.return_value = True
        mock_client = MagicMock()
        return Iec61850MmsSession(device=device, safety=safety, client=mock_client)

    @pytest.fixture
    def intent(self, session):  # type: ignore[no-untyped-def]
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(device=session.device, object_id="LD0/XCBR1.Pos", data_type="do")
        return session.prepare_write(ref, True)

    def _ctrl_ok(self, model: int) -> object:
        from protoskipper_iec61850._mms_client import ControlResult

        return ControlResult(success=True, control_model=model)

    def _ctrl_fail(self, model: int, add_cause: int, add_cause_name: str) -> object:
        from protoskipper_iec61850._mms_client import ControlResult

        return ControlResult(
            success=False,
            control_model=model,
            add_cause=add_cause,
            add_cause_name=add_cause_name,
            error_str=f"failed: addCause={add_cause_name}",
        )

    def test_denied_by_safety_returns_failure_no_record(self, session, intent) -> None:
        session.safety.require_write_authorization.return_value = False
        result = session.commit_write(intent)
        assert result.success is False
        session.safety.record_write_outcome.assert_not_called()
        session._client.write_control.assert_not_called()

    def test_direct_normal_success(self, session, intent) -> None:
        from protoskipper_iec61850._mms_client import CONTROL_MODEL_DIRECT_NORMAL

        session._client.write_control.return_value = self._ctrl_ok(CONTROL_MODEL_DIRECT_NORMAL)
        result = session.commit_write(intent)
        assert result.success is True
        assert result.metadata["control_model"] == CONTROL_MODEL_DIRECT_NORMAL
        session.safety.record_write_outcome.assert_called_once()

    def test_direct_enhanced_success(self, session, intent) -> None:
        from protoskipper_iec61850._mms_client import CONTROL_MODEL_DIRECT_ENHANCED

        session._client.write_control.return_value = self._ctrl_ok(CONTROL_MODEL_DIRECT_ENHANCED)
        result = session.commit_write(intent)
        assert result.success is True
        assert result.metadata["control_model"] == CONTROL_MODEL_DIRECT_ENHANCED

    def test_sbo_normal_success(self, session, intent) -> None:
        from protoskipper_iec61850._mms_client import CONTROL_MODEL_SBO_NORMAL

        session._client.write_control.return_value = self._ctrl_ok(CONTROL_MODEL_SBO_NORMAL)
        result = session.commit_write(intent)
        assert result.success is True
        assert result.metadata["control_model"] == CONTROL_MODEL_SBO_NORMAL

    def test_sbo_enhanced_success(self, session, intent) -> None:
        from protoskipper_iec61850._mms_client import CONTROL_MODEL_SBO_ENHANCED

        session._client.write_control.return_value = self._ctrl_ok(CONTROL_MODEL_SBO_ENHANCED)
        result = session.commit_write(intent)
        assert result.success is True
        assert result.metadata["control_model"] == CONTROL_MODEL_SBO_ENHANCED

    def test_select_fail_returns_failure_with_add_cause(self, session, intent) -> None:
        from protoskipper_iec61850._mms_client import CONTROL_MODEL_SBO_NORMAL

        session._client.write_control.return_value = self._ctrl_fail(
            CONTROL_MODEL_SBO_NORMAL, 3, "SELECT_FAILED"
        )
        result = session.commit_write(intent)
        assert result.success is False
        assert result.metadata["add_cause"] == 3
        assert result.metadata["add_cause_name"] == "SELECT_FAILED"
        session.safety.record_write_outcome.assert_called_once()

    def test_operate_fail_with_add_cause_in_metadata(self, session, intent) -> None:
        from protoskipper_iec61850._mms_client import CONTROL_MODEL_DIRECT_NORMAL

        session._client.write_control.return_value = self._ctrl_fail(
            CONTROL_MODEL_DIRECT_NORMAL, 10, "BLOCKED_BY_MODE"
        )
        result = session.commit_write(intent)
        assert result.success is False
        assert result.error is not None
        assert result.metadata["add_cause_name"] == "BLOCKED_BY_MODE"
        session.safety.record_write_outcome.assert_called_once()

    def test_write_control_receives_do_ref_and_value(self, session, intent) -> None:
        from protoskipper_iec61850._mms_client import ControlResult

        session._client.write_control.return_value = ControlResult(success=True, control_model=1)
        session.commit_write(intent)
        session._client.write_control.assert_called_once_with("LD0/XCBR1.Pos", True)

    def test_no_client_after_authorization_returns_failure(self, session, intent) -> None:
        session._client = None
        result = session.commit_write(intent)
        assert result.success is False
        session.safety.record_write_outcome.assert_called_once()

    def test_record_write_outcome_called_on_failure(self, session, intent) -> None:
        from protoskipper_iec61850._mms_client import ControlResult

        session._client.write_control.return_value = ControlResult(
            success=False, error_str="Operate failed"
        )
        result = session.commit_write(intent)
        assert result.success is False
        session.safety.record_write_outcome.assert_called_once()


# ---------------------------------------------------------------------------
# P8.B.6 — Reporting (BRCB / URCB)
# ---------------------------------------------------------------------------


def _make_mock_client_with_internals():  # type: ignore[no-untyped-def]
    """Return a MmsClient with _lib and _con mocked for low-level tests."""
    from unittest.mock import MagicMock

    from protoskipper_iec61850._mms_client import MmsClient

    client = MmsClient("10.0.0.1", 102)
    client._lib = MagicMock()
    client._con = MagicMock()
    # Sensible IED_ERROR_OK default
    client._lib.IED_ERROR_OK = 0
    return client


class TestMmsClientGetRcbValues:
    """MmsClient.get_rcb_values() reads RCB attributes and returns RcbValues."""

    def _setup(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        client = _make_mock_client_with_internals()
        lib = client._lib
        mock_rcb = MagicMock()
        lib.IedConnection_getRCBValues.return_value = (mock_rcb, 0)  # (rcb, IED_ERROR_OK)
        lib.ClientReportControlBlock_getRptId.return_value = "myRptId"
        lib.ClientReportControlBlock_getDatSet.return_value = "LD0/LLN0$ds1"
        lib.ClientReportControlBlock_getConfRev.return_value = 1
        lib.ClientReportControlBlock_getOptFlds.return_value = 10
        lib.ClientReportControlBlock_getBufTm.return_value = 0
        lib.ClientReportControlBlock_getTrgOps.return_value = 6  # dchg | qchg
        lib.ClientReportControlBlock_getIntgPd.return_value = 0
        lib.ClientReportControlBlock_getRptEna.return_value = False
        lib.ClientReportControlBlock_getResv.return_value = False
        return client, lib, mock_rcb

    def test_returns_rcb_values_on_success(self) -> None:
        from protoskipper_iec61850._mms_client import RcbValues

        client, _lib, _rcb = self._setup()
        result = client.get_rcb_values("LD0/LLN0.BR.rcb01", is_buffered=True)
        assert isinstance(result, RcbValues)
        assert result.rcb_ref == "LD0/LLN0.BR.rcb01"
        assert result.is_buffered is True
        assert result.rpt_id == "myRptId"
        assert result.dat_set == "LD0/LLN0$ds1"
        assert result.trg_ops == 6

    def test_destroys_rcb_after_success(self) -> None:
        client, lib, mock_rcb = self._setup()
        client.get_rcb_values("LD0/LLN0.BR.rcb01", is_buffered=True)
        lib.ClientReportControlBlock_destroy.assert_called_once_with(mock_rcb)

    def test_raises_on_ied_error(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client = _make_mock_client_with_internals()
        client._lib.IedConnection_getRCBValues.return_value = (None, 1)  # error
        with pytest.raises(MmsDirectoryError):
            client.get_rcb_values("LD0/LLN0.BR.rcb01", is_buffered=True)

    def test_resv_false_for_brcb(self) -> None:
        client, lib, _rcb = self._setup()
        lib.ClientReportControlBlock_getResv.return_value = True  # would be True if asked
        result = client.get_rcb_values("LD0/LLN0.BR.rcb01", is_buffered=True)
        assert result.resv is False  # ignored for BRCB


class TestMmsClientEnableReport:
    """MmsClient.enable_report() installs handler and sets RptEna=True."""

    def _setup(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        client = _make_mock_client_with_internals()
        lib = client._lib
        mock_rcb = MagicMock()
        lib.IedConnection_getRCBValues.return_value = (mock_rcb, 0)
        lib.ClientReportControlBlock_getRptId.return_value = "rptId1"
        lib.ClientReportControlBlock_getDatSet.return_value = "LD0/ds1"
        lib.ClientReportControlBlock_getConfRev.return_value = 0
        lib.ClientReportControlBlock_getOptFlds.return_value = 0
        lib.ClientReportControlBlock_getBufTm.return_value = 0
        lib.ClientReportControlBlock_getTrgOps.return_value = 6
        lib.ClientReportControlBlock_getIntgPd.return_value = 0
        lib.ClientReportControlBlock_getRptEna.return_value = True
        lib.ClientReportControlBlock_getResv.return_value = False
        lib.IedConnection_setRCBValues.return_value = 0  # IED_ERROR_OK
        return client, lib, mock_rcb

    def test_installs_handler_when_callback_provided(self) -> None:
        client, lib, _ = self._setup()
        client.enable_report("LD0/LLN0.BR.rcb01", True, on_report=lambda r: None)
        lib.IedConnection_installReportHandler.assert_called_once()

    def test_no_handler_installed_without_callback(self) -> None:
        client, lib, _ = self._setup()
        client.enable_report("LD0/LLN0.BR.rcb01", True, on_report=None)
        lib.IedConnection_installReportHandler.assert_not_called()

    def test_sets_rpt_ena_true(self) -> None:
        client, lib, mock_rcb = self._setup()
        client.enable_report("LD0/LLN0.BR.rcb01", True, on_report=None)
        lib.ClientReportControlBlock_setRptEna.assert_called_once_with(mock_rcb, True)

    def test_handler_stored_to_prevent_gc(self) -> None:
        client, _lib, _ = self._setup()
        callback = lambda r: None  # noqa: E731
        client.enable_report("LD0/LLN0.BR.rcb01", True, on_report=callback)
        assert "LD0/LLN0.BR.rcb01" in client._report_handlers

    def test_raises_on_get_rcb_failure(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client = _make_mock_client_with_internals()
        client._lib.IedConnection_getRCBValues.return_value = (None, 14)  # timeout
        with pytest.raises(MmsDirectoryError):
            client.enable_report("LD0/LLN0.BR.rcb01", True, on_report=None)


class TestMmsClientDisableReport:
    """MmsClient.disable_report() sets RptEna=False and clears handler ref."""

    def _setup(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        client = _make_mock_client_with_internals()
        lib = client._lib
        mock_rcb = MagicMock()
        lib.IedConnection_getRCBValues.return_value = (mock_rcb, 0)
        lib.ClientReportControlBlock_getRptId.return_value = "rptId1"
        lib.IedConnection_setRCBValues.return_value = 0
        client._report_handlers["LD0/LLN0.BR.rcb01"] = lambda: None
        return client, lib, mock_rcb

    def test_sets_rpt_ena_false(self) -> None:
        client, lib, mock_rcb = self._setup()
        client.disable_report("LD0/LLN0.BR.rcb01", True)
        lib.ClientReportControlBlock_setRptEna.assert_called_once_with(mock_rcb, False)

    def test_clears_handler_ref(self) -> None:
        client, _lib, _ = self._setup()
        assert "LD0/LLN0.BR.rcb01" in client._report_handlers
        client.disable_report("LD0/LLN0.BR.rcb01", True)
        assert "LD0/LLN0.BR.rcb01" not in client._report_handlers

    def test_does_not_raise_on_get_rcb_failure(self) -> None:
        client = _make_mock_client_with_internals()
        client._lib.IedConnection_getRCBValues.return_value = (None, 1)
        client.disable_report("LD0/LLN0.BR.rcb01", True)  # must not raise


class TestSessionReporting:
    """Iec61850MmsSession.subscribe_report / unsubscribe_report delegate to MmsClient."""

    @pytest.fixture
    def session(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = MagicMock(spec=SafetyContext)
        mock_client = MagicMock()
        return Iec61850MmsSession(device=device, safety=safety, client=mock_client)

    def test_subscribe_delegates_to_client(self, session) -> None:
        from protoskipper_iec61850._mms_client import RcbValues

        session._client.enable_report.return_value = RcbValues(
            rcb_ref="LD0/LLN0.BR.rcb01", is_buffered=True
        )
        cb = lambda r: None  # noqa: E731
        result = session.subscribe_report("LD0/LLN0.BR.rcb01", True, cb)
        session._client.enable_report.assert_called_once_with(
            "LD0/LLN0.BR.rcb01",
            True,
            6,
            0,
            cb,  # default trg_ops = 2|4 = 6
        )
        assert isinstance(result, RcbValues)

    def test_unsubscribe_delegates_to_client(self, session) -> None:
        session.unsubscribe_report("LD0/LLN0.BR.rcb01", True)
        session._client.disable_report.assert_called_once_with("LD0/LLN0.BR.rcb01", True)

    def test_subscribe_no_client_raises(self) -> None:
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import MmsDirectoryError
        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        session = Iec61850MmsSession(device=device, safety=MagicMock(spec=SafetyContext))
        with pytest.raises(MmsDirectoryError):
            session.subscribe_report("LD0/LLN0.BR.rcb01", True, lambda r: None)

    def test_unsubscribe_no_client_is_noop(self) -> None:
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        session = Iec61850MmsSession(device=device, safety=MagicMock(spec=SafetyContext))
        session.unsubscribe_report("LD0/LLN0.BR.rcb01", True)  # must not raise


# ---------------------------------------------------------------------------
# P8.B.7 — Log buffer query
# ---------------------------------------------------------------------------


def _make_mock_journal_entry(lib, entry_id_bytes=b"\x01\x02", occ_ms=1_700_000_000_000):  # type: ignore[no-untyped-def]
    """Return a mock MmsJournalEntry (as LinkedList data) with known fields."""
    from unittest.mock import MagicMock

    entry = MagicMock()
    # occurrence time
    occ_val = MagicMock()
    lib.MmsJournalEntry_getOccurenceTime.return_value = occ_val
    lib.MmsValue_getBinaryTimeAsUtcMs.return_value = occ_ms
    # entry ID
    eid_val = MagicMock()
    eid_buf = bytearray(entry_id_bytes)
    lib.MmsJournalEntry_getEntryID.return_value = eid_val
    lib.MmsValue_getOctetStringBuffer.return_value = eid_buf
    lib.MmsValue_getOctetStringSize.return_value = len(entry_id_bytes)
    # no journal variables (empty list head → LinkedList_getNext returns None)
    jvars_ll = MagicMock()
    lib.MmsJournalEntry_getJournalVariables.return_value = jvars_ll
    lib.LinkedList_getNext.side_effect = _ll_single_then_none_factory(entry, jvars_ll)
    lib.LinkedList_getData.return_value = entry
    return entry


def _ll_single_then_none_factory(data_entry, jvars_ll):  # type: ignore[no-untyped-def]
    """Return a side_effect for LinkedList_getNext that yields one data node then None.

    Outer call sequence (iterating entries_ll):
      LinkedList_getNext(entries_ll) -> node1
      LinkedList_getNext(node1) -> None
    Inner call sequence (iterating jvars_ll):
      LinkedList_getNext(jvars_ll) -> None  (no variables)
    """
    from unittest.mock import MagicMock

    node = MagicMock()
    node._is_entry_node = True
    calls = {}

    def _side_effect(ll_or_node):  # type: ignore[no-untyped-def]
        key = id(ll_or_node)
        if id(ll_or_node) == id(jvars_ll):
            return None  # no journal variables
        count = calls.get(key, 0)
        calls[key] = count + 1
        if count == 0:
            return node  # first call: return the single entry node
        return None  # subsequent calls: end of list

    return _side_effect


class TestMmsClientQueryLogByTime:
    """MmsClient.query_log_by_time() wraps IedConnection_queryLogByTime."""

    def _make_client(self):  # type: ignore[no-untyped-def]
        client = _make_mock_client_with_internals()
        lib = client._lib
        # Build a minimal entries linked-list mock
        entries_ll = object()  # opaque sentinel
        lib.IedConnection_queryLogByTime.return_value = (entries_ll, 0, False)
        # LinkedList traversal: no entries (getNext returns None for entries_ll)
        lib.LinkedList_getNext.return_value = None
        return client, lib, entries_ll

    def test_returns_empty_list_when_no_entries(self) -> None:
        client, _lib, _ = self._make_client()
        entries, more = client.query_log_by_time("LD0/LLN0$GL", 0, 1_000_000)
        assert entries == []
        assert more is False

    def test_more_follows_true_propagated(self) -> None:
        client, lib, entries_ll = self._make_client()
        lib.IedConnection_queryLogByTime.return_value = (entries_ll, 0, True)
        _, more = client.query_log_by_time("LD0/LLN0$GL", 0, 1_000_000)
        assert more is True

    def test_raises_mms_directory_error_on_ied_error(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client, lib, entries_ll = self._make_client()
        lib.IedConnection_queryLogByTime.return_value = (entries_ll, 20, False)
        lib._ied_error_name = lambda e: str(e)
        with pytest.raises(MmsDirectoryError):
            client.query_log_by_time("LD0/LLN0$GL", 0, 1_000_000)

    def test_ll_destroy_called_even_on_success(self) -> None:
        client, lib, entries_ll = self._make_client()
        client.query_log_by_time("LD0/LLN0$GL", 0, 1_000_000)
        lib.LinkedList_destroyDeep.assert_called_once_with(entries_ll, lib.MmsJournalEntry_destroy)


class TestMmsClientQueryLogAfter:
    """MmsClient.query_log_after() wraps IedConnection_queryLogAfter."""

    def _make_client(self):  # type: ignore[no-untyped-def]
        client = _make_mock_client_with_internals()
        lib = client._lib
        entries_ll = object()
        lib.IedConnection_queryLogAfter.return_value = (entries_ll, 0, False)
        lib.LinkedList_getNext.return_value = None
        # Mock MmsValue creation/deletion for entry ID
        eid_mv = object()
        lib.MmsValue_newOctetString.return_value = eid_mv
        buf = bytearray(4)
        lib.MmsValue_getOctetStringBuffer.return_value = buf
        return client, lib, entries_ll, eid_mv

    def test_returns_empty_list_on_no_entries(self) -> None:
        client, _lib, _, _ = self._make_client()
        entries, more = client.query_log_after("LD0/LLN0$GL", b"\x01\x02", 0)
        assert entries == []
        assert more is False

    def test_entry_id_mms_value_created_and_deleted(self) -> None:
        client, lib, _, eid_mv = self._make_client()
        client.query_log_after("LD0/LLN0$GL", b"\xab\xcd", 12345)
        lib.MmsValue_newOctetString.assert_called_once_with(2, 2)
        lib.MmsValue_delete.assert_called_once_with(eid_mv)

    def test_raises_mms_directory_error_on_ied_error(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client, lib, entries_ll, _ = self._make_client()
        lib.IedConnection_queryLogAfter.return_value = (entries_ll, 5, False)
        with pytest.raises(MmsDirectoryError):
            client.query_log_after("LD0/LLN0$GL", b"\x00", 0)


class TestSessionLog:
    """Iec61850MmsSession.query_log_by_time / query_log_after delegate to MmsClient."""

    @pytest.fixture
    def session_with_client(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = MagicMock(spec=SafetyContext)
        mock_client = MagicMock()
        return Iec61850MmsSession(device=device, safety=safety, client=mock_client)

    def test_query_log_by_time_delegates(self, session_with_client) -> None:
        from protoskipper_iec61850._mms_client import LogEntry

        expected = ([LogEntry(log_ref="LD0/LLN0$GL", entry_id=b"", occurrence_time_ms=0)], False)
        session_with_client._client.query_log_by_time.return_value = expected
        result = session_with_client.query_log_by_time("LD0/LLN0$GL", 0, 1000)
        session_with_client._client.query_log_by_time.assert_called_once_with(
            "LD0/LLN0$GL", 0, 1000
        )
        assert result == expected

    def test_query_log_after_delegates(self, session_with_client) -> None:

        expected = ([], True)
        session_with_client._client.query_log_after.return_value = expected
        result = session_with_client.query_log_after("LD0/LLN0$GL", b"\x01", 9999)
        session_with_client._client.query_log_after.assert_called_once_with(
            "LD0/LLN0$GL", b"\x01", 9999
        )
        assert result == expected

    def test_no_client_raises_mms_directory_error(self) -> None:
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import MmsDirectoryError
        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        session = Iec61850MmsSession(device=device, safety=MagicMock(spec=SafetyContext))
        with pytest.raises(MmsDirectoryError):
            session.query_log_by_time("LD0/LLN0$GL", 0, 1000)
        with pytest.raises(MmsDirectoryError):
            session.query_log_after("LD0/LLN0$GL", b"\x00", 0)


# ---------------------------------------------------------------------------
# P8.B.8 — File services
# ---------------------------------------------------------------------------


class TestMmsClientListFiles:
    """MmsClient.list_files() wraps IedConnection_getFileDirectory."""

    def _make_client(self):  # type: ignore[no-untyped-def]
        client = _make_mock_client_with_internals()
        lib = client._lib
        dir_ll = object()  # opaque sentinel for the LinkedList
        lib.IedConnection_getFileDirectory.return_value = (dir_ll, 0)
        # No entries by default
        lib.LinkedList_getNext.return_value = None
        return client, lib, dir_ll

    def test_returns_empty_list_for_empty_directory(self) -> None:
        client, _lib, _ = self._make_client()
        result = client.list_files()
        assert result == []

    def test_returns_file_info_list_on_success(self) -> None:
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import FileInfo

        client, lib, _dir_ll = self._make_client()
        node1 = MagicMock()
        entry1 = MagicMock()
        lib.LinkedList_getNext.side_effect = [node1, None]
        lib.LinkedList_getData.return_value = entry1
        lib.FileDirectoryEntry_getFileName.return_value = "fault01.cfg"
        lib.FileDirectoryEntry_getFileSize.return_value = 1024
        lib.FileDirectoryEntry_getLastModified.return_value = 1_700_000_000_000

        result = client.list_files("COMTRADE")
        expected_info = FileInfo(name="fault01.cfg", size=1024, last_modified_ms=1_700_000_000_000)
        assert result == [expected_info]

    def test_ll_destroyed_on_success(self) -> None:
        client, lib, dir_ll = self._make_client()
        client.list_files()
        lib.LinkedList_destroyDeep.assert_called_once_with(dir_ll, lib.FileDirectoryEntry_destroy)

    def test_raises_mms_directory_error_on_ied_error(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client, lib, dir_ll = self._make_client()
        lib.IedConnection_getFileDirectory.return_value = (dir_ll, 22)  # OBJECT_DOES_NOT_EXIST
        with pytest.raises(MmsDirectoryError):
            client.list_files("MISSING")


class TestMmsClientGetFile:
    """MmsClient.get_file() wraps IedConnection_getFile."""

    def _make_client(self):  # type: ignore[no-untyped-def]
        client = _make_mock_client_with_internals()
        lib = client._lib
        lib.IedConnection_getFile.return_value = (0, 0)
        return client, lib

    def test_returns_empty_bytes_when_no_chunks(self) -> None:
        client, _lib = self._make_client()
        result = client.get_file("COMTRADE/fault01.cfg")
        assert result == b""

    def test_handler_accumulates_chunks_into_bytes(self) -> None:
        client, lib = self._make_client()

        captured_handler = None

        def _capture_getfile(con, filename, handler, param):  # type: ignore[no-untyped-def]
            nonlocal captured_handler
            captured_handler = handler
            handler(bytearray(b"Hello, "), 7)
            handler(bytearray(b"world!"), 6)
            return (13, 0)

        lib.IedConnection_getFile.side_effect = _capture_getfile
        result = client.get_file("COMTRADE/data.cfg")
        assert result == b"Hello, world!"

    def test_raises_mms_directory_error_on_ied_error(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client, lib = self._make_client()
        lib.IedConnection_getFile.return_value = (0, 20)  # IED_ERROR_TIMEOUT
        with pytest.raises(MmsDirectoryError):
            client.get_file("MISSING.cfg")


class TestMmsClientDeleteFile:
    """MmsClient.delete_file() wraps IedConnection_deleteFile."""

    def _make_client(self):  # type: ignore[no-untyped-def]
        client = _make_mock_client_with_internals()
        lib = client._lib
        lib.IedConnection_deleteFile.return_value = 0  # IED_ERROR_OK
        return client, lib

    def test_success_does_not_raise(self) -> None:
        client, _lib = self._make_client()
        client.delete_file("COMTRADE/fault01.cfg")  # no exception

    def test_raises_mms_directory_error_on_ied_error(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client, lib = self._make_client()
        lib.IedConnection_deleteFile.return_value = 21  # IED_ERROR_ACCESS_DENIED
        with pytest.raises(MmsDirectoryError):
            client.delete_file("PROTECTED/file.bin")


class TestSessionFileServices:
    """Iec61850MmsSession file service methods delegate to MmsClient."""

    @pytest.fixture
    def session_with_client(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = MagicMock(spec=SafetyContext)
        mock_client = MagicMock()
        return Iec61850MmsSession(device=device, safety=safety, client=mock_client)

    def test_list_files_delegates_to_client(self, session_with_client) -> None:
        from protoskipper_iec61850._mms_client import FileInfo

        expected = [FileInfo(name="fault01.cfg", size=512, last_modified_ms=0)]
        session_with_client._client.list_files.return_value = expected
        result = session_with_client.list_files("COMTRADE")
        session_with_client._client.list_files.assert_called_once_with("COMTRADE")
        assert result == expected

    def test_get_file_delegates_to_client(self, session_with_client) -> None:
        session_with_client._client.get_file.return_value = b"\xde\xad\xbe\xef"
        result = session_with_client.get_file("COMTRADE/fault01.cfg")
        session_with_client._client.get_file.assert_called_once_with("COMTRADE/fault01.cfg")
        assert result == b"\xde\xad\xbe\xef"

    def test_delete_file_delegates_to_client(self, session_with_client) -> None:
        session_with_client.delete_file("COMTRADE/old.cfg")
        session_with_client._client.delete_file.assert_called_once_with("COMTRADE/old.cfg")

    def test_no_client_raises_mms_directory_error(self) -> None:
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import MmsDirectoryError
        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        session = Iec61850MmsSession(device=device, safety=MagicMock(spec=SafetyContext))
        with pytest.raises(MmsDirectoryError):
            session.list_files()
        with pytest.raises(MmsDirectoryError):
            session.get_file("x.cfg")
        with pytest.raises(MmsDirectoryError):
            session.delete_file("x.cfg")


# ---------------------------------------------------------------------------
# P8.B.9 — Setting-group services
# ---------------------------------------------------------------------------


class TestMmsClientGetSgcbValues:
    """MmsClient.get_sgcb_values reads NumOfSGs and ActSG via FC_SG."""

    def _make_client(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import MmsClient

        lib = MagicMock()
        lib.IED_ERROR_OK = 0
        lib.IedConnection_readObject.side_effect = [
            (MagicMock(name="num_mv"), 0),  # NumOfSGs call → OK
            (MagicMock(name="act_mv"), 0),  # ActSG call → OK
        ]
        lib.MmsValue_toInt32.side_effect = [3, 2]  # NumOfSGs=3, ActSG=2
        client = MmsClient.__new__(MmsClient)
        client._lib = lib
        client._con = MagicMock()
        return client, lib

    def test_returns_sgcb_values(self) -> None:
        from protoskipper_iec61850._mms_client import FC_SG, SgcbValues

        client, lib = self._make_client()
        result = client.get_sgcb_values("simpleIO/LLN0.SGCB")
        assert isinstance(result, SgcbValues)
        assert result.num_of_sgs == 3
        assert result.act_sg == 2
        # Both reads used FC_SG
        calls = lib.IedConnection_readObject.call_args_list
        assert calls[0].args[2] == FC_SG
        assert calls[1].args[2] == FC_SG

    def test_num_of_sgs_read_ref(self) -> None:
        client, lib = self._make_client()
        client.get_sgcb_values("LD/LLN0.SGCB")
        first_ref = lib.IedConnection_readObject.call_args_list[0].args[1]
        assert first_ref == "LD/LLN0.SGCB.NumOfSGs"

    def test_act_sg_read_ref(self) -> None:
        client, lib = self._make_client()
        client.get_sgcb_values("LD/LLN0.SGCB")
        second_ref = lib.IedConnection_readObject.call_args_list[1].args[1]
        assert second_ref == "LD/LLN0.SGCB.ActSG"

    def test_raises_on_num_of_sgs_error(self) -> None:
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import MmsClient, MmsDirectoryError

        lib = MagicMock()
        lib.IED_ERROR_OK = 0
        lib.IedConnection_readObject.return_value = (None, 3)  # error
        client = MmsClient.__new__(MmsClient)
        client._lib = lib
        client._con = MagicMock()
        with pytest.raises(MmsDirectoryError):
            client.get_sgcb_values("LD/LLN0.SGCB")

    def test_raises_on_act_sg_error(self) -> None:
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import MmsClient, MmsDirectoryError

        lib = MagicMock()
        lib.IED_ERROR_OK = 0
        lib.IedConnection_readObject.side_effect = [
            (MagicMock(), 0),  # NumOfSGs OK
            (None, 5),  # ActSG error
        ]
        lib.MmsValue_toInt32.return_value = 2
        client = MmsClient.__new__(MmsClient)
        client._lib = lib
        client._con = MagicMock()
        with pytest.raises(MmsDirectoryError):
            client.get_sgcb_values("LD/LLN0.SGCB")


class TestMmsClientSelectActiveSg:
    """MmsClient.select_active_sg writes ActSG with FC_SG."""

    def _make_client(self, error_code: int = 0):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import MmsClient

        lib = MagicMock()
        lib.IED_ERROR_OK = 0
        lib.IedConnection_writeObject.return_value = error_code
        client = MmsClient.__new__(MmsClient)
        client._lib = lib
        client._con = MagicMock()
        return client, lib

    def test_success_does_not_raise(self) -> None:
        client, _ = self._make_client(0)
        client.select_active_sg("simpleIO/LLN0.SGCB", 2)

    def test_writes_correct_ref_and_fc(self) -> None:
        from protoskipper_iec61850._mms_client import FC_SG

        client, lib = self._make_client(0)
        client.select_active_sg("LD/LLN0.SGCB", 1)
        args = lib.IedConnection_writeObject.call_args.args
        assert args[1] == "LD/LLN0.SGCB.ActSG"
        assert args[2] == FC_SG

    def test_creates_integer_mms_value(self) -> None:
        client, lib = self._make_client(0)
        client.select_active_sg("LD/LLN0.SGCB", 3)
        lib.MmsValue_newIntegerFromInt32.assert_called_once_with(3)

    def test_raises_on_error(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client, _ = self._make_client(21)  # IED_ERROR_ACCESS_DENIED
        with pytest.raises(MmsDirectoryError):
            client.select_active_sg("LD/LLN0.SGCB", 2)

    def test_mms_value_deleted_on_success(self) -> None:
        client, lib = self._make_client(0)
        mv = lib.MmsValue_newIntegerFromInt32.return_value
        client.select_active_sg("LD/LLN0.SGCB", 1)
        lib.MmsValue_delete.assert_called_once_with(mv)

    def test_mms_value_deleted_on_error(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client, lib = self._make_client(5)
        mv = lib.MmsValue_newIntegerFromInt32.return_value
        with pytest.raises(MmsDirectoryError):
            client.select_active_sg("LD/LLN0.SGCB", 1)
        lib.MmsValue_delete.assert_called_once_with(mv)


class TestMmsClientSelectEditSg:
    """MmsClient.select_edit_sg writes EditSG with FC_SE."""

    def _make_client(self, error_code: int = 0):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import MmsClient

        lib = MagicMock()
        lib.IED_ERROR_OK = 0
        lib.IedConnection_writeObject.return_value = error_code
        client = MmsClient.__new__(MmsClient)
        client._lib = lib
        client._con = MagicMock()
        return client, lib

    def test_success_does_not_raise(self) -> None:
        client, _ = self._make_client(0)
        client.select_edit_sg("simpleIO/LLN0.SGCB", 2)

    def test_writes_correct_ref_and_fc(self) -> None:
        from protoskipper_iec61850._mms_client import FC_SE

        client, lib = self._make_client(0)
        client.select_edit_sg("LD/LLN0.SGCB", 1)
        args = lib.IedConnection_writeObject.call_args.args
        assert args[1] == "LD/LLN0.SGCB.EditSG"
        assert args[2] == FC_SE

    def test_raises_on_error(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client, _ = self._make_client(21)
        with pytest.raises(MmsDirectoryError):
            client.select_edit_sg("LD/LLN0.SGCB", 1)


class TestMmsClientConfirmEditSg:
    """MmsClient.confirm_edit_sg writes CnfEdit=True with FC_SE."""

    def _make_client(self, error_code: int = 0):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import MmsClient

        lib = MagicMock()
        lib.IED_ERROR_OK = 0
        lib.IedConnection_writeObject.return_value = error_code
        client = MmsClient.__new__(MmsClient)
        client._lib = lib
        client._con = MagicMock()
        return client, lib

    def test_success_does_not_raise(self) -> None:
        client, _ = self._make_client(0)
        client.confirm_edit_sg("simpleIO/LLN0.SGCB")

    def test_writes_correct_ref_and_fc(self) -> None:
        from protoskipper_iec61850._mms_client import FC_SE

        client, lib = self._make_client(0)
        client.confirm_edit_sg("LD/LLN0.SGCB")
        args = lib.IedConnection_writeObject.call_args.args
        assert args[1] == "LD/LLN0.SGCB.CnfEdit"
        assert args[2] == FC_SE

    def test_creates_boolean_true_mms_value(self) -> None:
        client, lib = self._make_client(0)
        client.confirm_edit_sg("LD/LLN0.SGCB")
        lib.MmsValue_newBoolean.assert_called_once_with(True)

    def test_raises_on_error(self) -> None:
        from protoskipper_iec61850._mms_client import MmsDirectoryError

        client, _ = self._make_client(21)
        with pytest.raises(MmsDirectoryError):
            client.confirm_edit_sg("LD/LLN0.SGCB")


class TestSessionSgcbServices:
    """Iec61850MmsSession SGCB methods delegate to MmsClient."""

    @pytest.fixture
    def session_with_client(self):  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        safety = MagicMock(spec=SafetyContext)
        mock_client = MagicMock()
        return Iec61850MmsSession(device=device, safety=safety, client=mock_client)

    def test_get_sgcb_values_delegates(self, session_with_client) -> None:
        from protoskipper_iec61850._mms_client import SgcbValues

        expected = SgcbValues(num_of_sgs=3, act_sg=1)
        session_with_client._client.get_sgcb_values.return_value = expected
        result = session_with_client.get_sgcb_values("simpleIO/LLN0.SGCB")
        session_with_client._client.get_sgcb_values.assert_called_once_with("simpleIO/LLN0.SGCB")
        assert result == expected

    def test_select_active_sg_delegates(self, session_with_client) -> None:
        session_with_client.select_active_sg("simpleIO/LLN0.SGCB", 2)
        session_with_client._client.select_active_sg.assert_called_once_with(
            "simpleIO/LLN0.SGCB", 2
        )

    def test_select_edit_sg_delegates(self, session_with_client) -> None:
        session_with_client.select_edit_sg("simpleIO/LLN0.SGCB", 1)
        session_with_client._client.select_edit_sg.assert_called_once_with("simpleIO/LLN0.SGCB", 1)

    def test_confirm_edit_sg_delegates(self, session_with_client) -> None:
        session_with_client.confirm_edit_sg("simpleIO/LLN0.SGCB")
        session_with_client._client.confirm_edit_sg.assert_called_once_with("simpleIO/LLN0.SGCB")

    def test_no_client_raises_for_all_sgcb_methods(self) -> None:
        from unittest.mock import MagicMock

        from protoskipper_iec61850._mms_client import MmsDirectoryError
        from protoskipper_iec61850.driver import Iec61850MmsSession

        from protoskipper.core.driver import DeviceRef, SafetyContext

        device = DeviceRef(protocol="iec61850.mms", address="10.0.0.1:102")
        session = Iec61850MmsSession(device=device, safety=MagicMock(spec=SafetyContext))
        with pytest.raises(MmsDirectoryError):
            session.get_sgcb_values("LD/LLN0.SGCB")
        with pytest.raises(MmsDirectoryError):
            session.select_active_sg("LD/LLN0.SGCB", 1)
        with pytest.raises(MmsDirectoryError):
            session.select_edit_sg("LD/LLN0.SGCB", 1)
        with pytest.raises(MmsDirectoryError):
            session.confirm_edit_sg("LD/LLN0.SGCB")
