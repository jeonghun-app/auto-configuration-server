"""WBXML wire vectors and bounded rejection of malformed device input."""

from __future__ import annotations

from xml.etree import ElementTree

import pytest

from acs.protocol.omadm import syncml, wbxml

HEADER = b"\x03\xa4\x01\x6a\x00"
SMALL_XML = (
    b'<SyncML xmlns="SYNCML:SYNCML1.2"><SyncHdr><SessionID>1</SessionID>'
    b"<MsgID>1</MsgID></SyncHdr><SyncBody><Alert><CmdID>1</CmdID>"
    b"<Data>1201</Data></Alert><Final/></SyncBody></SyncML>"
)
# WAP-192 header: version 1.3, public id 0x1201, UTF-8, empty string table.
# Each 0x03 introduces a NUL-terminated string; each 0x01 closes a content tag.
SMALL_BODY = bytes.fromhex(
    "6d 6c 65 03 31 00 01 5b 03 31 00 01 01 "
    "6b 46 4b 03 31 00 01 4f 03 31 32 30 31 00 01 01 12 01 01"
)
SMALL_WBXML = HEADER + SMALL_BODY


@pytest.mark.spec
def test_a_fixed_wbxml_vector_decodes_to_the_expected_syncml_message() -> None:
    message = syncml.parse(wbxml.decode(SMALL_WBXML))
    assert message == syncml.parse(SMALL_XML)
    assert message.header.session_id == "1"
    assert message.header.msg_id == "1"
    assert message.has_alert("1201")
    assert message.final


@pytest.mark.spec
def test_a_small_syncml_message_encodes_to_the_fixed_wbxml_vector() -> None:
    assert wbxml.encode(SMALL_XML) == SMALL_WBXML


@pytest.mark.spec
@pytest.mark.parametrize("use_string_table", [False, True])
def test_indentation_is_omitted_from_the_fixed_wbxml_vector(use_string_table: bool) -> None:
    xml = SMALL_XML.replace(b"><", b">\n    <")
    assert wbxml.encode(xml, use_string_table=use_string_table) == SMALL_WBXML


@pytest.mark.spec
@pytest.mark.parametrize("use_string_table", [False, True])
@pytest.mark.parametrize("opaque", [False, True])
@pytest.mark.parametrize("value", [" \t\r\n ", " value \t ", "\u00a0"])
def test_removing_indentation_preserves_whitespace_in_leaf_values(
    use_string_table: bool, opaque: bool, value: str
) -> None:
    text = value.replace("\r", "&#13;")
    xml = (
        f"<SyncML>\n  <SyncBody>\n    <Data>{text}</Data>\n"
        f"    <Data>{text}</Data>\n    <Final/>\n  </SyncBody>\n</SyncML>"
    ).encode()
    wire = wbxml.encode(xml, use_string_table=use_string_table, opaque=opaque)
    root = ElementTree.fromstring(wbxml.decode(wire))
    assert [element.text for element in root.iter("{SYNCML:SYNCML1.2}Data")] == [value, value]
    assert all(element.text is None for element in root.iter() if len(element))
    assert all(element.tail is None for element in root.iter())


@pytest.mark.spec
@pytest.mark.parametrize("version", [wbxml.VERSION_12, wbxml.VERSION_13])
@pytest.mark.parametrize(
    ("namespace", "public_id"),
    [
        ("SYNCML:SYNCML1.1", wbxml.PUBLIC_ID_11),
        ("SYNCML:SYNCML1.1", "-//SYNCML//DTD SyncML 1.1//EN"),
        ("SYNCML:SYNCML1.2", wbxml.PUBLIC_ID_12),
        ("SYNCML:SYNCML1.2", "-//SYNCML//DTD SyncML 1.2//EN"),
    ],
)
def test_both_wbxml_versions_and_syncml_public_identifiers_round_trip(
    version: int, namespace: str, public_id: int | str
) -> None:
    xml = SMALL_XML.replace(b"SYNCML:SYNCML1.2", namespace.encode())
    wire = wbxml.encode(xml, version=version, public_id=public_id)
    assert wire[0] == version
    decoded = wbxml.decode(wire)
    assert ElementTree.fromstring(decoded).tag == f"{{{namespace}}}SyncML"
    assert syncml.parse(decoded) == syncml.parse(xml)


@pytest.mark.spec
def test_a_string_public_identifier_can_start_at_a_nonzero_table_offset() -> None:
    table = b"x\x00-//SYNCML//DTD SyncML 1.1//EN\x00"
    wire = b"\x02\x00\x02\x6a" + bytes([len(table)]) + table + SMALL_BODY
    assert ElementTree.fromstring(wbxml.decode(wire)).tag == "{SYNCML:SYNCML1.1}SyncML"


@pytest.mark.spec
@pytest.mark.parametrize("use_string_table", [False, True])
@pytest.mark.parametrize("opaque", [False, True])
def test_commands_metadata_and_unicode_values_survive_a_round_trip(
    use_string_table: bool, opaque: bool
) -> None:
    builder = syncml.SyncMlBuilder("42", 1, "device", "server")
    builder.status("SyncHdr", "1", "0", "407", challenge=(syncml.AUTH_MD5, "bm9uY2U="))
    builder.get(["./DevInfo/Mod"])
    builder.add([("./RCS", "", "node", "node")])
    builder.replace(
        [
            ("./RCS/Name", "설정 & <text>", "chr", "text/plain"),
            ("./RCS/Other", "설정 & <text>", "chr", "text/plain"),
        ]
    )
    builder.exec_("./FUMO/Download", "download")
    xml = builder.build()
    wire = wbxml.encode(xml, use_string_table=use_string_table, opaque=opaque)
    assert syncml.parse(wbxml.decode(wire)) == syncml.parse(xml)
    if opaque:
        assert bytes([wbxml.OPAQUE]) in wire
    elif use_string_table:
        assert bytes([wbxml.STR_T]) in wire


@pytest.mark.spec
def test_switch_page_state_persists_after_end_tokens() -> None:
    # Closing Meta does not reset page 1; a SWITCH_PAGE is needed for SyncBody.
    body = (
        b"\x6d\x6c\x5a\x00\x01\x4c\x031024\x00\x01"
        b"\x45\x4a\x03before\x00\x01\x4f\x03after\x00\x01\x01"
        b"\x01\x01\x00\x00\x6b\x12\x01\x01"
    )
    xml = wbxml.decode(HEADER + body)
    assert syncml.parse(xml).header.max_msg_size == 1024
    root = ElementTree.fromstring(xml)
    assert root.findtext(".//{syncml:metinf}Last") == "before"
    assert root.findtext(".//{syncml:metinf}Next") == "after"
    assert ElementTree.canonicalize(wbxml.decode(wbxml.encode(xml))) == ElementTree.canonicalize(
        xml
    )


@pytest.mark.spec
def test_inline_table_and_opaque_text_can_be_concatenated() -> None:
    table = b"prefix-tail\x00"
    wire = (
        b"\x03\xa4\x01\x6a"
        + bytes([len(table)])
        + table
        + b"\x6d\x6c\x01\x6b\x46\x4f\x03head\x00\xc3\x01-\x83\x07"
        + b"\x01\x01\x12\x01\x01"
    )
    assert syncml.parse(wbxml.decode(wire)).of("Alert")[0].data == "head-tail"


@pytest.mark.spec
def test_opaque_utf8_preserves_xml_escaping_and_carriage_returns() -> None:
    raw = "설정<&>\r\n".encode()
    wire = HEADER + b"\x6d\x4f\xc3" + bytes([len(raw)]) + raw + b"\x01\x01"
    root = ElementTree.fromstring(wbxml.decode(wire))
    assert root.findtext("{SYNCML:SYNCML1.2}Data") == raw.decode()


@pytest.mark.spec
def test_a_syncml_namespace_can_be_inferred_when_encoding_unqualified_xml() -> None:
    assert wbxml.encode(SMALL_XML.replace(b' xmlns="SYNCML:SYNCML1.2"', b"")) == SMALL_WBXML


INVALID_DOCUMENTS = [
    (b"", "truncated"),
    (b"\x01", "version"),
    (b"\x04", "version"),
    (b"\x03\x80", "truncated"),
    (b"\x03\x80\x80\x80\x80\x80\x00", "five bytes"),
    (b"\x03\x90\x80\x80\x80\x00", "uint32"),
    (b"\x03\x8f\xff\xff\xff\x7f\x6a\x00", "public identifier"),
    (b"\x03\x01\x6a\x00", "public identifier"),
    (b"\x03\xa4\x01\x04\x00", "charset"),
    (b"\x03\xa4\x01\x6a\x05abc", "truncated"),
    (b"\x03\xa4\x01\x6a\x80", "truncated"),
    (b"\x03\x00\x00\x6a\x00", "offset"),
    (b"\x03\x00\x01\x6a\x01\x00", "offset"),
    (b"\x03\x00\x00\x6a\x03abc", "unterminated"),
    (b"\x03\x00\x00\x6a\x02x\x00", "public identifier"),
    (HEADER, "root is missing"),
    (HEADER + b"\x2b", "root must be SyncML"),
    (HEADER + b"\x00\x01\x05", "root must be SyncML"),
    (HEADER + b"\x2d\x2d", "trailing"),
    (HEADER + b"\x01", "unexpected"),
    (HEADER + b"\x00", "truncated"),
    (HEADER + b"\x00\x02", "code page"),
    (HEADER + b"\x6d", "missing WBXML END"),
    (HEADER + b"\x6d\x30", "unknown WBXML tag"),
    (HEADER + b"\x6d\x7f", "unknown WBXML tag"),
    (HEADER + b"\x6d\x00\x01\x17", "unknown WBXML tag"),
    (HEADER + b"\xad", "attributes"),
    (HEADER + b"\xed", "attributes"),
    (HEADER + b"\x84", "attributes"),
    (HEADER + b"\xc4", "attributes"),
    (HEADER + b"\x03x\x00", "outside the root"),
    (HEADER + b"\x83\x00", "outside the root"),
    (HEADER + b"\xc3\x00", "outside the root"),
    (HEADER + b"\x6d\x03secret", "unterminated"),
    (HEADER + b"\x6d\x03\xff\x00", "not UTF-8"),
    (HEADER + b"\x6d\x03\x01\x00", "invalid XML character"),
    (HEADER + b"\x6d\xc3\x01\x00", "invalid XML character"),
    (HEADER + b"\x6d\xc3\x02\xff\xff", "not UTF-8"),
    (HEADER + b"\x6d\xc3\x02x", "truncated"),
    (HEADER + b"\x6d\xc3\xff\xff\xff\xff\x7f", "uint32"),
    (HEADER + b"\x6d\x83\x00", "offset"),
    (HEADER + b"\x6d\x83\x80", "truncated"),
    (b"\x03\xa4\x01\x6a\x01x\x6d\x83\x00", "unterminated"),
    (b"\x03\xa4\x01\x6a\x03\xc3\xa9\x00\x6d\x83\x01", "not UTF-8"),
]
INVALID_DOCUMENTS.extend(
    (HEADER + b"\x6d" + bytes([token]), "global token")
    for token in (0x02, 0x04, 0x40, 0x41, 0x42, 0x43, 0x44, 0x80, 0x81, 0x82, 0xC0, 0xC1, 0xC2)
)


@pytest.mark.spec
@pytest.mark.parametrize(("wire", "reason"), INVALID_DOCUMENTS)
def test_malformed_or_unsupported_wbxml_is_rejected(wire: bytes, reason: str) -> None:
    with pytest.raises(wbxml.WbxmlError, match=reason):
        wbxml.decode(wire)


def test_wbxml_input_size_is_bounded_before_parsing() -> None:
    with pytest.raises(wbxml.WbxmlError, match="input exceeds size"):
        wbxml.decode(b"x" * (wbxml.MAX_INPUT_BYTES + 1))


def test_wbxml_nesting_is_bounded_in_both_directions() -> None:
    wire = (
        HEADER
        + b"\x6d"
        + b"\x54" * (wbxml.MAX_DEPTH - 2)
        + b"\x14"
        + b"\x01" * (wbxml.MAX_DEPTH - 1)
    )
    xml = wbxml.decode(wire)
    assert wbxml.encode(xml) == wire
    excessive_wire = HEADER + b"\x6d" + b"\x54" * wbxml.MAX_DEPTH
    with pytest.raises(wbxml.WbxmlError, match="depth limit"):
        wbxml.decode(excessive_wire)
    response_xml = (
        b"<SyncML>"
        + b"<Item>" * (wbxml.MAX_ENCODE_DEPTH - 1)
        + b"</Item>" * (wbxml.MAX_ENCODE_DEPTH - 1)
        + b"</SyncML>"
    )
    assert wbxml.encode(response_xml) == (
        HEADER
        + b"\x6d"
        + b"\x54" * (wbxml.MAX_ENCODE_DEPTH - 2)
        + b"\x14"
        + b"\x01" * (wbxml.MAX_ENCODE_DEPTH - 1)
    )
    excessive_xml = (
        b"<SyncML>"
        + b"<Item>" * wbxml.MAX_ENCODE_DEPTH
        + b"</Item>" * wbxml.MAX_ENCODE_DEPTH
        + b"</SyncML>"
    )
    with pytest.raises(wbxml.WbxmlError, match="depth limit"):
        wbxml.encode(excessive_xml)


def test_wbxml_element_count_is_bounded_in_both_directions() -> None:
    wire = HEADER + b"\x6d" + b"\x12" * (wbxml.MAX_ELEMENTS - 1) + b"\x01"
    xml = wbxml.decode(wire)
    assert wbxml.encode(xml) == wire
    with pytest.raises(wbxml.WbxmlError, match="element count"):
        wbxml.decode(wire[:-1] + b"\x12\x01")
    response_xml = b"<SyncML>" + b"<Final/>" * (wbxml.MAX_ENCODE_ELEMENTS - 1) + b"</SyncML>"
    assert wbxml.encode(response_xml) == (
        HEADER + b"\x6d" + b"\x12" * (wbxml.MAX_ENCODE_ELEMENTS - 1) + b"\x01"
    )
    with pytest.raises(wbxml.WbxmlError, match="element count"):
        wbxml.encode(response_xml.replace(b"</SyncML>", b"<Final/></SyncML>"))


def expansion_bomb() -> bytes:
    table = b"x" * 1024 + b"\x00"
    return (
        b"\x03\xa4\x01\x6a\x88\x01"
        + table
        + b"\x6d"
        + b"\x83\x00" * (wbxml.MAX_XML_BYTES // 1024 + 1)
        + b"\x01"
    )


def test_repeated_string_table_references_cannot_exhaust_memory() -> None:
    wire = expansion_bomb()
    assert len(wire) < 8192
    with pytest.raises(wbxml.WbxmlError, match="output exceeds size"):
        wbxml.decode(wire)


def test_the_encoder_bounds_xml_input_and_wbxml_output() -> None:
    with pytest.raises(wbxml.WbxmlError, match="XML input exceeds size"):
        wbxml.encode(b"x" * (wbxml.MAX_ENCODE_XML_BYTES + 1))
    with pytest.raises(wbxml.WbxmlError, match="output exceeds size"):
        wbxml.encode(b"<SyncML><Data>" + b"x" * wbxml.MAX_ENCODE_BYTES + b"</Data></SyncML>")


@pytest.mark.parametrize(
    ("xml", "reason"),
    [
        (b"<SyncML", "malformed XML"),
        (b"<Other/>", "root must be SyncML"),
        (b"<SyncML><Other/></SyncML>", "unknown XML tag"),
        (b'<SyncML xmlns="urn:other"/>', "namespace"),
        (b'<SyncML><Data xmlns="urn:other"/></SyncML>', "namespace"),
        (b'<SyncML attr="secret"/>', "attributes"),
        (b'<!DOCTYPE SyncML SYSTEM "file:///etc/passwd"><SyncML/>', "document types"),
        (b'<!DOCTYPE SyncML [<!ENTITY x "secret">]><SyncML>&x;</SyncML>', "document types"),
    ],
)
def test_xml_that_cannot_be_encoded_is_rejected(xml: bytes, reason: str) -> None:
    with pytest.raises(wbxml.WbxmlError, match=reason):
        wbxml.encode(xml)


@pytest.mark.spec
@pytest.mark.parametrize("public_id", [1, "unknown", wbxml.PUBLIC_ID_11])
def test_the_encoder_rejects_unknown_or_mismatched_public_identifiers(
    public_id: int | str,
) -> None:
    with pytest.raises(wbxml.WbxmlError, match="public identifier"):
        wbxml.encode(SMALL_XML, public_id=public_id)


@pytest.mark.spec
def test_the_encoder_rejects_unknown_wbxml_versions() -> None:
    with pytest.raises(wbxml.WbxmlError, match="version"):
        wbxml.encode(SMALL_XML, version=0x04)


@pytest.mark.spec
@pytest.mark.parametrize(
    ("value", "wire"),
    [
        (0, b"\x00"),
        (127, b"\x7f"),
        (128, b"\x81\x00"),
        (wbxml.MAX_UINT32, b"\x8f\xff\xff\xff\x7f"),
    ],
)
def test_multibyte_integer_encoding_respects_uint32_boundaries(value: int, wire: bytes) -> None:
    assert wbxml._mb_uint32(value) == wire


@pytest.mark.spec
@pytest.mark.parametrize("value", [-1, wbxml.MAX_UINT32 + 1])
def test_multibyte_integer_encoding_rejects_values_outside_uint32(value: int) -> None:
    with pytest.raises(wbxml.WbxmlError, match="uint32"):
        wbxml._mb_uint32(value)
