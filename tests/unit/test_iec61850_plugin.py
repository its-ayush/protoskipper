# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for the IEC 61850 plugin — P8.A.1 / P8.B.2 contract verification.

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
* Session stubs for P8.B.3-P8.B.5 raise ``NotImplementedError``.
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

    def test_enumerate_objects_raises(self, session) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(NotImplementedError, match=r"P8\.B\.3"):
            next(iter(session.enumerate_objects()))

    def test_read_raises(self, session) -> None:  # type: ignore[no-untyped-def]
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(
            device=session.device,
            object_id="LD0/MMXU1.MX.A.phsA.cVal.mag.f",
            data_type="float32",
        )
        with pytest.raises(NotImplementedError, match=r"P8\.B\.4"):
            session.read(ref)

    def test_prepare_write_raises(self, session) -> None:  # type: ignore[no-untyped-def]
        from protoskipper.core.driver import ObjectRef

        ref = ObjectRef(
            device=session.device,
            object_id="LD0/XCBR1.CO.Pos.Oper.ctlVal",
            data_type="boolean",
        )
        with pytest.raises(NotImplementedError, match=r"P8\.B\.5"):
            session.prepare_write(ref, True)

    def test_commit_write_raises(self, session) -> None:  # type: ignore[no-untyped-def]
        from protoskipper.core.driver import ObjectRef, WriteIntent

        ref = ObjectRef(
            device=session.device,
            object_id="LD0/XCBR1.CO.Pos.Oper.ctlVal",
            data_type="boolean",
        )
        intent = WriteIntent(
            object_ref=ref,
            requested_value=True,
            encoded_bytes=b"\x01",
            description="test",
        )
        with pytest.raises(NotImplementedError, match=r"P8\.B\.5"):
            session.commit_write(intent)

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
