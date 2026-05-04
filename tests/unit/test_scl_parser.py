# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for protoskipper_iec61850.scl (parser + model) — P8.A.2."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Inline SCL fixtures
# ---------------------------------------------------------------------------

# Fixture 1: Minimal SCD — 1 IED, 1 LDevice, LN0 + 2 LNs
_SCD_MINIMAL = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <SCL version="2007" revision="B" release="4"
         xmlns="http://www.iec.ch/61850/2003/SCL">
      <Header id="MINIMAL" nameStructure="IEDName"/>
      <Substation name="SS_TEST" desc="Test substation"/>
      <Communication>
        <SubNetwork name="MMS" desc="" type="8-MMS">
          <ConnectedAP iedName="IED1" apName="P1">
            <Address>
              <P type="IP">192.168.1.10</P>
              <P type="IP-SUBNET">255.255.255.0</P>
            </Address>
          </ConnectedAP>
        </SubNetwork>
      </Communication>
      <IED name="IED1" desc="Protection IED" manufacturer="Acme"
           type="ProRelay" configVersion="1.0">
        <AccessPoint name="P1">
          <Server>
            <LDevice inst="LD0" desc="Logical Device">
              <LN0 lnClass="LLN0" inst="" lnType="LLN0_T" desc="">
                <DataSet name="DS_PROT" desc="Protection outputs">
                  <FCDA ldInst="LD0" lnClass="PTRC" lnInst="1"
                        doName="Tr" daName="general" fc="ST"/>
                </DataSet>
                <ReportControl name="BRCB01" datSet="DS_PROT"
                               rptID="IED1LD0/LLN0.BR.BRCB01"
                               buffered="true" confRev="1"/>
                <GSEControl name="GoCB01" datSet="DS_PROT"
                            appID="0x0001" type="GOOSE"/>
              </LN0>
              <LN prefix="" lnClass="PTRC" inst="1" lnType="PTRC_T"
                  desc="Trip"/>
              <LN prefix="" lnClass="MMXU" inst="1" lnType="MMXU_T"
                  desc="Measurement"/>
            </LDevice>
          </Server>
        </AccessPoint>
      </IED>
      <DataTypeTemplates>
        <LNodeType id="LLN0_T" lnClass="LLN0">
          <DO name="Mod" type="ENC_Mode"/>
        </LNodeType>
        <LNodeType id="PTRC_T" lnClass="PTRC">
          <DO name="Tr" type="ACT"/>
        </LNodeType>
        <LNodeType id="MMXU_T" lnClass="MMXU">
          <DO name="A" type="WYE"/>
        </LNodeType>
        <DOType id="ENC_Mode" cdc="ENC"><DA name="stVal" fc="ST" bType="Enum"/></DOType>
        <DOType id="ACT" cdc="ACT"><DA name="general" fc="ST" bType="BOOLEAN"/></DOType>
        <DOType id="WYE" cdc="WYE"><DA name="phsA" fc="MX" bType="Struct" type="CMV"/></DOType>
        <DOType id="CMV" cdc="CMV"><DA name="cVal" fc="MX" bType="Struct" type="Vector"/></DOType>
        <DOType id="Vector" cdc="Vector">
          <DA name="mag" fc="MX" bType="Struct" type="AnalogueValue"/>
        </DOType>
        <DOType id="AnalogueValue" cdc="AnalogueValue">
          <DA name="f" fc="MX" bType="FLOAT32"/>
        </DOType>
        <DAType id="CMVda"><BDA name="cVal" bType="Struct" type="Vector"/></DAType>
      </DataTypeTemplates>
    </SCL>
""")

# Fixture 2: Two-IED SCD — 2 IEDs, multiple LDevices, DataSets, GSE + SV controls
_SCD_TWO_IEDS = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <SCL version="2007" revision="B" release="4"
         xmlns="http://www.iec.ch/61850/2003/SCL">
      <Header id="TWO_IED" nameStructure="IEDName"/>
      <Substation name="SS1" desc=""/>
      <Communication>
        <SubNetwork name="MMS" type="8-MMS">
          <ConnectedAP iedName="PROT1" apName="P1">
            <Address><P type="IP">10.0.0.1</P></Address>
          </ConnectedAP>
          <ConnectedAP iedName="MEAS1" apName="P1">
            <Address><P type="IP">10.0.0.2</P></Address>
          </ConnectedAP>
        </SubNetwork>
        <SubNetwork name="GOOSE" type="GOOSE">
          <ConnectedAP iedName="PROT1" apName="G1"/>
          <ConnectedAP iedName="MEAS1" apName="G1"/>
        </SubNetwork>
      </Communication>
      <IED name="PROT1" desc="Protection" manufacturer="VendorA" type="PA100">
        <AccessPoint name="P1">
          <Server>
            <LDevice inst="PROT" desc="Protection LD">
              <LN0 lnClass="LLN0" inst="" lnType="T1">
                <DataSet name="DS_STATUS">
                  <FCDA ldInst="PROT" lnClass="PTRC" lnInst="1"
                        doName="Tr" daName="general" fc="ST"/>
                  <FCDA ldInst="PROT" lnClass="PDIF" lnInst="1"
                        doName="Op" daName="general" fc="ST"/>
                </DataSet>
                <GSEControl name="GOOSE1" datSet="DS_STATUS"
                            appID="0x1001" type="GOOSE"/>
              </LN0>
              <LN lnClass="PTRC" inst="1" lnType="T2"/>
              <LN lnClass="PDIF" inst="1" lnType="T3"/>
            </LDevice>
            <LDevice inst="MEAS" desc="Measurement LD">
              <LN0 lnClass="LLN0" inst="" lnType="T1">
                <DataSet name="DS_MX">
                  <FCDA ldInst="MEAS" lnClass="MMXU" lnInst="1"
                        doName="A" daName="" fc="MX"/>
                </DataSet>
                <ReportControl name="URCB01" datSet="DS_MX"
                               rptID="PROT1MEAS/LLN0.RP.URCB01"
                               buffered="false" confRev="2"/>
              </LN0>
              <LN lnClass="MMXU" inst="1" lnType="T4"/>
            </LDevice>
          </Server>
        </AccessPoint>
      </IED>
      <IED name="MEAS1" desc="Merging unit" manufacturer="VendorB" type="MU200">
        <AccessPoint name="P1">
          <Server>
            <LDevice inst="MU0" desc="Merging unit LD">
              <LN0 lnClass="LLN0" inst="" lnType="T1">
                <DataSet name="DS_SV">
                  <FCDA ldInst="MU0" lnClass="TCTR" lnInst="1"
                        doName="Amp" daName="" fc="MX"/>
                </DataSet>
                <SampledValueControl name="MSVCB01" datSet="DS_SV"
                                     smvID="MEAS1MU0/LLN0.SM.MSVCB01"
                                     smpRate="80"/>
              </LN0>
              <LN lnClass="TCTR" inst="1" lnType="T5"/>
              <LN lnClass="TCTR" inst="2" lnType="T5"/>
              <LN lnClass="TCTR" inst="3" lnType="T5"/>
            </LDevice>
          </Server>
        </AccessPoint>
      </IED>
      <DataTypeTemplates>
        <LNodeType id="T1" lnClass="LLN0"><DO name="Mod" type="D1"/></LNodeType>
        <LNodeType id="T2" lnClass="PTRC"><DO name="Tr" type="D2"/></LNodeType>
        <LNodeType id="T3" lnClass="PDIF"><DO name="Op" type="D2"/></LNodeType>
        <LNodeType id="T4" lnClass="MMXU"><DO name="A" type="D3"/></LNodeType>
        <LNodeType id="T5" lnClass="TCTR"><DO name="Amp" type="D3"/></LNodeType>
        <DOType id="D1" cdc="ENC"><DA name="stVal" fc="ST" bType="Enum"/></DOType>
        <DOType id="D2" cdc="ACT"><DA name="general" fc="ST" bType="BOOLEAN"/></DOType>
        <DOType id="D3" cdc="WYE"><DA name="phsA" fc="MX" bType="FLOAT32"/></DOType>
      </DataTypeTemplates>
    </SCL>
""")

# Fixture 3: ICD (single IED config) — edge cases: no Substation, no Communication
_SCD_ICD = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <SCL version="2007" revision="A"
         xmlns="http://www.iec.ch/61850/2003/SCL">
      <Header id="ICD_SAMPLE" nameStructure="IEDName"/>
      <IED name="RELAY1" manufacturer="Vendor" type="R100">
        <AccessPoint name="P1">
          <Server>
            <LDevice inst="LD1">
              <LN0 lnClass="LLN0" inst="" lnType="LT1"/>
              <LN lnClass="XCBR" inst="1" lnType="LT2" desc="Circuit breaker"/>
              <LN lnClass="CSWI" inst="1" lnType="LT3" desc="Switch controller"/>
            </LDevice>
          </Server>
        </AccessPoint>
      </IED>
      <DataTypeTemplates>
        <LNodeType id="LT1" lnClass="LLN0"><DO name="Mod" type="DT1"/></LNodeType>
        <LNodeType id="LT2" lnClass="XCBR"><DO name="Pos" type="DT2"/></LNodeType>
        <LNodeType id="LT3" lnClass="CSWI"><DO name="Pos" type="DT2"/></LNodeType>
        <DOType id="DT1" cdc="ENC"><DA name="stVal" fc="ST" bType="Enum"/></DOType>
        <DOType id="DT2" cdc="DPC"><DA name="stVal" fc="ST" bType="Enum"/></DOType>
      </DataTypeTemplates>
    </SCL>
""")

# Edge case: missing SCL namespace
_SCD_NO_NS = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <SCL version="2007" revision="B">
      <Header id="NO_NS" nameStructure="IEDName"/>
      <IED name="IED_X" manufacturer="Test" type="TX1">
        <AccessPoint name="P1">
          <Server>
            <LDevice inst="LD0">
              <LN0 lnClass="LLN0" inst="" lnType="T1"/>
            </LDevice>
          </Server>
        </AccessPoint>
      </IED>
      <DataTypeTemplates>
        <LNodeType id="T1" lnClass="LLN0"><DO name="Mod" type="D1"/></LNodeType>
        <DOType id="D1" cdc="ENC"><DA name="stVal" fc="ST" bType="Enum"/></DOType>
      </DataTypeTemplates>
    </SCL>
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_scl(tmp_path: Path):
    """Factory fixture: write an SCL string to a temp file and return its path."""

    def _write(content: str, name: str = "sample.scd") -> Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    return _write


# ---------------------------------------------------------------------------
# Tests: TestParseMinimal
# ---------------------------------------------------------------------------


class TestParseMinimal:
    def test_returns_scl_document(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import SclDocument, parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        assert isinstance(doc, SclDocument)

    def test_version_fields(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        assert doc.version == "2007"
        assert doc.revision == "B"
        assert doc.release == "4"

    def test_ied_count(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        assert len(doc.ieds) == 1

    def test_ied_attributes(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        ied = doc.ieds[0]
        assert ied.name == "IED1"
        assert ied.desc == "Protection IED"
        assert ied.manufacturer == "Acme"
        assert ied.model == "ProRelay"
        assert ied.config_version == "1.0"

    def test_ldevice_count(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        ied = doc.ieds[0]
        assert len(ied.ldevices) == 1
        assert ied.ldevices[0].inst == "LD0"

    def test_ln0_present(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        ld = doc.ieds[0].ldevices[0]
        assert ld.ln0 is not None
        assert ld.ln0.ln_class == "LLN0"

    def test_ln_count(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        ld = doc.ieds[0].ldevices[0]
        assert len(ld.lns) == 2  # PTRC1 + MMXU1

    def test_lns_property_includes_ln0(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        # IED.lns = LN0 + 2 LNs
        assert len(doc.ieds[0].lns) == 3

    def test_dataset_parsed(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        ln0 = doc.ieds[0].ldevices[0].ln0
        assert ln0 is not None
        assert len(ln0.datasets) == 1
        assert ln0.datasets[0].name == "DS_PROT"

    def test_fcda_parsed(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        ds = doc.ieds[0].ldevices[0].ln0.datasets[0]  # type: ignore[union-attr]
        assert len(ds.fcdas) == 1
        fcda = ds.fcdas[0]
        assert fcda.ld_inst == "LD0"
        assert fcda.ln_class == "PTRC"
        assert fcda.fc == "ST"

    def test_report_control_parsed(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        ln0 = doc.ieds[0].ldevices[0].ln0
        assert ln0 is not None
        assert len(ln0.report_controls) == 1
        rcb = ln0.report_controls[0]
        assert rcb.name == "BRCB01"
        assert rcb.buffered is True
        assert rcb.dataset_ref == "DS_PROT"

    def test_gse_control_parsed(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        ln0 = doc.ieds[0].ldevices[0].ln0
        assert ln0 is not None
        assert len(ln0.gse_controls) == 1
        gse = ln0.gse_controls[0]
        assert gse.name == "GoCB01"
        assert gse.type == "GOOSE"
        assert gse.app_id == "0x0001"

    def test_substation_parsed(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        assert len(doc.substations) == 1
        assert doc.substations[0].name == "SS_TEST"

    def test_subnetwork_parsed(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        assert len(doc.subnetworks) == 1
        sn = doc.subnetworks[0]
        assert sn.name == "MMS"
        assert sn.type == "8-MMS"

    def test_connected_ap_parsed(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        cap = doc.subnetworks[0].connected_aps[0]
        assert cap.ied_name == "IED1"
        assert cap.ap_name == "P1"
        # Address: IP + IP-SUBNET
        assert len(cap.address) == 2
        addr_dict = dict(cap.address)
        assert addr_dict["IP"] == "192.168.1.10"
        assert addr_dict["IP-SUBNET"] == "255.255.255.0"

    def test_data_type_templates_preserved(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_MINIMAL))
        assert doc.data_type_templates_xml != b""
        assert b"DataTypeTemplates" in doc.data_type_templates_xml

    def test_source_file_recorded(self, tmp_path: Path) -> None:
        from protoskipper_iec61850.scl import parse

        p = tmp_path / "test.scd"
        p.write_text(_SCD_MINIMAL, encoding="utf-8")
        doc = parse(p)
        assert doc.source_file == str(p)


# ---------------------------------------------------------------------------
# Tests: TestParseTwoIeds
# ---------------------------------------------------------------------------


class TestParseTwoIeds:
    def test_ied_count(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        assert len(doc.ieds) == 2

    def test_ied_names(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        names = {ied.name for ied in doc.ieds}
        assert names == {"PROT1", "MEAS1"}

    def test_prot1_has_two_ldevices(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        prot1 = next(ied for ied in doc.ieds if ied.name == "PROT1")
        assert len(prot1.ldevices) == 2

    def test_meas1_has_three_lns(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        meas1 = next(ied for ied in doc.ieds if ied.name == "MEAS1")
        ld = meas1.ldevices[0]
        # LN0 + 3 TCTR instances
        assert len(ld.lns) == 3

    def test_goose_control_in_prot_ld(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        prot1 = next(ied for ied in doc.ieds if ied.name == "PROT1")
        prot_ld = next(ld for ld in prot1.ldevices if ld.inst == "PROT")
        assert prot_ld.ln0 is not None
        assert len(prot_ld.ln0.gse_controls) == 1
        assert prot_ld.ln0.gse_controls[0].name == "GOOSE1"

    def test_sv_control_in_meas1(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        meas1 = next(ied for ied in doc.ieds if ied.name == "MEAS1")
        ln0 = meas1.ldevices[0].ln0
        assert ln0 is not None
        assert len(ln0.sv_controls) == 1
        svc = ln0.sv_controls[0]
        assert svc.name == "MSVCB01"
        assert svc.smp_rate == 80

    def test_two_subnetworks(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        assert len(doc.subnetworks) == 2
        types = {sn.type for sn in doc.subnetworks}
        assert types == {"8-MMS", "GOOSE"}

    def test_dataset_fcda_count(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        prot1 = next(ied for ied in doc.ieds if ied.name == "PROT1")
        prot_ld = next(ld for ld in prot1.ldevices if ld.inst == "PROT")
        ds = prot_ld.ln0.datasets[0]  # type: ignore[union-attr]
        assert ds.name == "DS_STATUS"
        assert len(ds.fcdas) == 2


# ---------------------------------------------------------------------------
# Tests: TestParseIcd (single-IED, no Communication, Edition 1 marker)
# ---------------------------------------------------------------------------


class TestParseIcd:
    def test_ied_count(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_ICD))
        assert len(doc.ieds) == 1

    def test_edition1_revision(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_ICD))
        assert doc.revision == "A"

    def test_no_subnetworks(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_ICD))
        assert doc.subnetworks == ()

    def test_no_substations(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_ICD))
        assert doc.substations == ()

    def test_lns_count(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_ICD))
        ied = doc.ieds[0]
        # LN0 + XCBR1 + CSWI1 = 3
        assert len(ied.lns) == 3


# ---------------------------------------------------------------------------
# Tests: TestEdgeCases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_missing_namespace_still_parses(self, tmp_scl) -> None:
        """Parser must not crash on missing namespace; IED count = 1."""
        from protoskipper_iec61850.scl import parse

        doc = parse(tmp_scl(_SCD_NO_NS, "no_ns.scd"))
        assert len(doc.ieds) == 1
        assert doc.ieds[0].name == "IED_X"

    def test_malformed_xml_raises(self, tmp_path: Path) -> None:
        from lxml.etree import XMLSyntaxError
        from protoskipper_iec61850.scl import parse

        bad = tmp_path / "bad.scd"
        bad.write_text("<SCL><unclosed>", encoding="utf-8")
        with pytest.raises(XMLSyntaxError):
            parse(bad)

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        from protoskipper_iec61850.scl import parse

        with pytest.raises(OSError):
            parse(tmp_path / "nonexistent.scd")


# ---------------------------------------------------------------------------
# Tests: TestValidate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_doc_no_issues(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse, validate

        doc = parse(tmp_scl(_SCD_MINIMAL))
        issues = validate(doc)
        assert issues == []

    def test_two_ied_doc_no_issues(self, tmp_scl) -> None:
        from protoskipper_iec61850.scl import parse, validate

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        issues = validate(doc)
        assert issues == []

    def test_duplicate_ied_name_is_error(self) -> None:
        from protoskipper_iec61850.scl import validate
        from protoskipper_iec61850.scl.model import IED, SclDocument

        ied1 = IED(name="IED1")
        ied2 = IED(name="IED1")  # duplicate
        doc = SclDocument(ieds=(ied1, ied2))
        issues = validate(doc)
        assert any(i.rule == "V02" and i.severity == "error" for i in issues)

    def test_missing_ied_name_is_error(self) -> None:
        from protoskipper_iec61850.scl import validate
        from protoskipper_iec61850.scl.model import IED, SclDocument

        doc = SclDocument(ieds=(IED(name=""),))
        issues = validate(doc)
        assert any(i.rule == "V02" and i.severity == "error" for i in issues)

    def test_duplicate_ldevice_inst_is_error(self) -> None:
        from protoskipper_iec61850.scl import validate
        from protoskipper_iec61850.scl.model import IED, AccessPoint, LDevice, SclDocument

        ap = AccessPoint(
            name="P1",
            ldevices=(LDevice(inst="LD0"), LDevice(inst="LD0")),  # duplicate
        )
        doc = SclDocument(ieds=(IED(name="IED1", access_points=(ap,)),))
        issues = validate(doc)
        assert any(i.rule == "V03" and i.severity == "error" for i in issues)

    def test_rcb_unknown_dataset_is_warning(self) -> None:
        from protoskipper_iec61850.scl import validate
        from protoskipper_iec61850.scl.model import (
            IED,
            LN,
            AccessPoint,
            LDevice,
            ReportControl,
            SclDocument,
        )

        rcb = ReportControl(name="BRCB01", dataset_ref="MISSING_DS")
        ln0 = LN(ln_class="LLN0", report_controls=(rcb,))
        ld = LDevice(inst="LD0", ln0=ln0)
        ap = AccessPoint(name="P1", ldevices=(ld,))
        doc = SclDocument(ieds=(IED(name="IED1", access_points=(ap,)),))
        issues = validate(doc)
        assert any(i.rule == "V05" and i.severity == "warning" for i in issues)


# ---------------------------------------------------------------------------
# Tests: TestWrite (round-trip)
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_produces_file(self, tmp_scl, tmp_path: Path) -> None:
        from protoskipper_iec61850.scl import parse, write

        doc = parse(tmp_scl(_SCD_MINIMAL))
        out = tmp_path / "out.scd"
        write(doc, out)
        assert out.exists()
        assert out.stat().st_size > 0

    def test_written_file_is_valid_xml(self, tmp_scl, tmp_path: Path) -> None:
        from lxml import etree
        from protoskipper_iec61850.scl import parse, write

        doc = parse(tmp_scl(_SCD_MINIMAL))
        out = tmp_path / "out.scd"
        write(doc, out)
        # Must parse without raising
        tree = etree.parse(str(out))
        assert tree is not None

    def test_round_trip_ied_names(self, tmp_scl, tmp_path: Path) -> None:
        from protoskipper_iec61850.scl import parse, write

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        out = tmp_path / "rt.scd"
        write(doc, out)
        doc2 = parse(out)
        assert {ied.name for ied in doc2.ieds} == {ied.name for ied in doc.ieds}

    def test_round_trip_ldevice_insts(self, tmp_scl, tmp_path: Path) -> None:
        from protoskipper_iec61850.scl import parse, write

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        out = tmp_path / "rt2.scd"
        write(doc, out)
        doc2 = parse(out)
        prot1_orig = next(ied for ied in doc.ieds if ied.name == "PROT1")
        prot1_rt = next(ied for ied in doc2.ieds if ied.name == "PROT1")
        assert {ld.inst for ld in prot1_orig.ldevices} == {ld.inst for ld in prot1_rt.ldevices}

    def test_round_trip_preserves_data_type_templates(self, tmp_scl, tmp_path: Path) -> None:
        from lxml import etree
        from protoskipper_iec61850.scl import parse, write

        doc = parse(tmp_scl(_SCD_MINIMAL))
        out = tmp_path / "dtt.scd"
        write(doc, out)
        tree = etree.parse(str(out))
        root = tree.getroot()
        ns = "http://www.iec.ch/61850/2003/SCL"
        dtt = root.find(f"{{{ns}}}DataTypeTemplates")
        assert dtt is not None, "DataTypeTemplates must be present in round-trip output"

    def test_round_trip_subnetworks(self, tmp_scl, tmp_path: Path) -> None:
        from protoskipper_iec61850.scl import parse, write

        doc = parse(tmp_scl(_SCD_TWO_IEDS))
        out = tmp_path / "sn.scd"
        write(doc, out)
        doc2 = parse(out)
        assert len(doc2.subnetworks) == len(doc.subnetworks)
        assert {sn.name for sn in doc2.subnetworks} == {sn.name for sn in doc.subnetworks}

    def test_write_icd_no_subnetworks(self, tmp_scl, tmp_path: Path) -> None:
        from protoskipper_iec61850.scl import parse, write

        doc = parse(tmp_scl(_SCD_ICD))
        out = tmp_path / "icd_out.icd"
        write(doc, out)
        doc2 = parse(out)
        assert doc2.subnetworks == ()
        assert len(doc2.ieds) == 1
