# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for the IEC 61850 plugin — P8.A.1 contract verification.

Tests verify that:
* The plugin package imports cleanly.
* ``Iec61850MmsDriver`` satisfies the ``ProtocolDriver`` contract.
* ``protoskipper.core.plugin_loader`` discovers ``iec61850.mms`` when the
  plugin is installed.
* ``parse_address`` accepts valid addresses and rejects invalid ones.
* ``discover`` is a generator that yields nothing (scaffold stage).
* ``connect`` raises ``NotImplementedError`` with a helpful message.
* ``Iec61850MmsSession`` stubs raise ``NotImplementedError`` with a note
  pointing to the relevant plan task.
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
# P8.A.1 — connect raises NotImplementedError (scaffold)
# ---------------------------------------------------------------------------


class TestConnect:
    def test_connect_raises_not_implemented(self) -> None:
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        from protoskipper.core.driver import SafetyContext, SessionProfile

        driver = Iec61850MmsDriver()
        ref = driver.parse_address("10.0.0.1")
        safety = SafetyContext(
            profile=SessionProfile.LAB,
            confirm_callback=lambda i, p: True,
            audit_callback=lambda **_kw: None,
        )
        with pytest.raises(NotImplementedError, match=r"P8\.B\.2"):
            driver.connect(ref, safety)


# ---------------------------------------------------------------------------
# P8.A.1 — session stubs raise NotImplementedError
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
        return Iec61850MmsSession(device=device, safety=safety)

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

    def test_close_is_noop(self, session) -> None:  # type: ignore[no-untyped-def]
        session.close()  # must not raise
