"""Serialise and validate OMA-CP documents.

XML is produced with ``lxml`` rather than string templates so attribute escaping
is always correct: subscriber-supplied values such as ``friendly_device_name``
can legitimately contain ``&``, ``<`` and quotes.
"""

from __future__ import annotations

from typing import Final

from lxml import etree

from acs.protocol.omacp.document import Characteristic, ProvisioningDoc

DOCTYPE: Final = (
    '<!DOCTYPE wap-provisioningdoc PUBLIC "-//WAPFORUM//DTD PROV 1.0//EN" '
    '"http://www.wapforum.org/DTD/prov.dtd">'
)


def _append(parent: etree._Element, characteristic: Characteristic) -> None:
    element = etree.SubElement(parent, "characteristic", {"type": characteristic.type})
    for parm in characteristic.parms:
        etree.SubElement(element, "parm", {"name": parm.name, "value": parm.value})
    for child in characteristic.children:
        _append(element, child)


def to_element(doc: ProvisioningDoc) -> etree._Element:
    root = etree.Element("wap-provisioningdoc", {"version": doc.version})
    for characteristic in doc.characteristics:
        _append(root, characteristic)
    return root


def to_xml(doc: ProvisioningDoc, *, doctype: bool = False, pretty: bool = True) -> bytes:
    """Serialise to UTF-8 XML bytes."""
    payload: bytes = etree.tostring(
        to_element(doc),
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=pretty,
        doctype=DOCTYPE if doctype else None,
    )
    return payload


def parse(payload: bytes) -> etree._Element:
    """Parse an OMA-CP document with entity expansion and network access off.

    OMA-CP documents reference an external DTD, so a naive parser would fetch it
    over the network and would be vulnerable to XXE. ``resolve_entities`` and
    ``no_network`` close both holes.
    """
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )
    return etree.fromstring(payload, parser=parser)


class ValidationIssue(str):
    """A human-readable structural problem found in a document."""


def validate_structure(payload: bytes) -> list[str]:
    """Structural validation of a serialised OMA-CP document.

    The OMA-CP DTD only constrains the generic characteristic/parm shape, so the
    useful checks are structural and semantic. Returns a list of problems; an
    empty list means the document is well formed for RCC.14 purposes.
    """
    problems: list[str] = []
    try:
        root = parse(payload)
    except etree.XMLSyntaxError as exc:
        return [f"not well-formed XML: {exc}"]

    if root.tag != "wap-provisioningdoc":
        problems.append(f"root element must be wap-provisioningdoc, got {root.tag}")
    if root.get("version") != "1.1":
        problems.append(f"root version must be 1.1, got {root.get('version')!r}")

    for element in root.iter():
        if element is root:
            continue
        if element.tag == "characteristic":
            if not element.get("type"):
                problems.append("characteristic without a type attribute")
        elif element.tag == "parm":
            if element.get("name") is None:
                problems.append("parm without a name attribute")
            if element.get("value") is None:
                problems.append(f"parm {element.get('name')!r} without a value attribute")
            if len(element) > 0:
                problems.append(f"parm {element.get('name')!r} must be empty")
        else:
            problems.append(f"unexpected element {element.tag}")

    if root.find("characteristic[@type='VERS']") is None:
        problems.append("mandatory VERS characteristic is missing")
    return problems
