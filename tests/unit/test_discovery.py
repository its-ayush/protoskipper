# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""Unit tests for network-discovery helpers across all protocol drivers.

These tests run without a live network by using mock sockets and patching
the BACnet asyncio loop.  They verify:

* CIDR expansion works correctly for IEC 104 and IEC 61850 discover().
* BACnet discover() converts CIDR notation to the subnet directed broadcast.
* IEC 61850 discover() is no longer a stub — it probes TCP/102.
* All drivers correctly skip unreachable hosts and yield DeviceRef objects
  with well-formed addresses.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# IEC 104 expansion helpers
# ---------------------------------------------------------------------------


class TestIec104Discover:
    """Tests for _expand_iec104_target() and Iec104TcpDriver.discover()."""

    def test_expand_single_host(self) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _expand_iec104_target

        result = _expand_iec104_target("10.0.0.5")
        assert result == [("10.0.0.5", 2404)]

    def test_expand_host_with_port(self) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _expand_iec104_target

        result = _expand_iec104_target("10.0.0.5:2406")
        assert result == [("10.0.0.5", 2406)]

    def test_expand_cidr_slash_24(self) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _expand_iec104_target

        result = _expand_iec104_target("10.0.0.0/30")
        # /30 has 2 usable hosts: .1 and .2
        assert result == [("10.0.0.1", 2404), ("10.0.0.2", 2404)]

    def test_expand_cidr_with_port(self) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _expand_iec104_target

        result = _expand_iec104_target("10.0.0.0/30:2406")
        assert all(port == 2406 for _, port in result)
        assert len(result) == 2

    def test_expand_comma_list(self) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _expand_iec104_target

        result = _expand_iec104_target("10.0.0.1,10.0.0.2:2405")
        assert ("10.0.0.1", 2404) in result
        assert ("10.0.0.2", 2405) in result

    def test_expand_mixed_cidr_and_host(self) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _expand_iec104_target

        result = _expand_iec104_target("10.0.0.0/30,192.168.1.1")
        hosts = [h for h, _ in result]
        assert "10.0.0.1" in hosts
        assert "10.0.0.2" in hosts
        assert "192.168.1.1" in hosts

    def test_expand_bad_cidr_skipped(self) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _expand_iec104_target

        result = _expand_iec104_target("notacidr/xyz,10.0.0.1")
        # bad CIDR skipped; single host still parsed
        assert result == [("10.0.0.1", 2404)]

    def test_discover_yields_reachable_host(self) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _probe_104_port

        fake_sock = MagicMock()
        fake_sock.__enter__ = lambda s: s
        fake_sock.__exit__ = MagicMock(return_value=False)
        with patch("socket.create_connection", return_value=fake_sock):
            ref = _probe_104_port("10.0.0.1", 2404)
        assert ref is not None
        assert ref.protocol == "iec104.tcp"
        assert "10.0.0.1" in ref.address

    def test_probe_returns_none_on_connection_refused(self) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _probe_104_port

        with patch("socket.create_connection", side_effect=OSError("refused")):
            ref = _probe_104_port("10.0.0.1", 2404)
        assert ref is None

    def test_discover_with_cidr_yields_multiple(self) -> None:
        """discover() with /30 CIDR yields DeviceRefs for reachable hosts."""
        from protoskipper.builtin_drivers.iec104.driver import Iec104TcpDriver

        driver = Iec104TcpDriver()
        fake_sock = MagicMock()
        fake_sock.__enter__ = lambda s: s
        fake_sock.__exit__ = MagicMock(return_value=False)
        with patch("socket.create_connection", return_value=fake_sock):
            results = list(driver.discover("10.0.0.0/30"))
        assert len(results) == 2
        addrs = {r.address for r in results}
        assert "10.0.0.1:2404/ca=1" in addrs
        assert "10.0.0.2:2404/ca=1" in addrs

    def test_discover_empty_target(self) -> None:
        from protoskipper.builtin_drivers.iec104.driver import Iec104TcpDriver

        driver = Iec104TcpDriver()
        results = list(driver.discover(""))
        assert results == []


# ---------------------------------------------------------------------------
# IEC 61850 expansion helpers
# ---------------------------------------------------------------------------


class TestIec61850Discover:
    """Tests for _expand_mms_target() and Iec61850MmsDriver.discover()."""

    def test_expand_single_host(self) -> None:
        from protoskipper_iec61850.driver import _expand_mms_target

        result = _expand_mms_target("10.0.0.5")
        assert result == [("10.0.0.5", 102)]

    def test_expand_host_with_port(self) -> None:
        from protoskipper_iec61850.driver import _expand_mms_target

        result = _expand_mms_target("10.0.0.5:4096")
        assert result == [("10.0.0.5", 4096)]

    def test_expand_cidr(self) -> None:
        from protoskipper_iec61850.driver import _expand_mms_target

        result = _expand_mms_target("10.0.0.0/30")
        assert len(result) == 2
        assert ("10.0.0.1", 102) in result
        assert ("10.0.0.2", 102) in result

    def test_expand_cidr_with_port(self) -> None:
        from protoskipper_iec61850.driver import _expand_mms_target

        result = _expand_mms_target("10.0.0.0/30:4096")
        assert all(port == 4096 for _, port in result)
        assert len(result) == 2

    def test_expand_comma_list(self) -> None:
        from protoskipper_iec61850.driver import _expand_mms_target

        result = _expand_mms_target("10.0.0.1,10.0.0.2:4096,10.0.0.0/30")
        hosts = [h for h, _ in result]
        assert "10.0.0.1" in hosts
        assert "10.0.0.2" in hosts
        assert "10.0.0.1" in hosts  # from CIDR
        assert len(result) >= 3

    def test_expand_bad_cidr_skipped(self) -> None:
        from protoskipper_iec61850.driver import _expand_mms_target

        result = _expand_mms_target("garbage/xyz,10.0.0.5")
        assert result == [("10.0.0.5", 102)]

    def test_probe_returns_device_ref_on_success(self) -> None:
        from protoskipper_iec61850.driver import _probe_mms_port

        fake_sock = MagicMock()
        fake_sock.__enter__ = lambda s: s
        fake_sock.__exit__ = MagicMock(return_value=False)
        with patch("socket.create_connection", return_value=fake_sock):
            ref = _probe_mms_port("10.0.0.5", 102)
        assert ref is not None
        assert ref.protocol == "iec61850.mms"
        assert ref.address == "10.0.0.5:102"

    def test_probe_returns_none_on_refused(self) -> None:
        from protoskipper_iec61850.driver import _probe_mms_port

        with patch("socket.create_connection", side_effect=OSError("refused")):
            ref = _probe_mms_port("10.0.0.5", 102)
        assert ref is None

    def test_discover_no_longer_stub(self) -> None:
        """discover() must yield at least one DeviceRef when sockets succeed."""
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        driver = Iec61850MmsDriver()
        fake_sock = MagicMock()
        fake_sock.__enter__ = lambda s: s
        fake_sock.__exit__ = MagicMock(return_value=False)
        with patch("socket.create_connection", return_value=fake_sock):
            results = list(driver.discover("10.0.0.5"))
        assert len(results) == 1
        assert results[0].protocol == "iec61850.mms"

    def test_discover_cidr_finds_all_usable_hosts(self) -> None:
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        driver = Iec61850MmsDriver()
        fake_sock = MagicMock()
        fake_sock.__enter__ = lambda s: s
        fake_sock.__exit__ = MagicMock(return_value=False)
        with patch("socket.create_connection", return_value=fake_sock):
            results = list(driver.discover("10.0.0.0/30"))
        assert len(results) == 2

    def test_discover_empty_target(self) -> None:
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        driver = Iec61850MmsDriver()
        results = list(driver.discover(""))
        assert results == []

    def test_discover_all_refused_yields_nothing(self) -> None:
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        driver = Iec61850MmsDriver()
        with patch("socket.create_connection", side_effect=OSError("refused")):
            results = list(driver.discover("10.0.0.0/30"))
        assert results == []


# ---------------------------------------------------------------------------
# BACnet CIDR → directed broadcast conversion
# ---------------------------------------------------------------------------


class TestBacnetCidrDiscovery:
    """Tests for CIDR→directed-broadcast conversion in BacnetIpDriver.discover()."""

    def _mock_async_result(self, async_result: list[dict]) -> Any:
        """Build a coroutine that returns async_result when awaited."""

        async def _coro(*args: Any, **kwargs: Any) -> list[dict]:
            return async_result

        return _coro

    def test_cidr_converts_to_directed_broadcast(self) -> None:
        """discover() must send Who-Is to the subnet broadcast, not the network addr."""
        from protoskipper.builtin_drivers.bacnet.driver import BacnetIpDriver

        driver = BacnetIpDriver()
        captured_addresses: list[str] = []

        async def fake_run(
            loop: Any,
            *,
            low_limit: Any,
            high_limit: Any,
            target_address: str | None,
            timeout: float,
        ) -> list:
            captured_addresses.append(str(target_address))
            return []

        with (
            patch.object(driver, "_async_discover_run", fake_run),
            patch("protoskipper.builtin_drivers.bacnet.driver.BacnetIpDriver.discover") as _mock,
        ):
            # Call the actual discover(), patching only _async_discover_run
            _mock.side_effect = None

        # Re-do without the second patch to actually call discover()
        captured_addresses.clear()
        with patch.object(driver, "_async_discover_run", fake_run):
            try:
                from bacpypes3.ipv4.app import NormalApplication  # noqa: F401
            except ImportError:
                pytest.skip("bacpypes3 not installed")
            list(driver.discover("10.10.14.0/23"))

        # The directed broadcast for 10.10.14.0/23 is 10.10.15.255
        assert len(captured_addresses) == 1
        assert captured_addresses[0] == "10.10.15.255"

    def test_cidr_slash_24(self) -> None:
        """10.0.0.0/24 → broadcast 10.0.0.255."""
        net = ipaddress.ip_network("10.0.0.0/24", strict=False)
        assert str(net.broadcast_address) == "10.0.0.255"

    def test_cidr_slash_23(self) -> None:
        """10.10.14.0/23 → broadcast 10.10.15.255."""
        net = ipaddress.ip_network("10.10.14.0/23", strict=False)
        assert str(net.broadcast_address) == "10.10.15.255"

    def test_single_host_not_modified(self) -> None:
        """A plain IP address must be passed through as-is (no CIDR conversion)."""
        from protoskipper.builtin_drivers.bacnet.driver import BacnetIpDriver

        driver = BacnetIpDriver()
        captured_addresses: list[str] = []

        async def fake_run(
            loop: Any,
            *,
            low_limit: Any,
            high_limit: Any,
            target_address: str | None,
            timeout: float,
        ) -> list:
            captured_addresses.append(str(target_address))
            return []

        with patch.object(driver, "_async_discover_run", fake_run):
            try:
                from bacpypes3.ipv4.app import NormalApplication  # noqa: F401
            except ImportError:
                pytest.skip("bacpypes3 not installed")
            list(driver.discover("10.0.0.5"))

        assert captured_addresses == ["10.0.0.5"]
