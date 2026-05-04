# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for BACnet/IP address parsing and plugin discovery."""

from __future__ import annotations

import pytest

from protoskipper.builtin_drivers.bacnet.driver import (
    DEFAULT_PORT,
    BacnetIpDriver,
    parse_address,
)
from protoskipper.core.errors import EncodingError


class TestParseAddress:
    def test_bare_host(self) -> None:
        host, port, dev_id = parse_address("192.168.1.100")
        assert host == "192.168.1.100"
        assert port == DEFAULT_PORT
        assert dev_id is None

    def test_host_and_port(self) -> None:
        host, port, dev_id = parse_address("192.168.1.100:47808")
        assert host == "192.168.1.100"
        assert port == 47808
        assert dev_id is None

    def test_host_port_dev_id(self) -> None:
        host, port, dev_id = parse_address("10.0.0.5:47808/dev=1234")
        assert host == "10.0.0.5"
        assert port == 47808
        assert dev_id == 1234

    def test_bacnet_scheme_stripped(self) -> None:
        host, _port, dev_id = parse_address("bacnet://10.0.0.5/dev=4194302")
        assert host == "10.0.0.5"
        assert dev_id == 4194302

    def test_non_standard_port(self) -> None:
        _host, port, _dev_id = parse_address("10.0.0.5:8090/dev=42")
        assert port == 8090

    def test_dev_id_only_no_port(self) -> None:
        host, port, dev_id = parse_address("10.0.0.5/dev=99")
        assert host == "10.0.0.5"
        assert port == DEFAULT_PORT
        assert dev_id == 99

    def test_bad_address_raises_encoding_error(self) -> None:
        with pytest.raises(EncodingError):
            parse_address("not a valid address!!!")

    def test_empty_raises_encoding_error(self) -> None:
        with pytest.raises(EncodingError):
            parse_address("")


class TestDriverBasics:
    def test_protocol_id(self) -> None:
        d = BacnetIpDriver()
        assert d.PROTOCOL_ID == "bacnet.ip"

    def test_display_name(self) -> None:
        d = BacnetIpDriver()
        assert "BACnet" in d.DISPLAY_NAME

    def test_parse_address_returns_device_ref(self) -> None:
        d = BacnetIpDriver()
        ref = d.parse_address("192.168.1.100/dev=1234")
        assert ref.protocol == "bacnet.ip"
        assert "1234" in ref.address
        assert ref.metadata["device_id"] == 1234
        assert ref.metadata["host"] == "192.168.1.100"

    def test_parse_address_metadata_includes_port(self) -> None:
        d = BacnetIpDriver()
        ref = d.parse_address("10.0.0.5:47808/dev=5")
        assert ref.metadata["port"] == 47808

    def test_driver_is_stateless(self) -> None:
        """Two driver instances should behave independently."""
        d1 = BacnetIpDriver()
        d2 = BacnetIpDriver()
        d1.register_point_list("10.0.0.1", [])
        assert "10.0.0.1" not in d2._point_lists


class TestPluginDiscovery:
    def test_bacnet_ip_entry_point_registered(self) -> None:
        from importlib.metadata import entry_points

        eps = {ep.name: ep for ep in entry_points(group="protoskipper.protocols")}
        assert "bacnet.ip" in eps

    def test_bacnet_ip_entry_point_loads_correct_class(self) -> None:
        from importlib.metadata import entry_points

        eps = {ep.name: ep for ep in entry_points(group="protoskipper.protocols")}
        cls = eps["bacnet.ip"].load()
        assert cls is BacnetIpDriver

    def test_plugin_loader_discovers_bacnet(self) -> None:
        from protoskipper.core.plugin_loader import load_protocol_drivers

        # Clear lru_cache so we see the freshly registered entry point.
        load_protocol_drivers.cache_clear()
        drivers = load_protocol_drivers()
        assert "bacnet.ip" in drivers
        # plugin_loader returns classes (or instances depending on impl)
        entry = drivers["bacnet.ip"]
        if isinstance(entry, type):
            assert issubclass(entry, BacnetIpDriver)
        else:
            assert isinstance(entry, BacnetIpDriver)


class TestObjectIdFormat:
    def test_object_id_str(self) -> None:
        from protoskipper.builtin_drivers.bacnet.objects import object_id_str

        assert object_id_str("analog-value", 1) == "analog-value:1"
        assert object_id_str("device", 4194302) == "device:4194302"

    def test_parse_object_id(self) -> None:
        from protoskipper.builtin_drivers.bacnet.objects import parse_object_id

        obj_type, instance = parse_object_id("analog-value:42")
        assert obj_type == "analog-value"
        assert instance == 42

    def test_parse_object_id_bad_format(self) -> None:
        from protoskipper.builtin_drivers.bacnet.objects import parse_object_id

        with pytest.raises(ValueError):
            parse_object_id("nocolon")

    def test_bacnet_value_to_python_none_for_null(self) -> None:
        from protoskipper.builtin_drivers.bacnet.objects import bacnet_value_to_python

        assert bacnet_value_to_python(None) is None
