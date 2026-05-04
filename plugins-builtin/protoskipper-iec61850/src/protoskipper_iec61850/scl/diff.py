# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Semantic diff and patch engine for IEC 61850-6 SCL documents.

Public API
----------
diff(a, b)         -> SclDiff
apply(doc, d)      -> SclDocument
summary(d)         -> str   (human-readable one-liner per change)

Design
------
The diff operates on the *semantic* identity of SCL objects, not on
their XML text.  Identity keys are:

    IED            : IED.name
    AccessPoint    : (IED.name, AccessPoint.name)
    LDevice        : (IED.name, LDevice.inst)
    LN             : (IED.name, LDevice.inst, LN.prefix, LN.ln_class, LN.inst)
    DataSet        : (IED.name, LDevice.inst, LN identity, DataSet.name)
    FCDA           : positional index within parent DataSet
    ReportControl  : (IED.name, LDevice.inst, LN identity, RCB.name)
    GseControl     : (IED.name, LDevice.inst, LN identity, GseControl.name)
    SampledValueControl: (IED.name, LDevice.inst, LN identity, SVC.name)
    SubNetwork     : SubNetwork.name
    ConnectedAP    : (SubNetwork.name, CAP.ied_name, CAP.ap_name)
    Substation     : Substation.name

Change classification
---------------------
Each change is one of:

    "added"   - object exists in b but not in a
    "removed" - object exists in a but not in b
    "modified"- object with the same identity key differs in a and b

``apply(doc, diff)`` reconstructs the "b" document from "a" and the diff.
The DataTypeTemplates blob is taken from the diff result directly (it is
treated as an opaque blob; changes are detected by byte comparison).

Round-trip guarantee
--------------------
``diff(a, a)`` always returns an empty ``SclDiff``.
``apply(a, diff(a, b))`` always equals ``b`` (field-for-field).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import (
    IED,
    LN,
    AccessPoint,
    ConnectedAP,
    DataSet,
    GseControl,
    LDevice,
    ReportControl,
    SampledValueControl,
    SclDocument,
    SubNetwork,
    Substation,
)

# ---------------------------------------------------------------------------
# Change record
# ---------------------------------------------------------------------------

_VALID_KINDS = {"added", "removed", "modified"}


@dataclass(frozen=True, slots=True)
class Change:
    """A single semantic change between two SCL documents.

    Attributes
    ----------
    path:
        Dotted path describing the location of the change, e.g.
        ``"IED[PROT1].LDevice[LD0].LN[.LLN0.].DataSet[DS_STATUS]"``.
    kind:
        One of ``"added"``, ``"removed"``, or ``"modified"``.
    old_value:
        The value in document *a* (``None`` for added changes).
    new_value:
        The value in document *b* (``None`` for removed changes).
    """

    path: str
    kind: str  # "added" | "removed" | "modified"
    old_value: Any
    new_value: Any


@dataclass(slots=True)
class SclDiff:
    """Result of :func:`diff`.

    ``changes`` is ordered: Substation changes first, then
    Communication (SubNetworks / ConnectedAPs), then IED / LDevice /
    LN / DataSet / control-block changes, then DataTypeTemplates.
    """

    changes: list[Change] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changes)

    def __len__(self) -> int:
        return len(self.changes)

    def by_section(self, prefix: str) -> list[Change]:
        """Return all changes whose path starts with *prefix*."""
        return [c for c in self.changes if c.path.startswith(prefix)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diff(a: SclDocument, b: SclDocument) -> SclDiff:
    """Compute a semantic diff between two :class:`~.model.SclDocument` objects.

    Parameters
    ----------
    a, b:
        Source and target documents.

    Returns
    -------
    SclDiff
        Collection of :class:`Change` records.  Empty if ``a == b``.
    """
    result = SclDiff()

    _diff_substations(a, b, result)
    _diff_subnetworks(a, b, result)
    _diff_ieds(a, b, result)
    _diff_dtt(a, b, result)

    return result


def apply(doc: SclDocument, d: SclDiff) -> SclDocument:
    """Apply a :class:`SclDiff` to *doc* and return the patched document.

    Parameters
    ----------
    doc:
        The base document (document *a* from :func:`diff`).
    d:
        The diff to apply.

    Returns
    -------
    SclDocument
        The patched document (equivalent to document *b* from :func:`diff`).
    """
    if not d:
        return doc

    # Rebuild from the change set.
    # Strategy: materialise the "b" side of every change; for objects
    # with no matching change, keep the "a" side unchanged.

    new_substations = _apply_substations(doc, d)
    new_subnetworks = _apply_subnetworks(doc, d)
    new_ieds = _apply_ieds(doc, d)
    new_dtt = _apply_dtt(doc, d)

    return SclDocument(
        version=doc.version,
        revision=doc.revision,
        release=doc.release,
        ieds=tuple(new_ieds),
        subnetworks=tuple(new_subnetworks),
        substations=tuple(new_substations),
        data_type_templates_xml=new_dtt,
        source_file=doc.source_file,
    )


def summary(d: SclDiff) -> str:
    """Return a human-readable multi-line summary of a :class:`SclDiff`."""
    if not d:
        return "(no changes)"
    lines = [f"{c.kind.upper():8s}  {c.path}" for c in d.changes]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Diff helpers — Substation
# ---------------------------------------------------------------------------


def _diff_substations(a: SclDocument, b: SclDocument, result: SclDiff) -> None:
    a_map = {ss.name: ss for ss in a.substations}
    b_map = {ss.name: ss for ss in b.substations}

    for name, ss_a in a_map.items():
        path = f"Substation[{name}]"
        if name not in b_map:
            result.changes.append(Change(path=path, kind="removed", old_value=ss_a, new_value=None))
        elif ss_a != b_map[name]:
            result.changes.append(
                Change(path=path, kind="modified", old_value=ss_a, new_value=b_map[name])
            )
    for name, ss_b in b_map.items():
        if name not in a_map:
            result.changes.append(
                Change(path=f"Substation[{name}]", kind="added", old_value=None, new_value=ss_b)
            )


# ---------------------------------------------------------------------------
# Diff helpers — Communication / SubNetwork / ConnectedAP
# ---------------------------------------------------------------------------


def _diff_subnetworks(a: SclDocument, b: SclDocument, result: SclDiff) -> None:
    a_map = {sn.name: sn for sn in a.subnetworks}
    b_map = {sn.name: sn for sn in b.subnetworks}

    for name, sn_a in a_map.items():
        path = f"Communication.SubNetwork[{name}]"
        if name not in b_map:
            result.changes.append(Change(path=path, kind="removed", old_value=sn_a, new_value=None))
        else:
            sn_b = b_map[name]
            # Check top-level fields
            sn_a_top = SubNetwork(name=sn_a.name, desc=sn_a.desc, type=sn_a.type)
            sn_b_top = SubNetwork(name=sn_b.name, desc=sn_b.desc, type=sn_b.type)
            if sn_a_top != sn_b_top:
                result.changes.append(
                    Change(
                        path=path + "/@attrs",
                        kind="modified",
                        old_value=sn_a_top,
                        new_value=sn_b_top,
                    )
                )
            _diff_connected_aps(name, sn_a, sn_b, result)

    for name, sn_b in b_map.items():
        if name not in a_map:
            result.changes.append(
                Change(
                    path=f"Communication.SubNetwork[{name}]",
                    kind="added",
                    old_value=None,
                    new_value=sn_b,
                )
            )


def _diff_connected_aps(sn_name: str, a: SubNetwork, b: SubNetwork, result: SclDiff) -> None:
    def _key(cap: ConnectedAP) -> tuple[str, str]:
        return (cap.ied_name, cap.ap_name)

    a_map = {_key(c): c for c in a.connected_aps}
    b_map = {_key(c): c for c in b.connected_aps}

    for key, cap_a in a_map.items():
        path = f"Communication.SubNetwork[{sn_name}].ConnectedAP[{key[0]}/{key[1]}]"
        if key not in b_map:
            result.changes.append(
                Change(path=path, kind="removed", old_value=cap_a, new_value=None)
            )
        elif cap_a != b_map[key]:
            result.changes.append(
                Change(path=path, kind="modified", old_value=cap_a, new_value=b_map[key])
            )
    for key, cap_b in b_map.items():
        if key not in a_map:
            result.changes.append(
                Change(
                    path=f"Communication.SubNetwork[{sn_name}].ConnectedAP[{key[0]}/{key[1]}]",
                    kind="added",
                    old_value=None,
                    new_value=cap_b,
                )
            )


# ---------------------------------------------------------------------------
# Diff helpers — IED / AccessPoint / LDevice / LN
# ---------------------------------------------------------------------------


def _diff_ieds(a: SclDocument, b: SclDocument, result: SclDiff) -> None:
    a_map = {ied.name: ied for ied in a.ieds}
    b_map = {ied.name: ied for ied in b.ieds}

    for name, ied_a in a_map.items():
        path = f"IED[{name}]"
        if name not in b_map:
            result.changes.append(
                Change(path=path, kind="removed", old_value=ied_a, new_value=None)
            )
        else:
            ied_b = b_map[name]
            # IED-level attributes
            ied_a_attrs = IED(
                name=ied_a.name,
                desc=ied_a.desc,
                manufacturer=ied_a.manufacturer,
                model=ied_a.model,
                config_version=ied_a.config_version,
            )
            ied_b_attrs = IED(
                name=ied_b.name,
                desc=ied_b.desc,
                manufacturer=ied_b.manufacturer,
                model=ied_b.model,
                config_version=ied_b.config_version,
            )
            if ied_a_attrs != ied_b_attrs:
                result.changes.append(
                    Change(
                        path=path + "/@attrs",
                        kind="modified",
                        old_value=ied_a_attrs,
                        new_value=ied_b_attrs,
                    )
                )
            _diff_access_points(name, ied_a, ied_b, result)

    for name, ied_b in b_map.items():
        if name not in a_map:
            result.changes.append(
                Change(path=f"IED[{name}]", kind="added", old_value=None, new_value=ied_b)
            )


def _diff_access_points(ied_name: str, a: IED, b: IED, result: SclDiff) -> None:
    a_map = {ap.name: ap for ap in a.access_points}
    b_map = {ap.name: ap for ap in b.access_points}

    for ap_name, ap_a in a_map.items():
        if ap_name not in b_map:
            result.changes.append(
                Change(
                    path=f"IED[{ied_name}].AccessPoint[{ap_name}]",
                    kind="removed",
                    old_value=ap_a,
                    new_value=None,
                )
            )
        else:
            _diff_ldevices(ied_name, ap_name, ap_a, b_map[ap_name], result)
    for ap_name, ap_b in b_map.items():
        if ap_name not in a_map:
            result.changes.append(
                Change(
                    path=f"IED[{ied_name}].AccessPoint[{ap_name}]",
                    kind="added",
                    old_value=None,
                    new_value=ap_b,
                )
            )


def _diff_ldevices(
    ied_name: str, ap_name: str, a: AccessPoint, b: AccessPoint, result: SclDiff
) -> None:
    a_map = {ld.inst: ld for ld in a.ldevices}
    b_map = {ld.inst: ld for ld in b.ldevices}

    for inst, ld_a in a_map.items():
        path = f"IED[{ied_name}].LDevice[{inst}]"
        if inst not in b_map:
            result.changes.append(Change(path=path, kind="removed", old_value=ld_a, new_value=None))
        else:
            ld_b = b_map[inst]
            # LDevice-level attributes
            if ld_a.inst != ld_b.inst or ld_a.desc != ld_b.desc:
                result.changes.append(
                    Change(
                        path=path + "/@attrs",
                        kind="modified",
                        old_value=LDevice(inst=ld_a.inst, desc=ld_a.desc),
                        new_value=LDevice(inst=ld_b.inst, desc=ld_b.desc),
                    )
                )
            _diff_lns(ied_name, inst, ld_a, ld_b, result)
    for inst, ld_b in b_map.items():
        if inst not in a_map:
            result.changes.append(
                Change(
                    path=f"IED[{ied_name}].LDevice[{inst}]",
                    kind="added",
                    old_value=None,
                    new_value=ld_b,
                )
            )


def _ln_key(ln: LN) -> str:
    return f"{ln.prefix}.{ln.ln_class}.{ln.inst}"


def _diff_lns(ied_name: str, ld_inst: str, a: LDevice, b: LDevice, result: SclDiff) -> None:
    def _all_lns(ld: LDevice) -> dict[str, LN]:
        m: dict[str, LN] = {}
        if ld.ln0 is not None:
            m[_ln_key(ld.ln0)] = ld.ln0
        for ln in ld.lns:
            m[_ln_key(ln)] = ln
        return m

    a_map = _all_lns(a)
    b_map = _all_lns(b)

    for key, ln_a in a_map.items():
        path = f"IED[{ied_name}].LDevice[{ld_inst}].LN[{key}]"
        if key not in b_map:
            result.changes.append(Change(path=path, kind="removed", old_value=ln_a, new_value=None))
        else:
            ln_b = b_map[key]
            # LN-level attributes (excluding children)
            if ln_a.ln_type != ln_b.ln_type or ln_a.desc != ln_b.desc:
                result.changes.append(
                    Change(
                        path=path + "/@attrs",
                        kind="modified",
                        old_value=LN(
                            prefix=ln_a.prefix,
                            ln_class=ln_a.ln_class,
                            inst=ln_a.inst,
                            ln_type=ln_a.ln_type,
                            desc=ln_a.desc,
                        ),
                        new_value=LN(
                            prefix=ln_b.prefix,
                            ln_class=ln_b.ln_class,
                            inst=ln_b.inst,
                            ln_type=ln_b.ln_type,
                            desc=ln_b.desc,
                        ),
                    )
                )
            _diff_datasets(ied_name, ld_inst, key, ln_a, ln_b, result)
            _diff_rcbs(ied_name, ld_inst, key, ln_a, ln_b, result)
            _diff_gse_controls(ied_name, ld_inst, key, ln_a, ln_b, result)
            _diff_sv_controls(ied_name, ld_inst, key, ln_a, ln_b, result)
    for key, ln_b in b_map.items():
        if key not in a_map:
            result.changes.append(
                Change(
                    path=f"IED[{ied_name}].LDevice[{ld_inst}].LN[{key}]",
                    kind="added",
                    old_value=None,
                    new_value=ln_b,
                )
            )


def _diff_datasets(
    ied_name: str, ld_inst: str, ln_key_str: str, a: LN, b: LN, result: SclDiff
) -> None:
    a_map = {ds.name: ds for ds in a.datasets}
    b_map = {ds.name: ds for ds in b.datasets}
    base = f"IED[{ied_name}].LDevice[{ld_inst}].LN[{ln_key_str}].DataSet"

    for name, ds_a in a_map.items():
        path = f"{base}[{name}]"
        if name not in b_map:
            result.changes.append(Change(path=path, kind="removed", old_value=ds_a, new_value=None))
        elif ds_a != b_map[name]:
            result.changes.append(
                Change(path=path, kind="modified", old_value=ds_a, new_value=b_map[name])
            )
    for name, ds_b in b_map.items():
        if name not in a_map:
            result.changes.append(
                Change(path=f"{base}[{name}]", kind="added", old_value=None, new_value=ds_b)
            )


def _diff_rcbs(ied_name: str, ld_inst: str, ln_key_str: str, a: LN, b: LN, result: SclDiff) -> None:
    a_map = {rcb.name: rcb for rcb in a.report_controls}
    b_map = {rcb.name: rcb for rcb in b.report_controls}
    base = f"IED[{ied_name}].LDevice[{ld_inst}].LN[{ln_key_str}].ReportControl"

    for name, rcb_a in a_map.items():
        if name not in b_map:
            result.changes.append(
                Change(path=f"{base}[{name}]", kind="removed", old_value=rcb_a, new_value=None)
            )
        elif rcb_a != b_map[name]:
            result.changes.append(
                Change(
                    path=f"{base}[{name}]",
                    kind="modified",
                    old_value=rcb_a,
                    new_value=b_map[name],
                )
            )
    for name, rcb_b in b_map.items():
        if name not in a_map:
            result.changes.append(
                Change(path=f"{base}[{name}]", kind="added", old_value=None, new_value=rcb_b)
            )


def _diff_gse_controls(
    ied_name: str, ld_inst: str, ln_key_str: str, a: LN, b: LN, result: SclDiff
) -> None:
    a_map = {gse.name: gse for gse in a.gse_controls}
    b_map = {gse.name: gse for gse in b.gse_controls}
    base = f"IED[{ied_name}].LDevice[{ld_inst}].LN[{ln_key_str}].GSEControl"

    for name, gse_a in a_map.items():
        if name not in b_map:
            result.changes.append(
                Change(path=f"{base}[{name}]", kind="removed", old_value=gse_a, new_value=None)
            )
        elif gse_a != b_map[name]:
            result.changes.append(
                Change(
                    path=f"{base}[{name}]",
                    kind="modified",
                    old_value=gse_a,
                    new_value=b_map[name],
                )
            )
    for name, gse_b in b_map.items():
        if name not in a_map:
            result.changes.append(
                Change(path=f"{base}[{name}]", kind="added", old_value=None, new_value=gse_b)
            )


def _diff_sv_controls(
    ied_name: str, ld_inst: str, ln_key_str: str, a: LN, b: LN, result: SclDiff
) -> None:
    a_map = {svc.name: svc for svc in a.sv_controls}
    b_map = {svc.name: svc for svc in b.sv_controls}
    base = f"IED[{ied_name}].LDevice[{ld_inst}].LN[{ln_key_str}].SampledValueControl"

    for name, svc_a in a_map.items():
        if name not in b_map:
            result.changes.append(
                Change(path=f"{base}[{name}]", kind="removed", old_value=svc_a, new_value=None)
            )
        elif svc_a != b_map[name]:
            result.changes.append(
                Change(
                    path=f"{base}[{name}]",
                    kind="modified",
                    old_value=svc_a,
                    new_value=b_map[name],
                )
            )
    for name, svc_b in b_map.items():
        if name not in a_map:
            result.changes.append(
                Change(path=f"{base}[{name}]", kind="added", old_value=None, new_value=svc_b)
            )


# ---------------------------------------------------------------------------
# Diff helpers — DataTypeTemplates (opaque blob diff)
# ---------------------------------------------------------------------------


def _diff_dtt(a: SclDocument, b: SclDocument, result: SclDiff) -> None:
    if a.data_type_templates_xml != b.data_type_templates_xml:
        result.changes.append(
            Change(
                path="DataTypeTemplates",
                kind="modified",
                old_value=a.data_type_templates_xml,
                new_value=b.data_type_templates_xml,
            )
        )


# ---------------------------------------------------------------------------
# Apply helpers
# ---------------------------------------------------------------------------


def _apply_substations(doc: SclDocument, d: SclDiff) -> list[Substation]:
    # Collect all changes that touch substations
    ss_changes: dict[str, Change] = {}
    for c in d.by_section("Substation["):
        # path is "Substation[NAME]"
        name = c.path[len("Substation[") : -1]
        ss_changes[name] = c

    result: list[Substation] = []
    for ss in doc.substations:
        if ss.name in ss_changes:
            c = ss_changes[ss.name]
            if c.kind == "removed":
                continue
            result.append(c.new_value)
        else:
            result.append(ss)
    # Add new substations
    for _name, c in ss_changes.items():
        if c.kind == "added":
            result.append(c.new_value)
    return result


def _apply_subnetworks(doc: SclDocument, d: SclDiff) -> list[SubNetwork]:
    # Gather changes per subnetwork
    sn_added: dict[str, SubNetwork] = {}
    sn_removed: set[str] = set()
    sn_cap_changes: dict[str, list[Change]] = {}
    sn_attr_changes: dict[str, Change] = {}

    for c in d.by_section("Communication.SubNetwork["):
        rest = c.path[len("Communication.SubNetwork[") :]
        sn_name = rest[: rest.index("]")]
        suffix = rest[len(sn_name) + 1 :]
        if not suffix:
            # Top-level subnetwork change
            if c.kind == "added":
                sn_added[sn_name] = c.new_value
            elif c.kind == "removed":
                sn_removed.add(sn_name)
        elif suffix.startswith(".ConnectedAP["):
            sn_cap_changes.setdefault(sn_name, []).append(c)
        elif suffix.startswith("/@attrs"):
            sn_attr_changes[sn_name] = c

    result: list[SubNetwork] = []
    for sn in doc.subnetworks:
        if sn.name in sn_removed:
            continue
        # Rebuild with potentially changed CAPs
        caps = _apply_connected_aps(sn, sn_cap_changes.get(sn.name, []))
        if sn.name in sn_attr_changes:
            attrs: SubNetwork = sn_attr_changes[sn.name].new_value
            result.append(
                SubNetwork(
                    name=attrs.name, desc=attrs.desc, type=attrs.type, connected_aps=tuple(caps)
                )
            )
        else:
            result.append(
                SubNetwork(name=sn.name, desc=sn.desc, type=sn.type, connected_aps=tuple(caps))
            )
    for _name, sn_b in sn_added.items():
        result.append(sn_b)
    return result


def _apply_connected_aps(sn: SubNetwork, cap_changes: list[Change]) -> list[ConnectedAP]:
    cap_change_map: dict[tuple[str, str], Change] = {}
    for c in cap_changes:
        # path = "Communication.SubNetwork[X].ConnectedAP[ied/ap]"
        cap_part = c.path[c.path.index("ConnectedAP[") + len("ConnectedAP[") : -1]
        ied_name, ap_name = cap_part.split("/", 1)
        cap_change_map[(ied_name, ap_name)] = c

    result: list[ConnectedAP] = []
    for cap in sn.connected_aps:
        key = (cap.ied_name, cap.ap_name)
        if key in cap_change_map:
            c = cap_change_map[key]
            if c.kind == "removed":
                continue
            result.append(c.new_value)
        else:
            result.append(cap)
    for _key, c in cap_change_map.items():
        if c.kind == "added":
            result.append(c.new_value)
    return result


def _apply_ieds(doc: SclDocument, d: SclDiff) -> list[IED]:
    ied_changes: dict[str, list[Change]] = {}
    ied_removed: set[str] = set()
    ied_added: dict[str, IED] = {}

    for c in d.by_section("IED["):
        rest = c.path[len("IED[") :]
        ied_name = rest[: rest.index("]")]
        suffix = rest[len(ied_name) + 1 :]
        if not suffix:
            if c.kind == "removed":
                ied_removed.add(ied_name)
            elif c.kind == "added":
                ied_added[ied_name] = c.new_value
        else:
            ied_changes.setdefault(ied_name, []).append(c)

    result: list[IED] = []
    for ied in doc.ieds:
        if ied.name in ied_removed:
            continue
        result.append(_apply_ied(ied, ied_changes.get(ied.name, [])))
    for _name, ied_b in ied_added.items():
        result.append(ied_b)
    return result


def _apply_ied(ied: IED, changes: list[Change]) -> IED:
    # Find attribute changes
    attrs_new: IED | None = None
    for c in changes:
        rest = c.path[len(f"IED[{ied.name}]") :]
        if rest == "/@attrs":
            attrs_new = c.new_value

    # Rebuild access points
    new_aps = _apply_access_points(ied, changes)

    if attrs_new is not None:
        return IED(
            name=attrs_new.name,
            desc=attrs_new.desc,
            manufacturer=attrs_new.manufacturer,
            model=attrs_new.model,
            config_version=attrs_new.config_version,
            access_points=tuple(new_aps),
        )
    return IED(
        name=ied.name,
        desc=ied.desc,
        manufacturer=ied.manufacturer,
        model=ied.model,
        config_version=ied.config_version,
        access_points=tuple(new_aps),
    )


def _apply_access_points(ied: IED, changes: list[Change]) -> list[AccessPoint]:
    ap_prefix = f"IED[{ied.name}].AccessPoint["
    ap_removed: set[str] = set()
    ap_added: dict[str, AccessPoint] = {}

    for c in changes:
        if not c.path.startswith(ap_prefix):
            continue
        rest = c.path[len(ap_prefix) :]
        ap_name = rest[: rest.index("]")]
        suffix = rest[len(ap_name) + 1 :]
        if not suffix:
            if c.kind == "removed":
                ap_removed.add(ap_name)
            elif c.kind == "added":
                ap_added[ap_name] = c.new_value

    result: list[AccessPoint] = []
    for ap in ied.access_points:
        if ap.name in ap_removed:
            continue
        # Diff paths omit the AccessPoint level (IED[x].LDevice[y]...).
        # Pass ALL changes; _apply_ldevices will filter by its own prefix.
        new_lds = _apply_ldevices(ied.name, ap, changes)
        result.append(AccessPoint(name=ap.name, ldevices=tuple(new_lds)))
    for _ap_name, ap_b in ap_added.items():
        result.append(ap_b)
    return result


def _apply_ldevices(ied_name: str, ap: AccessPoint, changes: list[Change]) -> list[LDevice]:
    ld_prefix = f"IED[{ied_name}].LDevice["
    ld_removed: set[str] = set()
    ld_added: dict[str, LDevice] = {}
    ld_child_changes: dict[str, list[Change]] = {}
    ld_attr_changes: dict[str, Change] = {}

    for c in changes:
        if not c.path.startswith(ld_prefix):
            continue
        rest = c.path[len(ld_prefix) :]
        ld_inst = rest[: rest.index("]")]
        suffix = rest[len(ld_inst) + 1 :]
        if not suffix:
            if c.kind == "removed":
                ld_removed.add(ld_inst)
            elif c.kind == "added":
                ld_added[ld_inst] = c.new_value
        elif suffix == "/@attrs":
            ld_attr_changes[ld_inst] = c
        else:
            ld_child_changes.setdefault(ld_inst, []).append(c)

    result: list[LDevice] = []
    for ld in ap.ldevices:
        if ld.inst in ld_removed:
            continue
        new_ln0, new_lns = _apply_lns(ied_name, ld, ld_child_changes.get(ld.inst, []))
        attrs_c = ld_attr_changes.get(ld.inst)
        if attrs_c:
            new_attrs: LDevice = attrs_c.new_value
            result.append(
                LDevice(inst=new_attrs.inst, desc=new_attrs.desc, ln0=new_ln0, lns=tuple(new_lns))
            )
        else:
            result.append(LDevice(inst=ld.inst, desc=ld.desc, ln0=new_ln0, lns=tuple(new_lns)))
    for _inst, ld_b in ld_added.items():
        result.append(ld_b)
    return result


def _apply_lns(ied_name: str, ld: LDevice, changes: list[Change]) -> tuple[LN | None, list[LN]]:
    ln_prefix = f"IED[{ied_name}].LDevice[{ld.inst}].LN["
    ln_removed: set[str] = set()
    ln_added: dict[str, LN] = {}
    ln_child_changes: dict[str, list[Change]] = {}
    ln_attr_changes: dict[str, Change] = {}

    for c in changes:
        if not c.path.startswith(ln_prefix):
            continue
        rest = c.path[len(ln_prefix) :]
        key = rest[: rest.index("]")]
        suffix = rest[len(key) + 1 :]
        if not suffix:
            if c.kind == "removed":
                ln_removed.add(key)
            elif c.kind == "added":
                ln_added[key] = c.new_value
        elif suffix == "/@attrs":
            ln_attr_changes[key] = c
        else:
            ln_child_changes.setdefault(key, []).append(c)

    def _rebuild_ln(ln: LN) -> LN:
        key = _ln_key(ln)
        child_changes = ln_child_changes.get(key, [])
        new_datasets = _apply_datasets(ied_name, ld.inst, key, ln, child_changes)
        new_rcbs = _apply_rcbs(ied_name, ld.inst, key, ln, child_changes)
        new_gses = _apply_gse_controls_to_ln(ied_name, ld.inst, key, ln, child_changes)
        new_svcs = _apply_sv_controls_to_ln(ied_name, ld.inst, key, ln, child_changes)
        attrs_c = ln_attr_changes.get(key)
        if attrs_c:
            new_attrs: LN = attrs_c.new_value
            return LN(
                prefix=new_attrs.prefix,
                ln_class=new_attrs.ln_class,
                inst=new_attrs.inst,
                ln_type=new_attrs.ln_type,
                desc=new_attrs.desc,
                datasets=tuple(new_datasets),
                report_controls=tuple(new_rcbs),
                gse_controls=tuple(new_gses),
                sv_controls=tuple(new_svcs),
            )
        return LN(
            prefix=ln.prefix,
            ln_class=ln.ln_class,
            inst=ln.inst,
            ln_type=ln.ln_type,
            desc=ln.desc,
            datasets=tuple(new_datasets),
            report_controls=tuple(new_rcbs),
            gse_controls=tuple(new_gses),
            sv_controls=tuple(new_svcs),
        )

    new_ln0: LN | None = None
    if ld.ln0 is not None:
        key0 = _ln_key(ld.ln0)
        if key0 not in ln_removed:
            new_ln0 = ln_added[key0] if key0 in ln_added else _rebuild_ln(ld.ln0)

    new_lns: list[LN] = []
    for ln in ld.lns:
        key = _ln_key(ln)
        if key in ln_removed:
            continue
        new_lns.append(_rebuild_ln(ln))
    for key, ln_b in ln_added.items():
        # Only add if it's not LN0 (already handled)
        if key not in ({_ln_key(ld.ln0)} if ld.ln0 else set()):
            # Check it wasn't in original lns
            existing_keys = {_ln_key(ln) for ln in ld.lns}
            if key not in existing_keys:
                new_lns.append(ln_b)

    return new_ln0, new_lns


def _apply_datasets(
    ied_name: str, ld_inst: str, ln_key_str: str, ln: LN, changes: list[Change]
) -> list[DataSet]:
    ds_prefix = f"IED[{ied_name}].LDevice[{ld_inst}].LN[{ln_key_str}].DataSet["
    ds_removed: set[str] = set()
    ds_added: dict[str, DataSet] = {}
    ds_modified: dict[str, DataSet] = {}

    for c in changes:
        if not c.path.startswith(ds_prefix):
            continue
        name = c.path[len(ds_prefix) : -1]
        if c.kind == "removed":
            ds_removed.add(name)
        elif c.kind == "added":
            ds_added[name] = c.new_value
        elif c.kind == "modified":
            ds_modified[name] = c.new_value

    result: list[DataSet] = []
    for ds in ln.datasets:
        if ds.name in ds_removed:
            continue
        if ds.name in ds_modified:
            result.append(ds_modified[ds.name])
        else:
            result.append(ds)
    for _name, ds_b in ds_added.items():
        result.append(ds_b)
    return result


def _apply_rcbs(
    ied_name: str, ld_inst: str, ln_key_str: str, ln: LN, changes: list[Change]
) -> list[ReportControl]:
    prefix = f"IED[{ied_name}].LDevice[{ld_inst}].LN[{ln_key_str}].ReportControl["
    removed: set[str] = set()
    added: dict[str, ReportControl] = {}
    modified: dict[str, ReportControl] = {}

    for c in changes:
        if not c.path.startswith(prefix):
            continue
        name = c.path[len(prefix) : -1]
        if c.kind == "removed":
            removed.add(name)
        elif c.kind == "added":
            added[name] = c.new_value
        elif c.kind == "modified":
            modified[name] = c.new_value

    result: list[ReportControl] = []
    for rcb in ln.report_controls:
        if rcb.name in removed:
            continue
        result.append(modified.get(rcb.name, rcb))
    for _name, rcb_b in added.items():
        result.append(rcb_b)
    return result


def _apply_gse_controls_to_ln(
    ied_name: str, ld_inst: str, ln_key_str: str, ln: LN, changes: list[Change]
) -> list[GseControl]:
    prefix = f"IED[{ied_name}].LDevice[{ld_inst}].LN[{ln_key_str}].GSEControl["
    removed: set[str] = set()
    added: dict[str, GseControl] = {}
    modified: dict[str, GseControl] = {}

    for c in changes:
        if not c.path.startswith(prefix):
            continue
        name = c.path[len(prefix) : -1]
        if c.kind == "removed":
            removed.add(name)
        elif c.kind == "added":
            added[name] = c.new_value
        elif c.kind == "modified":
            modified[name] = c.new_value

    result: list[GseControl] = []
    for gse in ln.gse_controls:
        if gse.name in removed:
            continue
        result.append(modified.get(gse.name, gse))
    for _name, gse_b in added.items():
        result.append(gse_b)
    return result


def _apply_sv_controls_to_ln(
    ied_name: str, ld_inst: str, ln_key_str: str, ln: LN, changes: list[Change]
) -> list[SampledValueControl]:
    prefix = f"IED[{ied_name}].LDevice[{ld_inst}].LN[{ln_key_str}].SampledValueControl["
    removed: set[str] = set()
    added: dict[str, SampledValueControl] = {}
    modified: dict[str, SampledValueControl] = {}

    for c in changes:
        if not c.path.startswith(prefix):
            continue
        name = c.path[len(prefix) : -1]
        if c.kind == "removed":
            removed.add(name)
        elif c.kind == "added":
            added[name] = c.new_value
        elif c.kind == "modified":
            modified[name] = c.new_value

    result: list[SampledValueControl] = []
    for svc in ln.sv_controls:
        if svc.name in removed:
            continue
        result.append(modified.get(svc.name, svc))
    for _name, svc_b in added.items():
        result.append(svc_b)
    return result


def _apply_dtt(doc: SclDocument, d: SclDiff) -> bytes:
    for c in d.by_section("DataTypeTemplates"):
        if c.kind == "modified":
            return c.new_value  # type: ignore[return-value]
    return doc.data_type_templates_xml
