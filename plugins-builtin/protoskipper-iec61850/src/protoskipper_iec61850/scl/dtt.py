# Copyright (C) 2026 DataSailors Pvt Ltd.  Licensed under GPL-3.0-or-later.
"""DataTypeTemplates expansion — resolves LNodeType → DOType → DAType into IecTag objects.

Public API
----------
expand_tags(xml_bytes, ied_name=None) -> list[IecTag]
    Parse raw SCL XML bytes and return one :class:`IecTag` per leaf data attribute
    found across all matching IEDs.

Design notes
------------
* Uses stdlib ``xml.etree.ElementTree`` (no lxml dependency) so it works
  without the ``[scl]`` optional extra.
* DTT expansion is purely structural — it never opens a network connection.
* Recursion depth is bounded: real SCL files rarely exceed 6 levels deep in
  the DA hierarchy (DOType → DAType → BDAType…).
* The ``fc`` of a leaf attribute is always the ``fc`` attribute from the
  nearest ancestor ``DA`` element in the ``DOType``; ``BDA`` elements do not
  carry ``fc`` (they inherit from the enclosing ``DA``).
* ``SDO`` (sub-data-object) references another ``DOType`` whose ``DA`` elements
  each define their own ``fc``; recursion handles that naturally.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

__all__ = ["IecTag", "expand_tags"]

_SCL_NS = "http://www.iec.ch/61850/2003/SCL"

# Functional constraints that allow write/control operations.
_WRITABLE_FCS: frozenset[str] = frozenset(
    {"SP", "SE", "SV", "CF", "EX", "DC", "SG", "RP", "BK", "CO"}
)


def _t(local: str) -> str:
    """Return a Clark-notation tag name for the SCL namespace."""
    return f"{{{_SCL_NS}}}{local}"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IecTag:
    """One leaf data attribute in an IEC 61850 data model.

    Every field except ``desc`` is mandatory and non-empty for real tags.
    """

    ld_inst: str  # Logical Device instance, e.g. "CTRL"
    ln_prefix: str  # LN prefix, e.g. "" or "M"
    ln_class: str  # LN class, e.g. "MMXU"
    ln_inst: str  # LN instance, e.g. "1"
    do_name: str  # Data Object name, e.g. "A"
    da_path: str  # DA path from DO root, e.g. "phsA.cVal.mag.f"
    fc: str  # Functional Constraint, e.g. "MX"
    basic_type: str  # IEC 61850 basic type, e.g. "FLOAT32"
    cdc: str  # CDC of the parent DO, e.g. "WYE"
    desc: str = ""  # Human-readable description from SCL

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def ln_ref(self) -> str:
        """Full LN reference string, e.g. ``"MMXU1"`` or ``"MMMXU1"``."""
        return f"{self.ln_prefix}{self.ln_class}{self.ln_inst}"

    @property
    def mms_path(self) -> str:
        """Dot-notation MMS path suitable for ``IedConnection_readObject``.

        E.g. ``"CTRL/MMXU1.A.phsA.cVal.mag.f"``
        """
        base = f"{self.ld_inst}/{self.ln_ref}.{self.do_name}"
        return f"{base}.{self.da_path}" if self.da_path else base

    @property
    def object_id(self) -> str:
        """Object ID with ``[FC]`` suffix, usable as ``ObjectRef.object_id``.

        E.g. ``"CTRL/MMXU1.A.phsA.cVal.mag.f[MX]"``
        """
        return f"{self.mms_path}[{self.fc}]"

    @property
    def label(self) -> str:
        """Human-readable label for display in the GUI table."""
        return self.mms_path

    @property
    def writable(self) -> bool:
        """``True`` when the functional constraint permits write operations."""
        return self.fc in _WRITABLE_FCS

    @property
    def tree_path(self) -> tuple[str, str, str]:
        """``(ld_inst, ln_ref, do_name)`` — used to build the tree widget hierarchy."""
        return (self.ld_inst, self.ln_ref, self.do_name)


# ---------------------------------------------------------------------------
# Internal recursion helpers
# ---------------------------------------------------------------------------

_Leaf = tuple[str, str, str]  # (da_path, fc, basic_type)


def _expand_dotype(
    dotype: ET.Element,
    path_prefix: str,
    dotype_map: dict[str, ET.Element],
    datype_map: dict[str, ET.Element],
    depth: int = 0,
) -> list[_Leaf]:
    """Expand a ``DOType`` element into ``(da_path, fc, basic_type)`` triples.

    ``path_prefix`` is non-empty when called recursively for an SDO.
    """
    if depth > 12:  # safety guard against malformed cyclic refs
        return []

    results: list[_Leaf] = []

    # DA children — each has a mandatory fc attribute.
    for da_elem in dotype.findall(_t("DA")):
        name = da_elem.get("name", "")
        fc = da_elem.get("fc", "")
        btype = da_elem.get("bType", "")
        type_id = da_elem.get("type", "")
        full_path = f"{path_prefix}.{name}" if path_prefix else name

        if btype == "Struct":
            results.extend(_expand_datype(type_id, full_path, fc, datype_map, depth + 1))
        elif btype and fc:
            results.append((full_path, fc, btype))

    # SDO children — sub-data-objects reference another DOType.
    for sdo_elem in dotype.findall(_t("SDO")):
        name = sdo_elem.get("name", "")
        type_id = sdo_elem.get("type", "")
        full_path = f"{path_prefix}.{name}" if path_prefix else name
        sub_dotype = dotype_map.get(type_id)
        if sub_dotype is not None:
            results.extend(_expand_dotype(sub_dotype, full_path, dotype_map, datype_map, depth + 1))

    return results


def _expand_datype(
    datype_id: str,
    path_prefix: str,
    inherited_fc: str,
    datype_map: dict[str, ET.Element],
    depth: int = 0,
) -> list[_Leaf]:
    """Expand a ``DAType`` element into ``(da_path, fc, basic_type)`` triples.

    FC is inherited from the enclosing ``DA`` and propagated to all ``BDA`` leaves.
    """
    if depth > 12:
        return []

    datype = datype_map.get(datype_id)
    if datype is None:
        return []

    results: list[_Leaf] = []

    for bda_elem in datype.findall(_t("BDA")):
        name = bda_elem.get("name", "")
        btype = bda_elem.get("bType", "")
        type_id = bda_elem.get("type", "")
        full_path = f"{path_prefix}.{name}" if path_prefix else name

        if btype == "Struct":
            results.extend(_expand_datype(type_id, full_path, inherited_fc, datype_map, depth + 1))
        elif btype:
            results.append((full_path, inherited_fc, btype))

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def expand_tags(
    xml_bytes: bytes,
    ied_name: str | None = None,
) -> list[IecTag]:
    """Parse SCL XML bytes and return one :class:`IecTag` per leaf data attribute.

    Parameters
    ----------
    xml_bytes:
        Raw SCL XML (UTF-8 or UTF-16 with BOM).  GZip **must** be
        decompressed by the caller before passing here.
    ied_name:
        If given, only expand tags for this IED. ``None`` expands all IEDs.

    Returns
    -------
    list[IecTag]
        Flat list of all leaf DAs, sorted by ``(ld_inst, ln_ref, do_name, da_path)``.
    """
    # Strip UTF-8 BOM if present.
    if xml_bytes[:3] == b"\xef\xbb\xbf":
        xml_bytes = xml_bytes[3:]
    # Strip UTF-16 BOM.
    elif xml_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        xml_bytes = xml_bytes.decode("utf-16").encode("utf-8")

    root = ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))

    # ------------------------------------------------------------------
    # Build DataTypeTemplates lookup maps.
    # ------------------------------------------------------------------
    dotype_map: dict[str, ET.Element] = {}
    datype_map: dict[str, ET.Element] = {}
    lntype_map: dict[str, ET.Element] = {}

    dtt = root.find(_t("DataTypeTemplates"))
    if dtt is not None:
        for elem in dtt:
            id_ = elem.get("id", "")
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "LNodeType":
                lntype_map[id_] = elem
            elif local == "DOType":
                dotype_map[id_] = elem
            elif local == "DAType":
                datype_map[id_] = elem

    if not lntype_map:
        return []  # No DataTypeTemplates to expand.

    # ------------------------------------------------------------------
    # Walk the IED / AccessPoint / LDevice / LN hierarchy.
    # ------------------------------------------------------------------
    tags: list[IecTag] = []

    for ied_elem in root.findall(_t("IED")):
        this_ied = ied_elem.get("name", "")
        if ied_name is not None and this_ied != ied_name:
            continue

        for ap_elem in ied_elem.findall(_t("AccessPoint")):
            # LDevices can be either directly under AccessPoint or under Server
            server = ap_elem.find(_t("Server"))
            ld_container = server if server is not None else ap_elem
            for ld_elem in ld_container.findall(_t("LDevice")):
                ld_inst = ld_elem.get("inst", "")

                # Collect LN0 + LN elements
                ln_elems = list(ld_elem.findall(_t("LN0"))) + list(ld_elem.findall(_t("LN")))
                for ln_elem in ln_elems:
                    ln_class = ln_elem.get("lnClass", "")
                    ln_prefix = ln_elem.get("prefix", "")
                    ln_inst = ln_elem.get("inst", "")
                    ln_type = ln_elem.get("lnType", "")
                    ln_desc = ln_elem.get("desc", "")

                    lntype = lntype_map.get(ln_type)
                    if lntype is None:
                        continue

                    for do_elem in lntype.findall(_t("DO")):
                        do_name = do_elem.get("name", "")
                        do_type = do_elem.get("type", "")
                        do_desc = do_elem.get("desc", ln_desc)

                        dotype = dotype_map.get(do_type)
                        if dotype is None:
                            continue

                        cdc = dotype.get("cdc", "")
                        leaf_das = _expand_dotype(dotype, "", dotype_map, datype_map)

                        for da_path, fc, btype in leaf_das:
                            tags.append(
                                IecTag(
                                    ld_inst=ld_inst,
                                    ln_prefix=ln_prefix,
                                    ln_class=ln_class,
                                    ln_inst=ln_inst,
                                    do_name=do_name,
                                    da_path=da_path,
                                    fc=fc,
                                    basic_type=btype,
                                    cdc=cdc,
                                    desc=do_desc,
                                )
                            )

    tags.sort(key=lambda t: (t.ld_inst, t.ln_ref, t.do_name, t.da_path))
    return tags
