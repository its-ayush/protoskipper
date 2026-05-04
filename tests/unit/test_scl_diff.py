# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Unit tests for protoskipper_iec61850.scl.diff — P8.A.3.

Six required cases from the spec plus round-trip and summary helpers:
  1. add IED
  2. remove IED
  3. rename LN (lnType change — identity key unchanged; desc change)
  4. change DataTypeTemplates (opaque blob diff proxy for "change DAType")
  5. change GoCB attribute (app_id — proxy for "change GoCB MAC/appID")
  6. add DataSet member (add FCDA)
Plus:
  7. diff(a, a) → empty
  8. apply(a, diff(a, b)) == b  (round-trip)
  9. summary() non-empty on non-empty diff
"""

from __future__ import annotations

import pytest
from protoskipper_iec61850.scl.diff import Change, SclDiff, apply, diff, summary
from protoskipper_iec61850.scl.model import (
    FCDA,
    IED,
    LN,
    AccessPoint,
    ConnectedAP,
    DataSet,
    GseControl,
    LDevice,
    SclDocument,
    SubNetwork,
    Substation,
)

# ---------------------------------------------------------------------------
# Helpers to build minimal SclDocument fixtures
# ---------------------------------------------------------------------------


def _make_ln0(gse_controls: tuple[GseControl, ...] = (), datasets: tuple[DataSet, ...] = ()) -> LN:
    return LN(ln_class="LLN0", ln_type="LT0", datasets=datasets, gse_controls=gse_controls)


def _make_ied(
    name: str,
    ld_inst: str = "LD0",
    ln0: LN | None = None,
    lns: tuple[LN, ...] = (),
) -> IED:
    if ln0 is None:
        ln0 = _make_ln0()
    ap = AccessPoint(
        name="P1",
        ldevices=(LDevice(inst=ld_inst, ln0=ln0, lns=lns),),
    )
    return IED(name=name, access_points=(ap,))


def _make_doc(
    ieds: tuple[IED, ...] = (),
    subnetworks: tuple[SubNetwork, ...] = (),
    substations: tuple[Substation, ...] = (),
    dtt: bytes = b"",
) -> SclDocument:
    return SclDocument(
        version="2007",
        revision="B",
        release="4",
        ieds=ieds,
        subnetworks=subnetworks,
        substations=substations,
        data_type_templates_xml=dtt,
    )


# ---------------------------------------------------------------------------
# Case 1: Add IED
# ---------------------------------------------------------------------------


class TestAddIed:
    def test_diff_detects_added_ied(self) -> None:
        a = _make_doc(ieds=(_make_ied("IED1"),))
        b = _make_doc(ieds=(_make_ied("IED1"), _make_ied("IED2")))
        d = diff(a, b)
        assert bool(d)
        added = [c for c in d.changes if c.kind == "added" and "IED[IED2]" in c.path]
        assert len(added) == 1
        assert added[0].new_value.name == "IED2"

    def test_apply_adds_ied(self) -> None:
        ied1 = _make_ied("IED1")
        ied2 = _make_ied("IED2")
        a = _make_doc(ieds=(ied1,))
        b = _make_doc(ieds=(ied1, ied2))
        result = apply(a, diff(a, b))
        assert {ied.name for ied in result.ieds} == {"IED1", "IED2"}


# ---------------------------------------------------------------------------
# Case 2: Remove IED
# ---------------------------------------------------------------------------


class TestRemoveIed:
    def test_diff_detects_removed_ied(self) -> None:
        a = _make_doc(ieds=(_make_ied("IED1"), _make_ied("IED2")))
        b = _make_doc(ieds=(_make_ied("IED1"),))
        d = diff(a, b)
        removed = [c for c in d.changes if c.kind == "removed" and "IED[IED2]" in c.path]
        assert len(removed) == 1
        assert removed[0].old_value.name == "IED2"

    def test_apply_removes_ied(self) -> None:
        ied1 = _make_ied("IED1")
        ied2 = _make_ied("IED2")
        a = _make_doc(ieds=(ied1, ied2))
        b = _make_doc(ieds=(ied1,))
        result = apply(a, diff(a, b))
        assert {ied.name for ied in result.ieds} == {"IED1"}

    def test_no_ied_left(self) -> None:
        a = _make_doc(ieds=(_make_ied("IED1"),))
        b = _make_doc()
        result = apply(a, diff(a, b))
        assert result.ieds == ()


# ---------------------------------------------------------------------------
# Case 3: Rename LN (lnType change — same identity key, different lnType)
# ---------------------------------------------------------------------------


class TestRenameLn:
    def _make_docs(self) -> tuple[SclDocument, SclDocument]:
        ln_a = LN(prefix="", ln_class="PTRC", inst="1", ln_type="OLD_TYPE", desc="")
        ln_b = LN(prefix="", ln_class="PTRC", inst="1", ln_type="NEW_TYPE", desc="renamed")
        ap_a = AccessPoint(name="P1", ldevices=(LDevice(inst="LD0", ln0=_make_ln0(), lns=(ln_a,)),))
        ap_b = AccessPoint(name="P1", ldevices=(LDevice(inst="LD0", ln0=_make_ln0(), lns=(ln_b,)),))
        ied_a = IED(name="IED1", access_points=(ap_a,))
        ied_b = IED(name="IED1", access_points=(ap_b,))
        return _make_doc(ieds=(ied_a,)), _make_doc(ieds=(ied_b,))

    def test_diff_detects_modified_ln(self) -> None:
        a, b = self._make_docs()
        d = diff(a, b)
        modified = [c for c in d.changes if c.kind == "modified" and "LN[.PTRC.1]" in c.path]
        assert len(modified) == 1
        assert modified[0].new_value.ln_type == "NEW_TYPE"

    def test_apply_updates_ln_type(self) -> None:
        a, b = self._make_docs()
        result = apply(a, diff(a, b))
        ied = result.ieds[0]
        ln = ied.ldevices[0].lns[0]
        assert ln.ln_type == "NEW_TYPE"
        assert ln.desc == "renamed"


# ---------------------------------------------------------------------------
# Case 4: Change DataTypeTemplates (opaque blob — stands for "change DAType")
# ---------------------------------------------------------------------------


class TestChangeDtt:
    def test_diff_detects_dtt_change(self) -> None:
        a = _make_doc(dtt=b"<DataTypeTemplates><LNodeType id='T1'/></DataTypeTemplates>")
        b = _make_doc(dtt=b"<DataTypeTemplates><LNodeType id='T2'/></DataTypeTemplates>")
        d = diff(a, b)
        dtt_changes = [c for c in d.changes if c.path == "DataTypeTemplates"]
        assert len(dtt_changes) == 1
        assert dtt_changes[0].kind == "modified"

    def test_diff_no_change_when_same_dtt(self) -> None:
        blob = b"<DataTypeTemplates><LNodeType id='T1'/></DataTypeTemplates>"
        a = _make_doc(dtt=blob)
        b = _make_doc(dtt=blob)
        d = diff(a, b)
        assert not any(c.path == "DataTypeTemplates" for c in d.changes)

    def test_apply_updates_dtt(self) -> None:
        old_dtt = b"<DataTypeTemplates><LNodeType id='T1'/></DataTypeTemplates>"
        new_dtt = b"<DataTypeTemplates><LNodeType id='T2'/></DataTypeTemplates>"
        a = _make_doc(dtt=old_dtt)
        b = _make_doc(dtt=new_dtt)
        result = apply(a, diff(a, b))
        assert result.data_type_templates_xml == new_dtt


# ---------------------------------------------------------------------------
# Case 5: Change GoCB attribute (appID — proxy for "change GoCB MAC")
# ---------------------------------------------------------------------------


class TestChangeGoCbAppId:
    def _make_docs(self) -> tuple[SclDocument, SclDocument]:
        gse_a = GseControl(name="GoCB01", dataset_ref="DS", app_id="0x0001", type="GOOSE")
        gse_b = GseControl(name="GoCB01", dataset_ref="DS", app_id="0x0002", type="GOOSE")
        ln0_a = _make_ln0(gse_controls=(gse_a,))
        ln0_b = _make_ln0(gse_controls=(gse_b,))
        return _make_doc(ieds=(_make_ied("IED1", ln0=ln0_a),)), _make_doc(
            ieds=(_make_ied("IED1", ln0=ln0_b),)
        )

    def test_diff_detects_gocb_change(self) -> None:
        a, b = self._make_docs()
        d = diff(a, b)
        gse_changes = [c for c in d.changes if "GSEControl[GoCB01]" in c.path]
        assert len(gse_changes) == 1
        assert gse_changes[0].kind == "modified"
        assert gse_changes[0].new_value.app_id == "0x0002"

    def test_apply_updates_gocb_appid(self) -> None:
        a, b = self._make_docs()
        result = apply(a, diff(a, b))
        ied = result.ieds[0]
        ln0 = ied.ldevices[0].ln0
        assert ln0 is not None
        assert ln0.gse_controls[0].app_id == "0x0002"


# ---------------------------------------------------------------------------
# Case 6: Add DataSet member (add FCDA)
# ---------------------------------------------------------------------------


class TestAddDataSetMember:
    def _make_docs(self) -> tuple[SclDocument, SclDocument]:
        fcda1 = FCDA(ld_inst="LD0", ln_class="PTRC", ln_inst="1", do_name="Tr", fc="ST")
        fcda2 = FCDA(ld_inst="LD0", ln_class="MMXU", ln_inst="1", do_name="A", fc="MX")
        ds_a = DataSet(name="DS_PROT", fcdas=(fcda1,))
        ds_b = DataSet(name="DS_PROT", fcdas=(fcda1, fcda2))
        ln0_a = _make_ln0(datasets=(ds_a,))
        ln0_b = _make_ln0(datasets=(ds_b,))
        return _make_doc(ieds=(_make_ied("IED1", ln0=ln0_a),)), _make_doc(
            ieds=(_make_ied("IED1", ln0=ln0_b),)
        )

    def test_diff_detects_dataset_change(self) -> None:
        a, b = self._make_docs()
        d = diff(a, b)
        ds_changes = [c for c in d.changes if "DataSet[DS_PROT]" in c.path]
        assert len(ds_changes) == 1
        assert ds_changes[0].kind == "modified"
        assert len(ds_changes[0].new_value.fcdas) == 2

    def test_apply_adds_fcda(self) -> None:
        a, b = self._make_docs()
        result = apply(a, diff(a, b))
        ln0 = result.ieds[0].ldevices[0].ln0
        assert ln0 is not None
        ds = ln0.datasets[0]
        assert len(ds.fcdas) == 2
        assert ds.fcdas[1].ln_class == "MMXU"


# ---------------------------------------------------------------------------
# Round-trip and identity guarantees
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_diff_self_is_empty(self) -> None:
        """diff(a, a) must return an empty SclDiff."""
        ied = _make_ied("IED1")
        a = _make_doc(ieds=(ied,))
        d = diff(a, a)
        assert not bool(d)
        assert len(d) == 0

    def test_apply_identity(self) -> None:
        """apply(a, diff(a, a)) == a."""
        ied = _make_ied("IED1")
        a = _make_doc(
            ieds=(ied,),
            substations=(Substation(name="SS1"),),
            subnetworks=(
                SubNetwork(
                    name="MMS",
                    type="8-MMS",
                    connected_aps=(ConnectedAP(ied_name="IED1", ap_name="P1"),),
                ),
            ),
        )
        result = apply(a, diff(a, a))
        assert result.ieds == a.ieds
        assert result.subnetworks == a.subnetworks
        assert result.substations == a.substations

    def test_apply_produces_b(self) -> None:
        """apply(a, diff(a, b)) must produce b field-for-field."""
        ied1 = _make_ied("IED1")
        ied2 = _make_ied("IED2")
        a = _make_doc(ieds=(ied1,))
        b = _make_doc(ieds=(ied1, ied2))
        result = apply(a, diff(a, b))
        assert {i.name for i in result.ieds} == {i.name for i in b.ieds}

    def test_full_roundtrip_with_subnetwork_change(self) -> None:
        sn_a = SubNetwork(
            name="MMS",
            type="8-MMS",
            connected_aps=(
                ConnectedAP(ied_name="IED1", ap_name="P1", address=(("IP", "10.0.0.1"),)),
            ),
        )
        sn_b = SubNetwork(
            name="MMS",
            type="8-MMS",
            connected_aps=(
                ConnectedAP(ied_name="IED1", ap_name="P1", address=(("IP", "10.0.0.99"),)),
            ),
        )
        a = _make_doc(subnetworks=(sn_a,))
        b = _make_doc(subnetworks=(sn_b,))
        result = apply(a, diff(a, b))
        cap = result.subnetworks[0].connected_aps[0]
        assert dict(cap.address)["IP"] == "10.0.0.99"

    def test_roundtrip_ldevice_add(self) -> None:
        ap_a = AccessPoint(name="P1", ldevices=(LDevice(inst="LD0", ln0=_make_ln0()),))
        ap_b = AccessPoint(
            name="P1",
            ldevices=(
                LDevice(inst="LD0", ln0=_make_ln0()),
                LDevice(inst="LD1", ln0=_make_ln0()),
            ),
        )
        ied_a = IED(name="IED1", access_points=(ap_a,))
        ied_b = IED(name="IED1", access_points=(ap_b,))
        a = _make_doc(ieds=(ied_a,))
        b = _make_doc(ieds=(ied_b,))
        result = apply(a, diff(a, b))
        insts = {ld.inst for ld in result.ieds[0].ldevices}
        assert insts == {"LD0", "LD1"}


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


class TestSummary:
    def test_empty_diff_summary(self) -> None:
        d = SclDiff()
        assert summary(d) == "(no changes)"

    def test_non_empty_diff_summary(self) -> None:
        a = _make_doc(ieds=(_make_ied("IED1"),))
        b = _make_doc(ieds=(_make_ied("IED1"), _make_ied("IED2")))
        d = diff(a, b)
        s = summary(d)
        assert "IED2" in s
        assert "ADDED" in s.upper()

    def test_by_section_filters_correctly(self) -> None:
        a = _make_doc(
            ieds=(_make_ied("IED1"),),
            subnetworks=(SubNetwork(name="MMS"),),
        )
        b = _make_doc(
            ieds=(_make_ied("IED1"), _make_ied("IED2")),
            subnetworks=(SubNetwork(name="MMS"), SubNetwork(name="GOOSE")),
        )
        d = diff(a, b)
        ied_changes = d.by_section("IED[")
        comm_changes = d.by_section("Communication.")
        assert any("IED2" in c.path for c in ied_changes)
        assert any("GOOSE" in c.path for c in comm_changes)
        # IED changes should not appear in Communication section
        assert not any("IED2" in c.path for c in comm_changes)


# ---------------------------------------------------------------------------
# Change dataclass
# ---------------------------------------------------------------------------


class TestChangeDataclass:
    def test_change_is_frozen(self) -> None:
        c = Change(path="IED[X]", kind="added", old_value=None, new_value="x")
        with pytest.raises(AttributeError):
            c.path = "other"  # type: ignore[misc]

    def test_scl_diff_bool_empty(self) -> None:
        assert not SclDiff()

    def test_scl_diff_bool_non_empty(self) -> None:
        d = SclDiff(changes=[Change(path="X", kind="added", old_value=None, new_value="v")])
        assert bool(d)
        assert len(d) == 1
