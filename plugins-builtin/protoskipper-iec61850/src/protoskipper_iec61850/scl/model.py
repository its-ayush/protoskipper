# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""Frozen dataclass model for IEC 61850-6 SCL documents.

Only the elements critical for ProtoSkipper use-cases are fully
modelled: IEDs, LDevices, LNs, DataSets, RCBs, GSEControls,
SampledValueControls, SubNetworks, and ConnectedAPs.

``SclDocument.data_type_templates_xml`` preserves the raw
DataTypeTemplates bytes so that :func:`~.parser.write` can produce a
round-trip-valid file even though that section is not yet modelled in
detail.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A single result from :func:`~.parser.validate`."""

    severity: str  # "error" | "warning" | "info"
    line: int | None
    message: str
    rule: str


@dataclass(frozen=True, slots=True)
class FCDA:
    """One FCDA (Functional Constraint Data Attribute) inside a DataSet."""

    ld_inst: str = ""
    prefix: str = ""
    ln_class: str = ""
    ln_inst: str = ""
    do_name: str = ""
    da_name: str = ""
    fc: str = ""


@dataclass(frozen=True, slots=True)
class DataSet:
    """A DataSet element within a Logical Node."""

    name: str = ""
    desc: str = ""
    fcdas: tuple[FCDA, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportControl:
    """A ReportControl (buffered or unbuffered) element."""

    name: str = ""
    desc: str = ""
    dataset_ref: str = ""
    rpt_id: str = ""
    buffered: bool = False
    conf_rev: int = 1
    indexed: bool = True


@dataclass(frozen=True, slots=True)
class GseControl:
    """A GSEControl element (GOOSE or GSSE)."""

    name: str = ""
    desc: str = ""
    dataset_ref: str = ""
    app_id: str = ""
    fixed_offs: bool = False
    type: str = "GOOSE"  # "GOOSE" | "GSSE"


@dataclass(frozen=True, slots=True)
class SampledValueControl:
    """A SampledValueControl element."""

    name: str = ""
    desc: str = ""
    dataset_ref: str = ""
    smv_id: str = ""
    smp_rate: int = 80
    smp_mod: int = 0
    multi_cast: bool = True


@dataclass(frozen=True, slots=True)
class LN:
    """Logical Node (LN or LN0)."""

    prefix: str = ""
    ln_class: str = ""
    inst: str = ""
    ln_type: str = ""
    desc: str = ""
    datasets: tuple[DataSet, ...] = ()
    report_controls: tuple[ReportControl, ...] = ()
    gse_controls: tuple[GseControl, ...] = ()
    sv_controls: tuple[SampledValueControl, ...] = ()


@dataclass(frozen=True, slots=True)
class LDevice:
    """Logical Device (LDevice)."""

    inst: str = ""
    desc: str = ""
    ln0: LN | None = None
    lns: tuple[LN, ...] = ()


@dataclass(frozen=True, slots=True)
class AccessPoint:
    """An IED AccessPoint."""

    name: str = ""
    ldevices: tuple[LDevice, ...] = ()


@dataclass(frozen=True, slots=True)
class IED:
    """IED element.

    ``ldevices`` and ``lns`` are convenience properties that flatten the
    access-point hierarchy.
    """

    name: str = ""
    desc: str = ""
    manufacturer: str = ""
    model: str = ""
    config_version: str = ""
    access_points: tuple[AccessPoint, ...] = ()

    @property
    def ldevices(self) -> tuple[LDevice, ...]:
        """All LDevices across all AccessPoints."""
        result: list[LDevice] = []
        for ap in self.access_points:
            result.extend(ap.ldevices)
        return tuple(result)

    @property
    def lns(self) -> tuple[LN, ...]:
        """All LNs (including LN0) across all LDevices."""
        result: list[LN] = []
        for ld in self.ldevices:
            if ld.ln0 is not None:
                result.append(ld.ln0)
            result.extend(ld.lns)
        return tuple(result)


@dataclass(frozen=True, slots=True)
class ConnectedAP:
    """ConnectedAP — links an IED AccessPoint to a SubNetwork."""

    ied_name: str = ""
    ap_name: str = ""
    address: tuple[tuple[str, str], ...] = ()  # ((P-type, value), ...)


@dataclass(frozen=True, slots=True)
class SubNetwork:
    """Communication/SubNetwork element."""

    name: str = ""
    desc: str = ""
    type: str = ""  # "8-MMS" | "GOOSE" | "SMV" | ...
    connected_aps: tuple[ConnectedAP, ...] = ()


@dataclass(frozen=True, slots=True)
class Substation:
    """Top-level Substation element (name + desc; voltage-level hierarchy not modelled)."""

    name: str = ""
    desc: str = ""


@dataclass(frozen=True, slots=True)
class SclDocument:
    """Parsed IEC 61850-6 SCL document.

    All modelled sections are in the typed fields.
    ``data_type_templates_xml`` carries the raw DataTypeTemplates bytes so
    that :func:`~.parser.write` can produce a round-trip-valid output file.
    ``source_file`` records the filesystem path the document was read from.
    """

    version: str = ""
    revision: str = ""
    release: str = ""
    ieds: tuple[IED, ...] = ()
    subnetworks: tuple[SubNetwork, ...] = ()
    substations: tuple[Substation, ...] = ()
    # Preserved verbatim for round-trip; excluded from == and hash
    data_type_templates_xml: bytes = field(default=b"", compare=False, repr=False)
    source_file: str | None = None
