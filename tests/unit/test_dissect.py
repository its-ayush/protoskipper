# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for dissect subpackage — P8.H."""

from __future__ import annotations

import struct

import pytest
from protoskipper_iec61850.dissect.filter_lang import (
    FilterSyntaxError,
    compile_filter,
)
from protoskipper_iec61850.dissect.goose import GooseDissection, decode_goose_frame
from protoskipper_iec61850.dissect.mms import (
    MMS_CONFIRMED_REQUEST,
    MMS_CONFIRMED_RESPONSE,
    decode_mms_frame,
)
from protoskipper_iec61850.dissect.sv import decode_sv_frame

# ===========================================================================
# Helpers
# ===========================================================================


def _make_ber_tlv(tag: int, data: bytes) -> bytes:
    """Encode a simple BER TLV (short form only)."""
    return bytes([tag, len(data)]) + data


def _encode_goose_frame(
    go_cb_ref: str = "IED/LLN0$GO$gcb1",
    go_id: str = "GooseID",
    dat_set: str = "IED/LLN0$DS1",
    st_num: int = 1,
    sq_num: int = 0,
    conf_rev: int = 1,
    app_id: int = 0x0001,
    vlan: bool = False,
    with_boolean_data: bool = False,
) -> bytes:
    """Build a minimal valid GOOSE Ethernet frame."""
    # Encode GOOSE PDU fields
    gocbref_bytes = go_cb_ref.encode("ascii")
    go_id_bytes = go_id.encode("ascii")
    dat_set_bytes = dat_set.encode("ascii")
    utctime = b"\x00" * 8  # t = 0

    pdu_body = (
        _make_ber_tlv(0x80, gocbref_bytes)  # gocbRef
        + _make_ber_tlv(0x81, b"\x00\xfa")  # timeAllowedtoLive = 250 ms
        + _make_ber_tlv(0x82, dat_set_bytes)  # datSet
        + _make_ber_tlv(0x83, go_id_bytes)  # goID
        + _make_ber_tlv(0x84, utctime)  # t (UtcTime)
        + _make_ber_tlv(0x85, st_num.to_bytes(1, "big"))  # stNum
        + _make_ber_tlv(0x86, sq_num.to_bytes(1, "big"))  # sqNum
        + _make_ber_tlv(0x87, b"\x00")  # test = false
        + _make_ber_tlv(0x88, conf_rev.to_bytes(1, "big"))  # confRev
        + _make_ber_tlv(0x89, b"\x00")  # ndsCom = false
        + _make_ber_tlv(0x8A, b"\x01")  # numDatSetEntries = 1
    )

    if with_boolean_data:
        # allData with one boolean value
        bool_val = _make_ber_tlv(0x83, b"\x01")  # MMS boolean = true
        pdu_body += _make_ber_tlv(0xAB, bool_val)
    else:
        pdu_body += _make_ber_tlv(0xAB, b"")  # empty allData

    goose_pdu = _make_ber_tlv(0x61, pdu_body)

    # Common header
    common_header = struct.pack(">HHHH", app_id, len(goose_pdu) + 8, 0, 0)
    apdu = common_header + goose_pdu

    # Ethernet header
    dst_mac = bytes([0x01, 0x0C, 0xCD, 0x01, 0x00, 0x01])
    src_mac = bytes([0x00, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE])

    if vlan:
        vlan_tag = struct.pack(">HH", 0x8100, 0x0064)  # VLAN 100
        ethertype = struct.pack(">H", 0x88B8)
        return dst_mac + src_mac + vlan_tag + ethertype + apdu
    else:
        ethertype = struct.pack(">H", 0x88B8)
        return dst_mac + src_mac + ethertype + apdu


# ===========================================================================
# TestGooseDissect
# ===========================================================================


class TestGooseDissect:
    def test_decode_basic(self):
        frame = _encode_goose_frame()
        disc = decode_goose_frame(frame)
        assert disc is not None
        assert disc.go_cb_ref == "IED/LLN0$GO$gcb1"
        assert disc.go_id == "GooseID"
        assert disc.dat_set == "IED/LLN0$DS1"
        assert disc.st_num == 1
        assert disc.sq_num == 0
        assert disc.conf_rev == 1
        assert disc.app_id == 0x0001
        assert disc.test is False
        assert disc.nds_comm is False
        assert disc.num_dataset_entries == 1
        assert disc.time_allowed_ms == 250

    def test_decode_with_vlan(self):
        frame = _encode_goose_frame(vlan=True)
        disc = decode_goose_frame(frame)
        assert disc is not None
        assert disc.vlan_id == 100

    def test_decode_without_vlan(self):
        frame = _encode_goose_frame(vlan=False)
        disc = decode_goose_frame(frame)
        assert disc is not None
        assert disc.vlan_id is None

    def test_mac_addresses_formatted(self):
        frame = _encode_goose_frame()
        disc = decode_goose_frame(frame)
        assert disc is not None
        assert disc.dst_mac == "01:0c:cd:01:00:01"
        assert disc.src_mac == "00:aa:bb:cc:dd:ee"

    def test_decode_boolean_data(self):
        frame = _encode_goose_frame(with_boolean_data=True)
        disc = decode_goose_frame(frame)
        assert disc is not None
        assert len(disc.all_data) == 1
        assert disc.all_data[0] is True

    def test_non_goose_frame_returns_none(self):
        # EtherType 0x0800 (IPv4) — should return None
        payload = b"\x00" * 50
        # Craft Ethernet frame with wrong EtherType
        dst = b"\xff\xff\xff\xff\xff\xff"
        src = b"\x00" * 6
        etype = struct.pack(">H", 0x0800)
        frame = dst + src + etype + payload
        assert decode_goose_frame(frame) is None

    def test_short_frame_returns_none(self):
        assert decode_goose_frame(b"\x00" * 5) is None

    def test_wrong_pdu_tag_returns_none(self):
        # Correct EtherType but bad PDU tag
        dst = b"\x01\x0c\xcd\x01\x00\x00"
        src = b"\x00" * 6
        etype = struct.pack(">H", 0x88B8)
        common_hdr = struct.pack(">HHHH", 0, 12, 0, 0)
        bad_pdu = b"\x62\x00"  # tag 0x62, not 0x61
        frame = dst + src + etype + common_hdr + bad_pdu
        assert decode_goose_frame(frame) is None

    def test_high_sequence_numbers(self):
        frame = _encode_goose_frame(st_num=255, sq_num=200)
        disc = decode_goose_frame(frame)
        assert disc is not None
        assert disc.st_num == 255
        assert disc.sq_num == 200


# ===========================================================================
# TestSvDissect
# ===========================================================================


class TestSvDissect:
    def test_non_sv_frame_returns_none(self):
        dst = b"\x01\x0c\xcd\x04\x00\x00"
        src = b"\x00" * 6
        etype = struct.pack(">H", 0x0800)  # not SV
        frame = dst + src + etype + b"\x00" * 20
        assert decode_sv_frame(frame) is None

    def test_short_frame_returns_none(self):
        assert decode_sv_frame(b"\x00" * 8) is None


# ===========================================================================
# TestMmsDissect
# ===========================================================================


class TestMmsDissect:
    def _make_tpkt_cotp(self, mms_payload: bytes) -> bytes:
        """Wrap MMS payload in TPKT + COTP DT header."""
        total_len = 4 + 3 + len(mms_payload)
        tpkt = struct.pack(">BBH", 0x03, 0x00, total_len)  # version=3
        cotp_dt = bytes([0x00, 0x01, 0x80])  # COTP DT, PDU type 0x0F
        return tpkt + cotp_dt + mms_payload

    def _make_confirmed_request(self, invoke_id: int = 5) -> bytes:
        """Build a minimal MMS ConfirmedRequest PDU."""
        invoke_id_bytes = struct.pack(">H", invoke_id)
        invoke_id_tlv = bytes([0x02, len(invoke_id_bytes)]) + invoke_id_bytes
        # Service: Read (tag 0xA0)
        service_tlv = bytes([0xA0, 0x00])
        content = invoke_id_tlv + service_tlv
        pdu = bytes([MMS_CONFIRMED_REQUEST, len(content)]) + content
        return self._make_tpkt_cotp(pdu)

    def _make_confirmed_response(self, invoke_id: int = 5) -> bytes:
        """Build a minimal MMS ConfirmedResponse PDU."""
        invoke_id_bytes = struct.pack(">H", invoke_id)
        invoke_id_tlv = bytes([0x02, len(invoke_id_bytes)]) + invoke_id_bytes
        service_tlv = bytes([0xA0, 0x00])  # Read response
        content = invoke_id_tlv + service_tlv
        pdu = bytes([MMS_CONFIRMED_RESPONSE, len(content)]) + content
        return self._make_tpkt_cotp(pdu)

    def test_decode_confirmed_request(self):
        payload = self._make_confirmed_request(invoke_id=42)
        disc = decode_mms_frame(payload)
        assert disc is not None
        assert disc.pdu_type == MMS_CONFIRMED_REQUEST
        assert disc.pdu_type_name == "ConfirmedRequest"
        assert disc.invoke_id == 42

    def test_decode_confirmed_response(self):
        payload = self._make_confirmed_response(invoke_id=42)
        disc = decode_mms_frame(payload)
        assert disc is not None
        assert disc.pdu_type == MMS_CONFIRMED_RESPONSE
        assert disc.pdu_type_name == "ConfirmedResponse"
        assert disc.invoke_id == 42

    def test_too_short_returns_none(self):
        assert decode_mms_frame(b"\x03\x00") is None

    def test_no_mms_tag_returns_none(self):
        # TPKT header with no recognisable MMS tag
        payload = struct.pack(">BBH", 0x03, 0x00, 0x08) + b"\xff" * 4
        assert decode_mms_frame(payload) is None


# ===========================================================================
# TestFilterLang
# ===========================================================================


class TestFilterLang:
    def _make_dissections(
        self,
        goose: GooseDissection | None = None,
        sv: object = None,
        mms: object = None,
    ) -> dict:
        return {"goose": goose, "sv": sv, "mms": mms}

    def _make_goose(self, **kwargs) -> GooseDissection:
        defaults = dict(
            src_mac="00:aa:bb:cc:dd:ee",
            dst_mac="01:0c:cd:01:00:01",
            vlan_id=None,
            app_id=1,
            go_cb_ref="IED1/LLN0$GO$gcb1",
            go_id="GoID1",
            dat_set="IED1/LLN0$DS1",
            t_ms=0,
            st_num=3,
            sq_num=1,
            conf_rev=2,
            time_allowed_ms=500,
            test=False,
            nds_comm=False,
            num_dataset_entries=0,
            all_data=[],
            raw_apdu=b"",
        )
        defaults.update(kwargs)
        return GooseDissection(**defaults)

    def test_simple_eq(self):
        flt = compile_filter('goose.gocbref == "IED1/LLN0$GO$gcb1"')
        goose = self._make_goose()
        assert flt.match(self._make_dissections(goose=goose)) is True

    def test_simple_eq_fail(self):
        flt = compile_filter('goose.gocbref == "other"')
        goose = self._make_goose()
        assert flt.match(self._make_dissections(goose=goose)) is False

    def test_neq(self):
        flt = compile_filter("goose.stnum != 99")
        goose = self._make_goose(st_num=3)
        assert flt.match(self._make_dissections(goose=goose)) is True

    def test_gt(self):
        flt = compile_filter("goose.stnum > 2")
        goose = self._make_goose(st_num=3)
        assert flt.match(self._make_dissections(goose=goose)) is True

    def test_lte(self):
        flt = compile_filter("goose.stnum <= 3")
        goose = self._make_goose(st_num=3)
        assert flt.match(self._make_dissections(goose=goose)) is True

    def test_regex_match(self):
        flt = compile_filter('goose.gocbref ~= "IED1.*"')
        goose = self._make_goose()
        assert flt.match(self._make_dissections(goose=goose)) is True

    def test_regex_no_match(self):
        flt = compile_filter('goose.gocbref ~= "^IED9"')
        goose = self._make_goose()
        assert flt.match(self._make_dissections(goose=goose)) is False

    def test_and_both_true(self):
        flt = compile_filter("goose.stnum == 3 and goose.sqnum == 1")
        goose = self._make_goose()
        assert flt.match(self._make_dissections(goose=goose)) is True

    def test_and_one_false(self):
        flt = compile_filter("goose.stnum == 3 and goose.sqnum == 99")
        goose = self._make_goose()
        assert flt.match(self._make_dissections(goose=goose)) is False

    def test_or_one_true(self):
        flt = compile_filter("goose.stnum == 99 or goose.sqnum == 1")
        goose = self._make_goose()
        assert flt.match(self._make_dissections(goose=goose)) is True

    def test_not(self):
        flt = compile_filter("not goose.test == true")
        goose = self._make_goose(test=False)
        assert flt.match(self._make_dissections(goose=goose)) is True

    def test_parentheses(self):
        flt = compile_filter("(goose.stnum == 3) and (goose.sqnum == 1)")
        goose = self._make_goose()
        assert flt.match(self._make_dissections(goose=goose)) is True

    def test_missing_proto_returns_false(self):
        flt = compile_filter('sv.svid == "test"')
        # No SV dissection
        assert flt.match(self._make_dissections()) is False

    def test_invalid_syntax_raises(self):
        with pytest.raises(FilterSyntaxError):
            compile_filter("goose.stnum === 3")  # triple equals invalid

    def test_unclosed_paren_raises(self):
        with pytest.raises(FilterSyntaxError):
            compile_filter("(goose.stnum == 3")

    def test_missing_value_raises(self):
        with pytest.raises(FilterSyntaxError):
            compile_filter("goose.stnum ==")

    def test_complex_expression(self):
        flt = compile_filter(
            'goose.gocbref == "IED1/LLN0$GO$gcb1" and goose.stnum > 0 and not goose.test == true'
        )
        goose = self._make_goose()
        assert flt.match(self._make_dissections(goose=goose)) is True

    def test_filter_source_preserved(self):
        src = "goose.stnum == 5"
        flt = compile_filter(src)
        assert flt.source == src
