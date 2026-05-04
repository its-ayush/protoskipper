# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for the IEC 61850 plugin - P8.A.1 / P8.B.2 / P8.B.3 / P8.B.4 / P8.B.5 contract.

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
