"""Seeded adversarial inputs and exact resource boundaries for the DM codec."""

from __future__ import annotations

import random
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from xml.etree import ElementTree as ET

import pytest
from tests.test_dm_wbxml import HEADER, SMALL_WBXML

from acs.protocol.omadm import wbxml

INPUT_SECONDS = 2.0
XML_OPEN = b'<SyncML xmlns="SYNCML:SYNCML1.2" xmlns:metinf="syncml:metinf"><Data>'
XML_CLOSE = b"</Data></SyncML>"
VALUES = (
    "",
    " ",
    "\t\r\n ",
    " 한글 설정 📱🙂 ",
    "<&>\"'",
    "e\u0301",
    "\u00a0\u2003",
    "반복되는 값",
    "x" * 4096,
)


def _timeout(_signum: int, _frame: object) -> None:
    raise AssertionError(f"a single codec input exceeded {INPUT_SECONDS}s")


@contextmanager
def _deadline() -> Iterator[None]:
    # An elapsed-time assertion alone never returns if a mutated input hangs.
    previous = signal.signal(signal.SIGALRM, _timeout)
    started = time.perf_counter()
    signal.setitimer(signal.ITIMER_REAL, INPUT_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert time.perf_counter() - started < INPUT_SECONDS


def _decode_or_reject(payload: bytes) -> bool:
    with _deadline():
        try:
            xml = wbxml.decode(payload)
        except wbxml.WbxmlError:
            return False
        root = ET.fromstring(xml)
        assert root.tag in ("{SYNCML:SYNCML1.1}SyncML", "{SYNCML:SYNCML1.2}SyncML")
        assert len(xml) <= wbxml.MAX_XML_BYTES
    return True


def _uint(value: int, width: int = 0) -> bytes:
    digits = [value & 127]
    while value > 127:
        value >>= 7
        digits.insert(0, value & 127)
    digits = [0] * max(0, width - len(digits)) + digits
    return bytes([digit | 128 for digit in digits[:-1]] + digits[-1:])


def _tree_value(element: ET.Element) -> tuple[object, ...]:
    text = element.text or ""
    tail = element.tail or ""
    if len(element) and not text.strip(" \t\r\n"):
        text = ""
    if not tail.strip(" \t\r\n"):
        tail = ""
    return element.tag, text, tail, tuple(_tree_value(child) for child in element)


def _random_document(rng: random.Random, namespace: str) -> ET.Element:
    def sub(parent: ET.Element, name: str, text: str | None = None) -> ET.Element:
        element = ET.SubElement(parent, f"{{{namespace}}}{name}")
        element.text = text
        return element

    root = ET.Element(f"{{{namespace}}}SyncML")
    header = sub(root, "SyncHdr")
    sub(header, "VerDTD", namespace[-3:])
    sub(header, "VerProto", "DM/1.2")
    sub(header, "SessionID", str(rng.randrange(1, 100)))
    sub(header, "MsgID", str(rng.randrange(1, 100)))
    for name in ("Source", "Target"):
        sub(sub(header, name), "LocURI", rng.choice(VALUES))
    body = sub(root, "SyncBody")
    for index in range(rng.randrange(1, 12)):
        command = sub(body, rng.choice(("Add", "Replace", "Results", "Get", "Put", "Exec")))
        sub(command, "CmdID", str(index + 1))
        for _ in range(rng.randrange(1, 5)):
            item = sub(command, "Item")
            sub(sub(item, rng.choice(("Source", "Target"))), "LocURI", "./DevInfo/Mod")
            meta = sub(item, "Meta")
            for name, value in (("Format", "chr"), ("Type", "text/plain")):
                ET.SubElement(meta, f"{{syncml:metinf}}{name}").text = value
            if rng.choice((False, True)):
                anchor = ET.SubElement(meta, "{syncml:metinf}Anchor")
                for name in ("Last", "Next"):
                    ET.SubElement(anchor, f"{{syncml:metinf}}{name}").text = rng.choice(VALUES)
            sub(item, "Data", rng.choice(VALUES))
    sub(body, "Final")
    for element in root.iter():
        if len(element):
            element.text = rng.choice(("", "\n  ", "\t", "\r\n"))
            for child in element:
                child.tail = rng.choice(("", "\n    ", "\t", "\r\n"))
    return root


@pytest.mark.spec
@pytest.mark.parametrize("seed", [10, 192])
@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("namespace", ["SYNCML:SYNCML1.1", "SYNCML:SYNCML1.2"])
@pytest.mark.parametrize(
    ("use_string_table", "opaque"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_seeded_syncml_trees_preserve_values_and_structure(
    seed: int, version: int, namespace: str, use_string_table: bool, opaque: bool
) -> None:
    rng = random.Random(seed)
    for index in range(20):
        root = _random_document(rng, namespace)
        xml = ET.tostring(root, encoding="utf-8").replace(b"\r", b"&#13;")
        public_id = f"-//SYNCML//DTD SyncML {namespace[-3:]}//EN" if index % 2 else None
        with _deadline():
            wire = wbxml.encode(
                xml,
                version=version,
                public_id=public_id,
                use_string_table=use_string_table,
                opaque=opaque,
            )
            decoded = ET.fromstring(wbxml.decode(wire))
        assert _tree_value(decoded) == _tree_value(root), (seed, index)
        assert all(element.text is None for element in decoded.iter() if len(element))
        assert all(element.tail is None for element in decoded.iter())


@pytest.mark.parametrize("seed", [10, 192, 65535])
def test_seeded_arbitrary_bytes_only_decode_or_raise_wbxml_error(seed: int) -> None:
    rng = random.Random(seed)
    for index in range(1000):
        raw = rng.randbytes(rng.randrange(2049))
        payload = raw if index % 2 else HEADER + b"\x6d" + raw + b"\x01"
        _decode_or_reject(payload)
    for size in (wbxml.MAX_INPUT_BYTES - 1, wbxml.MAX_INPUT_BYTES, wbxml.MAX_INPUT_BYTES + 1):
        _decode_or_reject(rng.randbytes(size))
    # Valid page switches force a scan of almost the entire input budget.
    switches = b"\x00\x00" * ((wbxml.MAX_INPUT_BYTES - len(HEADER) - 1) // 2)
    assert _decode_or_reject(HEADER + switches + b"\x2d")


@pytest.mark.parametrize("mutation", ["replace", "insert", "truncate"])
def test_every_single_byte_mutation_of_a_fixed_vector_is_bounded(mutation: str) -> None:
    accepted = rejected = 0
    for position in range(len(SMALL_WBXML) + 1):
        if mutation == "truncate":
            candidates = [SMALL_WBXML[:position]]
        elif mutation == "insert":
            candidates = [
                SMALL_WBXML[:position] + bytes([value]) + SMALL_WBXML[position:]
                for value in range(256)
            ]
        else:
            if position == len(SMALL_WBXML):
                continue
            candidates = [
                SMALL_WBXML[:position] + bytes([value]) + SMALL_WBXML[position + 1 :]
                for value in range(256)
                if value != SMALL_WBXML[position]
            ]
        for candidate in candidates:
            if _decode_or_reject(candidate):
                accepted += 1
            else:
                rejected += 1
    assert accepted > 0 and rejected > 0


@pytest.mark.parametrize("mode", ["inline", "table", "opaque"])
def test_each_position_of_a_multilingual_metadata_message_can_be_mutated_safely(
    mode: str,
) -> None:
    xml = (
        '<SyncML xmlns="SYNCML:SYNCML1.2"><SyncHdr><SessionID>1</SessionID>'
        '<MsgID>1</MsgID><Meta><Type xmlns="syncml:metinf">반복 문자열📱</Type>'
        "</Meta></SyncHdr><SyncBody><Replace><CmdID>2</CmdID><Item>"
        "<Source><LocURI>./DevInfo/Mod</LocURI></Source><Data>반복 문자열📱</Data>"
        "</Item></Replace><Final/></SyncBody></SyncML>"
    ).encode()
    wire = wbxml.encode(
        xml,
        public_id="-//SYNCML//DTD SyncML 1.2//EN",
        use_string_table=mode == "table",
        opaque=mode == "opaque",
    )
    rng = random.Random(192)
    for position in range(len(wire) + 1):
        _decode_or_reject(wire[:position])
        _decode_or_reject(wire[:position] + bytes([rng.randrange(256)]) + wire[position:])
        if position < len(wire):
            _decode_or_reject(wire[:position] + wire[position + 1 :])
            _decode_or_reject(
                wire[:position]
                + bytes([wire[position] ^ (1 << (position % 8))])
                + wire[position + 1 :]
            )


@pytest.mark.parametrize("excess", [0, 1])
def test_the_input_byte_limit_accepts_exactly_the_boundary(excess: int) -> None:
    prefix, suffix = HEADER + b"\x6d\x4f\x03", b"\x00\x01\x01"
    length = wbxml.MAX_INPUT_BYTES + excess
    wire = prefix + b"x" * (length - len(prefix) - len(suffix)) + suffix
    assert len(wire) == length
    with _deadline():
        if excess:
            with pytest.raises(wbxml.WbxmlError, match="input exceeds size"):
                wbxml.decode(wire)
        else:
            xml = wbxml.decode(wire)
            assert ET.fromstring(xml)[0].text == "x" * (length - len(prefix) - len(suffix))


@pytest.mark.parametrize("excess", [0, 1])
@pytest.mark.parametrize(
    ("raw", "expanded"), [(b"x" * 4096, b"x" * 4096), (b"<&>\r" * 256, b"&lt;&amp;&gt;&#13;" * 256)]
)
def test_the_decoded_byte_limit_includes_escaped_text_and_closing_tags(
    excess: int, raw: bytes, expanded: bytes
) -> None:
    length = wbxml.MAX_XML_BYTES + excess
    repeats, remainder = divmod(length - len(XML_OPEN) - len(XML_CLOSE), len(expanded))
    table = raw + b"\x00"
    wire = (
        HEADER[:-1]
        + _uint(len(table))
        + table
        + b"\x6d\x4f"
        + b"\x83\x00" * repeats
        + b"\x03"
        + b"x" * remainder
        + b"\x00\x01\x01"
    )
    assert len(wire) < wbxml.MAX_INPUT_BYTES
    with _deadline():
        if excess:
            with pytest.raises(wbxml.WbxmlError, match="output exceeds size"):
                wbxml.decode(wire)
        else:
            xml = wbxml.decode(wire)
            assert xml == XML_OPEN + expanded * repeats + b"x" * remainder + XML_CLOSE
            assert len(xml) == length
            assert ET.fromstring(xml)[0].text == raw.decode() * repeats + "x" * remainder


@pytest.mark.parametrize("excess", [0, 1])
@pytest.mark.parametrize("content_leaf", [False, True])
def test_the_depth_limit_counts_both_empty_and_content_elements(
    excess: int, content_leaf: bool
) -> None:
    depth = wbxml.MAX_DEPTH + excess
    leaf = b"\x54\x03value\x00\x01" if content_leaf else b"\x14"
    wire = HEADER + b"\x6d" + b"\x54" * (depth - 2) + leaf + b"\x01" * (depth - 1)
    with _deadline():
        if excess:
            with pytest.raises(wbxml.WbxmlError, match="depth limit"):
                wbxml.decode(wire)
        else:
            xml = wbxml.decode(wire)
            assert sum(1 for _ in ET.fromstring(xml).iter()) == depth
            assert wbxml.encode(xml) == wire


@pytest.mark.parametrize("excess", [0, 1])
@pytest.mark.parametrize("content_leaf", [False, True])
def test_the_element_limit_counts_both_empty_and_content_elements(
    excess: int, content_leaf: bool
) -> None:
    count = wbxml.MAX_ELEMENTS + excess
    leaf = b"\x4f\x03x\x00\x01" if content_leaf else b"\x0f"
    wire = HEADER + b"\x6d" + leaf * (count - 1) + b"\x01"
    with _deadline():
        if excess:
            with pytest.raises(wbxml.WbxmlError, match="element count"):
                wbxml.decode(wire)
        else:
            xml = wbxml.decode(wire)
            assert sum(1 for _ in ET.fromstring(xml).iter()) == count


def _integer_document(field: str, integer: bytes) -> bytes:
    public_name = b"-//SYNCML//DTD SyncML 1.2//EN\x00"
    fields = {
        "public_id": b"\x03" + integer + b"\x6a\x00\x2d",
        "public_offset": (
            b"\x03\x00" + integer + b"\x6a" + _uint(len(public_name)) + public_name + b"\x2d"
        ),
        "charset": b"\x03\xa4\x01" + integer + b"\x00\x2d",
        "table_length": b"\x03\xa4\x01\x6a" + integer + b"x\x00\x2d",
        "table_offset": b"\x03\xa4\x01\x6a\x02x\x00\x6d\x4f\x83" + integer + b"\x01\x01",
        "opaque_length": HEADER + b"\x6d\x4f\xc3" + integer + b"x\x01\x01",
    }
    return fields[field]


@pytest.mark.spec
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_id", 0x1201),
        ("public_offset", 0),
        ("charset", 106),
        ("table_length", 2),
        ("table_offset", 0),
        ("opaque_length", 1),
    ],
)
def test_every_integer_position_accepts_five_bytes_but_rejects_six(field: str, value: int) -> None:
    assert len(_uint(value, 5)) == wbxml.MAX_MB_UINT32_BYTES
    assert _decode_or_reject(_integer_document(field, _uint(value, 5)))
    with pytest.raises(wbxml.WbxmlError, match="five bytes"):
        wbxml.decode(_integer_document(field, _uint(value, 6)))


@pytest.mark.spec
@pytest.mark.parametrize(
    "field",
    ["public_id", "public_offset", "charset", "table_length", "table_offset", "opaque_length"],
)
def test_uint32_maximum_is_parsed_without_wraparound_and_overflow_is_rejected(field: str) -> None:
    with pytest.raises(wbxml.WbxmlError) as accepted_integer:
        wbxml.decode(_integer_document(field, b"\x8f\xff\xff\xff\x7f"))
    assert "uint32" not in str(accepted_integer.value)
    assert "five bytes" not in str(accepted_integer.value)
    with pytest.raises(wbxml.WbxmlError, match="uint32"):
        wbxml.decode(_integer_document(field, b"\x90\x80\x80\x80\x00"))


@pytest.mark.spec
@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("mode", ["inline", "table", "opaque"])
def test_leaf_whitespace_survives_without_strings_between_elements(value: str, mode: str) -> None:
    root = ET.Element("{SYNCML:SYNCML1.2}SyncML")
    body = ET.SubElement(root, "{SYNCML:SYNCML1.2}SyncBody")
    for _ in range(2):
        ET.SubElement(body, "{SYNCML:SYNCML1.2}Data").text = value
    ET.indent(root)
    xml = ET.tostring(root, encoding="utf-8").replace(b"\r", b"&#13;")
    wire = wbxml.encode(xml, use_string_table=mode == "table", opaque=mode == "opaque")
    decoded = ET.fromstring(wbxml.decode(wire))
    assert [(element.text or "") for element in decoded[0]] == [value, value]
    assert decoded.text is None and decoded[0].text is None
    assert all(element.tail is None for element in decoded.iter())


@pytest.mark.parametrize("encoding", ["qa-unknown-encoding", "UTF-7", "UTF-32"])
def test_unsupported_xml_encoding_declarations_raise_the_codec_error(encoding: str) -> None:
    payload = f'<?xml version="1.0" encoding="{encoding}"?><SyncML/>'.encode()
    with pytest.raises(wbxml.WbxmlError):
        wbxml.encode(payload)
