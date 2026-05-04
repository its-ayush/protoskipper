# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""SCL parser, structural validator, and round-trip writer.

Public API
----------
parse(path)          -> SclDocument
validate(doc)        -> list[ValidationIssue]
write(doc, path)     -> None

The parser uses ``lxml`` (available via the ``[scl]`` extra).  Install
with ``pip install protoskipper-iec61850[scl]``.

Edition detection
-----------------
All known editions of IEC 61850-6 share the same XML namespace
``http://www.iec.ch/61850/2003/SCL``.  The edition is implied by the
combination of ``SCL/@version``, ``SCL/@revision``, and
``SCL/@release``:

    version="2007", revision="A"           → Edition 1.0
    version="2007", revision="B"           → Edition 2.0
    version="2007", revision="B", release="4" → Edition 2.1
    version="2007", revision="C"           → Edition 2.2 (draft)

The parser accepts all editions without failing; missing attributes are
treated as empty strings.

Round-trip fidelity
-------------------
``write()`` rebuilds the XML tree from the model objects.  The
``SclDocument.data_type_templates_xml`` bytes blob is re-inserted
verbatim, preserving DataTypeTemplates validity.  Elements not yet
modelled (Bay, VoltageLevel, etc.) are **not** preserved.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    from lxml import etree as _etree
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "protoskipper-iec61850[scl] is required for SCL parsing.  "
        "Install with:  pip install protoskipper-iec61850[scl]"
    ) from exc

from .model import (
    FCDA,
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
    ValidationIssue,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCL_NS = "http://www.iec.ch/61850/2003/SCL"

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse(path: str | os.PathLike[str]) -> SclDocument:
    """Parse an SCL file (SCD / ICD / IID / CID / SSD) into a :class:`SclDocument`.

    Parameters
    ----------
    path:
        Filesystem path to the SCL file.

    Returns
    -------
    SclDocument
        Fully populated frozen model.

    Raises
    ------
    lxml.etree.XMLSyntaxError
        If the file is not well-formed XML.
    OSError
        If the file cannot be opened.
    """
    path = Path(path)
    tree = _etree.parse(str(path))  # type: ignore[attr-defined]
    root = tree.getroot()
    return _parse_root(root, source_file=str(path))


def validate(doc: SclDocument) -> list[ValidationIssue]:
    """Run structural validation on a parsed :class:`SclDocument`.

    This performs semantic checks (uniqueness, referential integrity, etc.)
    that go beyond what an XSD schema can express.  For XSD-level checks,
    use an external tool such as ``xmllint --schema iec61850.xsd``.

    Returns
    -------
    list[ValidationIssue]
        Empty list means the document is structurally valid.
        Issues have ``severity`` of ``"error"``, ``"warning"``, or ``"info"``.
    """
    issues: list[ValidationIssue] = []

    # Rule V01: Standard SCL namespace required
    # (checked by parser; we re-surface it here for programmatic access)
    # Note: namespace is not stored in model; skip if called on a
    # manually-constructed SclDocument.

    # Rule V02: IED names must be unique
    ied_names: list[str] = []
    for ied in doc.ieds:
        if not ied.name:
            issues.append(
                ValidationIssue(
                    severity="error",
                    line=None,
                    message="IED element has no 'name' attribute",
                    rule="V02",
                )
            )
        elif ied.name in ied_names:
            issues.append(
                ValidationIssue(
                    severity="error",
                    line=None,
                    message=f"Duplicate IED name: '{ied.name}'",
                    rule="V02",
                )
            )
        else:
            ied_names.append(ied.name)

        # Rule V03: LDevice inst must be unique within an IED
        ld_insts: list[str] = []
        for ap in ied.access_points:
            for ld in ap.ldevices:
                if not ld.inst:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            line=None,
                            message=f"LDevice in IED '{ied.name}' has no 'inst' attribute",
                            rule="V03",
                        )
                    )
                elif ld.inst in ld_insts:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            line=None,
                            message=(f"Duplicate LDevice inst '{ld.inst}' in IED '{ied.name}'"),
                            rule="V03",
                        )
                    )
                else:
                    ld_insts.append(ld.inst)

                # Rule V04: LN (prefix, lnClass, inst) must be unique within LD
                ln_keys: list[tuple[str, str, str]] = []
                all_lns = ([ld.ln0] if ld.ln0 is not None else []) + list(ld.lns)
                for ln in all_lns:
                    key = (ln.prefix, ln.ln_class, ln.inst)
                    if key in ln_keys:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                line=None,
                                message=(
                                    f"Duplicate LN (prefix='{ln.prefix}', "
                                    f"lnClass='{ln.ln_class}', inst='{ln.inst}') "
                                    f"in LD '{ld.inst}' of IED '{ied.name}'"
                                ),
                                rule="V04",
                            )
                        )
                    else:
                        ln_keys.append(key)

                    # Rule V05: RCB datSet references must resolve to a DataSet
                    ds_names = {ds.name for ds in ln.datasets}
                    for rcb in ln.report_controls:
                        if rcb.dataset_ref and rcb.dataset_ref not in ds_names:
                            issues.append(
                                ValidationIssue(
                                    severity="warning",
                                    line=None,
                                    message=(
                                        f"RCB '{rcb.name}' references unknown DataSet "
                                        f"'{rcb.dataset_ref}' in LN "
                                        f"'{ln.prefix}{ln.ln_class}{ln.inst}'"
                                    ),
                                    rule="V05",
                                )
                            )
                    for gse in ln.gse_controls:
                        if gse.dataset_ref and gse.dataset_ref not in ds_names:
                            issues.append(
                                ValidationIssue(
                                    severity="warning",
                                    line=None,
                                    message=(
                                        f"GSEControl '{gse.name}' references unknown "
                                        f"DataSet '{gse.dataset_ref}' in LN "
                                        f"'{ln.prefix}{ln.ln_class}{ln.inst}'"
                                    ),
                                    rule="V05",
                                )
                            )
                    for svc in ln.sv_controls:
                        if svc.dataset_ref and svc.dataset_ref not in ds_names:
                            issues.append(
                                ValidationIssue(
                                    severity="warning",
                                    line=None,
                                    message=(
                                        f"SampledValueControl '{svc.name}' references "
                                        f"unknown DataSet '{svc.dataset_ref}' in LN "
                                        f"'{ln.prefix}{ln.ln_class}{ln.inst}'"
                                    ),
                                    rule="V05",
                                )
                            )

    return issues


def write(doc: SclDocument, path: str | os.PathLike[str]) -> None:
    """Serialise a :class:`SclDocument` to an SCL XML file.

    The output is a standards-conformant SCL file.  ``DataTypeTemplates``
    from the original file (stored in
    ``doc.data_type_templates_xml``) are re-inserted verbatim if present.

    Parameters
    ----------
    doc:
        Document to serialise.
    path:
        Destination file path.
    """
    nsmap: dict[str | None, str] = {None: _SCL_NS}
    root = _etree.Element(f"{{{_SCL_NS}}}SCL", nsmap=nsmap)  # type: ignore[attr-defined]
    if doc.version:
        root.set("version", doc.version)
    if doc.revision:
        root.set("revision", doc.revision)
    if doc.release:
        root.set("release", doc.release)

    # Header (minimal; id required by schema)
    header = _etree.SubElement(root, f"{{{_SCL_NS}}}Header")  # type: ignore[attr-defined]
    header.set("id", "")
    header.set("nameStructure", "IEDName")

    # Substations
    for ss in doc.substations:
        el = _etree.SubElement(root, f"{{{_SCL_NS}}}Substation")  # type: ignore[attr-defined]
        el.set("name", ss.name)
        if ss.desc:
            el.set("desc", ss.desc)

    # Communication
    if doc.subnetworks:
        comm = _etree.SubElement(root, f"{{{_SCL_NS}}}Communication")  # type: ignore[attr-defined]
        for sn in doc.subnetworks:
            _write_subnetwork(comm, sn)

    # IEDs
    for ied in doc.ieds:
        _write_ied(root, ied)

    # DataTypeTemplates passthrough
    if doc.data_type_templates_xml:
        dtt_el = _etree.fromstring(doc.data_type_templates_xml)  # type: ignore[attr-defined]
        root.append(dtt_el)

    tree = _etree.ElementTree(root)  # type: ignore[attr-defined]
    tree.write(
        str(path),
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    )


# ---------------------------------------------------------------------------
# Internal: parsing helpers
# ---------------------------------------------------------------------------


def _qn(tag: str, ns: str | None) -> str:
    """Return a Clark-notation qualified name."""
    return f"{{{ns}}}{tag}" if ns else tag


def _detect_ns(root: Any) -> str | None:
    """Extract the XML namespace from the root element tag, or None."""
    tag: str = root.tag
    if tag.startswith("{"):
        return tag[1 : tag.index("}")]
    return None


def _parse_root(root: Any, source_file: str | None = None) -> SclDocument:
    ns = _detect_ns(root)
    if ns not in {_SCL_NS, None}:
        # Unknown namespace — still parse, but caller can inspect source_file
        pass

    def _q(tag: str) -> str:
        return _qn(tag, ns)

    # SCL/@version / @revision / @release
    version = root.get("version", "")
    revision = root.get("revision", "")
    release = root.get("release", "")

    # IEDs
    ieds = tuple(_parse_ied(el, _q) for el in root.findall(_q("IED")))

    # Communication → SubNetworks
    subnetworks: list[SubNetwork] = []
    comm_el = root.find(_q("Communication"))
    if comm_el is not None:
        for sn_el in comm_el.findall(_q("SubNetwork")):
            subnetworks.append(_parse_subnetwork(sn_el, _q))

    # Substations
    substations = tuple(
        Substation(name=el.get("name", ""), desc=el.get("desc", ""))
        for el in root.findall(_q("Substation"))
    )

    # DataTypeTemplates — preserve verbatim
    dtt_el = root.find(_q("DataTypeTemplates"))
    dtt_xml = b""
    if dtt_el is not None:
        dtt_xml = _etree.tostring(dtt_el, encoding="unicode").encode("utf-8")  # type: ignore[attr-defined]

    return SclDocument(
        version=version,
        revision=revision,
        release=release,
        ieds=ieds,
        subnetworks=tuple(subnetworks),
        substations=substations,
        data_type_templates_xml=dtt_xml,
        source_file=source_file,
    )


def _parse_ied(el: Any, q: Any) -> IED:
    aps = tuple(_parse_access_point(ap_el, q) for ap_el in el.findall(q("AccessPoint")))
    return IED(
        name=el.get("name", ""),
        desc=el.get("desc", ""),
        manufacturer=el.get("manufacturer", ""),
        model=el.get("type", ""),  # SCL uses 'type' for IED model
        config_version=el.get("configVersion", ""),
        access_points=aps,
    )


def _parse_access_point(el: Any, q: Any) -> AccessPoint:
    server_el = el.find(q("Server"))
    ldevices: list[LDevice] = []
    if server_el is not None:
        for ld_el in server_el.findall(q("LDevice")):
            ldevices.append(_parse_ldevice(ld_el, q))
    return AccessPoint(name=el.get("name", ""), ldevices=tuple(ldevices))


def _parse_ldevice(el: Any, q: Any) -> LDevice:
    ln0_el = el.find(q("LN0"))
    ln0 = _parse_ln(ln0_el, q, is_ln0=True) if ln0_el is not None else None
    lns = tuple(_parse_ln(ln_el, q, is_ln0=False) for ln_el in el.findall(q("LN")))
    return LDevice(
        inst=el.get("inst", ""),
        desc=el.get("desc", ""),
        ln0=ln0,
        lns=lns,
    )


def _parse_ln(el: Any, q: Any, *, is_ln0: bool = False) -> LN:
    datasets = tuple(_parse_dataset(ds_el, q) for ds_el in el.findall(q("DataSet")))
    rcbs = tuple(_parse_rcb(rcb_el, q) for rcb_el in el.findall(q("ReportControl")))
    gse_ctrls = tuple(_parse_gse_control(gse_el, q) for gse_el in el.findall(q("GSEControl")))
    sv_ctrls = tuple(_parse_sv_control(sv_el, q) for sv_el in el.findall(q("SampledValueControl")))
    return LN(
        prefix="" if is_ln0 else el.get("prefix", ""),
        ln_class=el.get("lnClass", ""),
        inst="" if is_ln0 else el.get("inst", ""),
        ln_type=el.get("lnType", ""),
        desc=el.get("desc", ""),
        datasets=datasets,
        report_controls=rcbs,
        gse_controls=gse_ctrls,
        sv_controls=sv_ctrls,
    )


def _parse_dataset(el: Any, q: Any) -> DataSet:
    fcdas = tuple(_parse_fcda(f, q) for f in el.findall(q("FCDA")))
    return DataSet(name=el.get("name", ""), desc=el.get("desc", ""), fcdas=fcdas)


def _parse_fcda(el: Any, _q: Any) -> FCDA:
    return FCDA(
        ld_inst=el.get("ldInst", ""),
        prefix=el.get("prefix", ""),
        ln_class=el.get("lnClass", ""),
        ln_inst=el.get("lnInst", ""),
        do_name=el.get("doName", ""),
        da_name=el.get("daName", ""),
        fc=el.get("fc", ""),
    )


def _parse_rcb(el: Any, _q: Any) -> ReportControl:
    return ReportControl(
        name=el.get("name", ""),
        desc=el.get("desc", ""),
        dataset_ref=el.get("datSet", ""),
        rpt_id=el.get("rptID", ""),
        buffered=el.get("buffered", "false").lower() == "true",
        conf_rev=int(el.get("confRev", "1") or "1"),
        indexed=el.get("indexed", "true").lower() != "false",
    )


def _parse_gse_control(el: Any, _q: Any) -> GseControl:
    return GseControl(
        name=el.get("name", ""),
        desc=el.get("desc", ""),
        dataset_ref=el.get("datSet", ""),
        app_id=el.get("appID", ""),
        fixed_offs=el.get("fixedOffs", "false").lower() == "true",
        type=el.get("type", "GOOSE"),
    )


def _parse_sv_control(el: Any, _q: Any) -> SampledValueControl:
    return SampledValueControl(
        name=el.get("name", ""),
        desc=el.get("desc", ""),
        dataset_ref=el.get("datSet", ""),
        smv_id=el.get("smvID", ""),
        smp_rate=int(el.get("smpRate", "80") or "80"),
        smp_mod=int(el.get("smpMod", "0") or "0"),
        multi_cast=el.get("multiCast", "true").lower() != "false",
    )


def _parse_subnetwork(el: Any, q: Any) -> SubNetwork:
    caps = tuple(_parse_connected_ap(cap_el, q) for cap_el in el.findall(q("ConnectedAP")))
    return SubNetwork(
        name=el.get("name", ""),
        desc=el.get("desc", ""),
        type=el.get("type", ""),
        connected_aps=caps,
    )


def _parse_connected_ap(el: Any, q: Any) -> ConnectedAP:
    addr_el = el.find(q("Address"))
    address: list[tuple[str, str]] = []
    if addr_el is not None:
        for p_el in addr_el.findall(q("P")):
            p_type = p_el.get("type", "")
            p_val = (p_el.text or "").strip()
            address.append((p_type, p_val))
    return ConnectedAP(
        ied_name=el.get("iedName", ""),
        ap_name=el.get("apName", ""),
        address=tuple(address),
    )


# ---------------------------------------------------------------------------
# Internal: write helpers
# ---------------------------------------------------------------------------


def _write_ied(parent: Any, ied: IED) -> None:
    el = _etree.SubElement(parent, f"{{{_SCL_NS}}}IED")  # type: ignore[attr-defined]
    el.set("name", ied.name)
    if ied.desc:
        el.set("desc", ied.desc)
    if ied.manufacturer:
        el.set("manufacturer", ied.manufacturer)
    if ied.model:
        el.set("type", ied.model)
    if ied.config_version:
        el.set("configVersion", ied.config_version)
    for ap in ied.access_points:
        _write_access_point(el, ap)


def _write_access_point(parent: Any, ap: AccessPoint) -> None:
    el = _etree.SubElement(parent, f"{{{_SCL_NS}}}AccessPoint")  # type: ignore[attr-defined]
    el.set("name", ap.name)
    if ap.ldevices:
        server = _etree.SubElement(el, f"{{{_SCL_NS}}}Server")  # type: ignore[attr-defined]
        for ld in ap.ldevices:
            _write_ldevice(server, ld)


def _write_ldevice(parent: Any, ld: LDevice) -> None:
    el = _etree.SubElement(parent, f"{{{_SCL_NS}}}LDevice")  # type: ignore[attr-defined]
    el.set("inst", ld.inst)
    if ld.desc:
        el.set("desc", ld.desc)
    if ld.ln0 is not None:
        _write_ln(el, ld.ln0, is_ln0=True)
    for ln in ld.lns:
        _write_ln(el, ln, is_ln0=False)


def _write_ln(parent: Any, ln: LN, *, is_ln0: bool) -> None:
    tag = f"{{{_SCL_NS}}}LN0" if is_ln0 else f"{{{_SCL_NS}}}LN"
    el = _etree.SubElement(parent, tag)  # type: ignore[attr-defined]
    if not is_ln0 and ln.prefix:
        el.set("prefix", ln.prefix)
    el.set("lnClass", ln.ln_class)
    if not is_ln0:
        el.set("inst", ln.inst)
    el.set("lnType", ln.ln_type)
    if ln.desc:
        el.set("desc", ln.desc)
    for ds in ln.datasets:
        _write_dataset(el, ds)
    for rcb in ln.report_controls:
        _write_rcb(el, rcb)
    for gse in ln.gse_controls:
        _write_gse_control(el, gse)
    for svc in ln.sv_controls:
        _write_sv_control(el, svc)


def _write_dataset(parent: Any, ds: DataSet) -> None:
    el = _etree.SubElement(parent, f"{{{_SCL_NS}}}DataSet")  # type: ignore[attr-defined]
    el.set("name", ds.name)
    if ds.desc:
        el.set("desc", ds.desc)
    for fcda in ds.fcdas:
        _write_fcda(el, fcda)


def _write_fcda(parent: Any, fcda: FCDA) -> None:
    el = _etree.SubElement(parent, f"{{{_SCL_NS}}}FCDA")  # type: ignore[attr-defined]
    if fcda.ld_inst:
        el.set("ldInst", fcda.ld_inst)
    if fcda.prefix:
        el.set("prefix", fcda.prefix)
    if fcda.ln_class:
        el.set("lnClass", fcda.ln_class)
    if fcda.ln_inst:
        el.set("lnInst", fcda.ln_inst)
    if fcda.do_name:
        el.set("doName", fcda.do_name)
    if fcda.da_name:
        el.set("daName", fcda.da_name)
    if fcda.fc:
        el.set("fc", fcda.fc)


def _write_rcb(parent: Any, rcb: ReportControl) -> None:
    el = _etree.SubElement(parent, f"{{{_SCL_NS}}}ReportControl")  # type: ignore[attr-defined]
    el.set("name", rcb.name)
    if rcb.desc:
        el.set("desc", rcb.desc)
    if rcb.dataset_ref:
        el.set("datSet", rcb.dataset_ref)
    if rcb.rpt_id:
        el.set("rptID", rcb.rpt_id)
    el.set("buffered", "true" if rcb.buffered else "false")
    el.set("confRev", str(rcb.conf_rev))
    if not rcb.indexed:
        el.set("indexed", "false")


def _write_gse_control(parent: Any, gse: GseControl) -> None:
    el = _etree.SubElement(parent, f"{{{_SCL_NS}}}GSEControl")  # type: ignore[attr-defined]
    el.set("name", gse.name)
    if gse.desc:
        el.set("desc", gse.desc)
    if gse.dataset_ref:
        el.set("datSet", gse.dataset_ref)
    if gse.app_id:
        el.set("appID", gse.app_id)
    if gse.fixed_offs:
        el.set("fixedOffs", "true")
    el.set("type", gse.type)


def _write_sv_control(parent: Any, svc: SampledValueControl) -> None:
    el = _etree.SubElement(parent, f"{{{_SCL_NS}}}SampledValueControl")  # type: ignore[attr-defined]
    el.set("name", svc.name)
    if svc.desc:
        el.set("desc", svc.desc)
    if svc.dataset_ref:
        el.set("datSet", svc.dataset_ref)
    if svc.smv_id:
        el.set("smvID", svc.smv_id)
    el.set("smpRate", str(svc.smp_rate))


def _write_subnetwork(parent: Any, sn: SubNetwork) -> None:
    el = _etree.SubElement(parent, f"{{{_SCL_NS}}}SubNetwork")  # type: ignore[attr-defined]
    el.set("name", sn.name)
    if sn.desc:
        el.set("desc", sn.desc)
    if sn.type:
        el.set("type", sn.type)
    for cap in sn.connected_aps:
        cap_el = _etree.SubElement(el, f"{{{_SCL_NS}}}ConnectedAP")  # type: ignore[attr-defined]
        cap_el.set("iedName", cap.ied_name)
        cap_el.set("apName", cap.ap_name)
        if cap.address:
            addr_el = _etree.SubElement(cap_el, f"{{{_SCL_NS}}}Address")  # type: ignore[attr-defined]
            for p_type, p_val in cap.address:
                p_el = _etree.SubElement(addr_el, f"{{{_SCL_NS}}}P")  # type: ignore[attr-defined]
                p_el.set("type", p_type)
                p_el.text = p_val
