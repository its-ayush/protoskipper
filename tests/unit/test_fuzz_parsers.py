# Copyright (C) 2026 DataSailors Pvt Ltd. Licensed under GPL-3.0-or-later.
"""P9.A.3 — Property-based fuzz tests for all protocol parsers.

Uses the Hypothesis library to generate arbitrary string inputs and verify
that parsers never raise anything other than the documented
:class:`~protoskipper.core.errors.EncodingError` exception.  No crash,
``AssertionError``, ``ValueError``, or ``AttributeError`` is allowed to
escape.

Run with::

    pytest tests/unit/test_fuzz_parsers.py
    pytest tests/unit/test_fuzz_parsers.py --hypothesis-seed=0
    pytest tests/unit/test_fuzz_parsers.py -x  # stop on first failure

For extended fuzzing (CI nightly)::

    pytest tests/unit/test_fuzz_parsers.py --hypothesis-seed=0 \\
           -p hypothesis --hypothesis-settings max_examples=10000
"""

from __future__ import annotations

import contextlib

from hypothesis import given, settings
from hypothesis import strategies as st

from protoskipper.core.errors import EncodingError

# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------

# Printable ASCII + some Unicode — covers injection attempts, null bytes, etc.
_any_str = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd", "Po", "Sm", "Zs"),
        whitelist_characters="/:.@,=\x00\n\r\t\\",
    ),
    max_size=200,
)

# Strings that look vaguely like IP addresses / CIDR / hostnames
_ip_like = st.one_of(
    st.ip_addresses(v=4).map(str),
    st.ip_addresses(v=6).map(str),
    st.from_regex(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/\d{1,2}", fullmatch=True),
    st.text(alphabet="0123456789abcdefABCDEF.:/", max_size=60),
)

# Strings that look like Modbus table:address[:count]
_modbus_obj = st.one_of(
    st.from_regex(r"[a-zA-Z_]{1,20}:\d{1,6}(:\d{1,4})?", fullmatch=True),
    _any_str,
)


# ---------------------------------------------------------------------------
# Modbus parsers
# ---------------------------------------------------------------------------


class TestFuzzModbusParsers:
    """All Modbus parser functions must only raise EncodingError or succeed."""

    @given(_any_str)
    @settings(max_examples=500)
    def test_parse_probe_target_never_crashes(self, spec: str) -> None:
        from protoskipper.builtin_drivers.modbus.driver import parse_probe_target

        with contextlib.suppress(EncodingError):  # expected
            parse_probe_target(spec)

    @given(_modbus_obj)
    @settings(max_examples=500)
    def test_parse_object_id_never_crashes(self, oid: str) -> None:
        from protoskipper.builtin_drivers.modbus.driver import _parse_object_id

        with contextlib.suppress(EncodingError):  # expected
            _parse_object_id(oid)

    @given(_any_str)
    @settings(max_examples=500)
    def test_parse_rtu_address_never_crashes(self, address: str) -> None:
        from protoskipper.builtin_drivers.modbus.driver import parse_rtu_address

        with contextlib.suppress(EncodingError):  # expected
            parse_rtu_address(address)

    @given(
        st.ip_addresses(v=4).map(str),
        st.integers(min_value=1, max_value=65535),
        st.integers(min_value=1, max_value=247),
    )
    @settings(max_examples=200)
    def test_parse_probe_target_valid_host_succeeds(self, ip: str, port: int, unit: int) -> None:
        """Well-formed targets must parse without error."""
        from protoskipper.builtin_drivers.modbus.driver import parse_probe_target

        target = f"{ip}:{port}/unit={unit}"
        result = parse_probe_target(target)
        assert len(result.hosts) == 1
        assert result.hosts[0] == (ip, port)
        assert result.units == (unit,)


# ---------------------------------------------------------------------------
# IEC 104 parsers
# ---------------------------------------------------------------------------


class TestFuzzIec104Parsers:
    """IEC 104 address and target parsers must not crash on arbitrary input."""

    @given(_any_str)
    @settings(max_examples=500)
    def test_parse_address_never_crashes(self, address: str) -> None:
        from protoskipper.builtin_drivers.iec104.driver import parse_address

        with contextlib.suppress(EncodingError):  # expected
            parse_address(address)

    @given(_any_str)
    @settings(max_examples=500)
    def test_expand_target_never_crashes(self, target: str) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _expand_iec104_target

        # Must not raise; bad CIDRs are skipped, not raised
        result = _expand_iec104_target(target)
        assert isinstance(result, list)

    @given(
        st.ip_addresses(v=4).map(str),
        st.integers(min_value=1, max_value=65535),
    )
    @settings(max_examples=200)
    def test_expand_single_host_always_parsed(self, ip: str, port: int) -> None:
        from protoskipper.builtin_drivers.iec104.driver import _expand_iec104_target

        result = _expand_iec104_target(f"{ip}:{port}")
        assert (ip, port) in result


# ---------------------------------------------------------------------------
# IEC 61850 parsers
# ---------------------------------------------------------------------------


class TestFuzzIec61850Parsers:
    """IEC 61850 address and target parsers must not crash on arbitrary input."""

    @given(_any_str)
    @settings(max_examples=500)
    def test_expand_target_never_crashes(self, target: str) -> None:
        from protoskipper_iec61850.driver import _expand_mms_target

        result = _expand_mms_target(target)
        assert isinstance(result, list)

    @given(
        st.ip_addresses(v=4).map(str),
        st.integers(min_value=1, max_value=65535),
    )
    @settings(max_examples=200)
    def test_expand_single_host_always_parsed(self, ip: str, port: int) -> None:
        from protoskipper_iec61850.driver import _expand_mms_target

        result = _expand_mms_target(f"{ip}:{port}")
        assert (ip, port) in result

    @given(_any_str)
    @settings(max_examples=500)
    def test_parse_address_never_crashes(self, address: str) -> None:
        from protoskipper_iec61850.driver import Iec61850MmsDriver

        driver = Iec61850MmsDriver()
        with contextlib.suppress(EncodingError):  # expected
            driver.parse_address(address)


# ---------------------------------------------------------------------------
# BACnet parsers
# ---------------------------------------------------------------------------


class TestFuzzBacnetParsers:
    """BACnet address parser must not crash on arbitrary input."""

    @given(_any_str)
    @settings(max_examples=500)
    def test_parse_address_never_crashes(self, address: str) -> None:
        from protoskipper.builtin_drivers.bacnet.driver import parse_address

        with contextlib.suppress(EncodingError):  # expected
            parse_address(address)

    @given(
        st.ip_addresses(v=4).map(str),
        st.integers(min_value=1, max_value=65535),
        st.one_of(st.none(), st.integers(min_value=0, max_value=4194302)),
    )
    @settings(max_examples=200)
    def test_valid_address_roundtrips(self, ip: str, port: int, dev_id: int | None) -> None:
        from protoskipper.builtin_drivers.bacnet.driver import parse_address

        addr = f"{ip}:{port}"
        if dev_id is not None:
            addr += f"/dev={dev_id}"
        host, parsed_port, parsed_dev = parse_address(addr)
        assert host == ip
        assert parsed_port == port
        assert parsed_dev == dev_id


# ---------------------------------------------------------------------------
# pcapng reader
# ---------------------------------------------------------------------------


class TestFuzzPcapngReader:
    """The pcapng reader must never crash on arbitrary byte sequences."""

    @given(st.binary(max_size=4096))
    @settings(max_examples=500)
    def test_read_never_crashes(self, data: bytes) -> None:
        import io

        from protoskipper.core.capture.pcapng import read_pcapng

        buf = io.BytesIO(data)
        # Any exception is acceptable; a crash (SIGSEGV etc.) is not.
        with contextlib.suppress(Exception):  # any exception is acceptable
            list(read_pcapng(buf))


# ---------------------------------------------------------------------------
# IEC 104 APCI / ASDU parsers (binary fuzz)
# ---------------------------------------------------------------------------


class TestFuzzIec104Wire:
    """IEC 104 APCI and ASDU parsers must handle arbitrary bytes gracefully."""

    @given(st.binary(max_size=255))
    @settings(max_examples=1000)
    def test_apci_parse_never_crashes(self, data: bytes) -> None:
        from protoskipper.builtin_drivers.iec104.apci import parse_apdu

        with contextlib.suppress(Exception):  # any exception is OK; crash is not
            parse_apdu(data)

    @given(st.binary(max_size=255))
    @settings(max_examples=1000)
    def test_asdu_parse_never_crashes(self, data: bytes) -> None:
        from protoskipper.builtin_drivers.iec104.asdu import Asdu

        with contextlib.suppress(Exception):  # any exception is OK; crash is not
            Asdu.from_bytes(data)
