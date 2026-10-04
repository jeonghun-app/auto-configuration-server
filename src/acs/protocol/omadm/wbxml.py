"""Bounded WBXML 1.2/1.3 conversion for SyncML 1.1/1.2 and MetInf.

The wire constants follow WAP Binary XML Content Format (WAP-192, WBXML 1.3)
and the public SyncML Representation Protocol token tables, also published in
libwbxml's SyncML tables. This is a text-valued DM codec, not a general WBXML
processor: attributes, literal tags, extensions and other code pages are refused.
"""

from __future__ import annotations

from collections import Counter
from typing import Final
from xml.etree import ElementTree
from xml.sax.saxutils import escape

VERSION_12: Final = 0x02
VERSION_13: Final = 0x03
PUBLIC_ID_11: Final = 0x0FD3
PUBLIC_ID_12: Final = 0x1201
CHARSET_UTF8: Final = 106

MAX_INPUT_BYTES: Final = 512 * 1024
MAX_XML_BYTES: Final = 2 * 1024 * 1024
MAX_DEPTH: Final = 64
MAX_ELEMENTS: Final = 16384
MAX_MB_UINT32_BYTES: Final = 5
MAX_UINT32: Final = (1 << 32) - 1

# Each received command can expand to a seven-element Status. Eight times the
# request node budget leaves room for those statuses and catalogue commands.
# Server XML also includes indentation and repeated references, so response
# byte/depth budgets are separate from the untrusted input limits.
MAX_ENCODE_XML_BYTES: Final = 32 * 1024 * 1024
MAX_ENCODE_BYTES: Final = 16 * 1024 * 1024
MAX_ENCODE_DEPTH: Final = 128
MAX_ENCODE_ELEMENTS: Final = 8 * MAX_ELEMENTS

SWITCH_PAGE: Final = 0x00
END: Final = 0x01
STR_I: Final = 0x03
STR_T: Final = 0x83
OPAQUE: Final = 0xC3
CONTENT: Final = 0x40
ATTRIBUTES: Final = 0x80

METINF_NS: Final = "syncml:metinf"
PUBLIC_IDS: Final = {
    PUBLIC_ID_11: ("-//SYNCML//DTD SyncML 1.1//EN", "SYNCML:SYNCML1.1"),
    PUBLIC_ID_12: ("-//SYNCML//DTD SyncML 1.2//EN", "SYNCML:SYNCML1.2"),
}

# The gap at 0x30 is reserved in the public SyncML token table.
SYNCML_TAGS: Final = {
    0x05: "Add",
    0x06: "Alert",
    0x07: "Archive",
    0x08: "Atomic",
    0x09: "Chal",
    0x0A: "Cmd",
    0x0B: "CmdID",
    0x0C: "CmdRef",
    0x0D: "Copy",
    0x0E: "Cred",
    0x0F: "Data",
    0x10: "Delete",
    0x11: "Exec",
    0x12: "Final",
    0x13: "Get",
    0x14: "Item",
    0x15: "Lang",
    0x16: "LocName",
    0x17: "LocURI",
    0x18: "Map",
    0x19: "MapItem",
    0x1A: "Meta",
    0x1B: "MsgID",
    0x1C: "MsgRef",
    0x1D: "NoResp",
    0x1E: "NoResults",
    0x1F: "Put",
    0x20: "Replace",
    0x21: "RespURI",
    0x22: "Results",
    0x23: "Search",
    0x24: "Sequence",
    0x25: "SessionID",
    0x26: "SftDel",
    0x27: "Source",
    0x28: "SourceRef",
    0x29: "Status",
    0x2A: "Sync",
    0x2B: "SyncBody",
    0x2C: "SyncHdr",
    0x2D: "SyncML",
    0x2E: "Target",
    0x2F: "TargetRef",
    0x31: "VerDTD",
    0x32: "VerProto",
    0x33: "NumberOfChanges",
    0x34: "MoreData",
    0x35: "Field",
    0x36: "Filter",
    0x37: "Record",
    0x38: "FilterType",
    0x39: "SourceParent",
    0x3A: "TargetParent",
    0x3B: "Move",
    0x3C: "Correlator",
}
METINF_TAGS: Final = {
    0x05: "Anchor",
    0x06: "EMI",
    0x07: "Format",
    0x08: "FreeID",
    0x09: "FreeMem",
    0x0A: "Last",
    0x0B: "Mark",
    0x0C: "MaxMsgSize",
    0x0D: "Mem",
    0x0E: "MetInf",
    0x0F: "Next",
    0x10: "NextNonce",
    0x11: "SharedMem",
    0x12: "Size",
    0x13: "Type",
    0x14: "Version",
    0x15: "MaxObjSize",
    0x16: "FieldLevel",
}
TAG_PAGES: Final = (SYNCML_TAGS, METINF_TAGS)
TAG_TOKENS: Final = tuple({name: token for token, name in page.items()} for page in TAG_PAGES)


class WbxmlError(ValueError):
    """Unsupported, malformed or over-limit WBXML; never includes device data."""


def _utf8(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WbxmlError("WBXML text is not UTF-8") from exc
    if any(
        not (
            char in "\t\n\r"
            or 0x20 <= ord(char) <= 0xD7FF
            or 0xE000 <= ord(char) <= 0xFFFD
            or 0x10000 <= ord(char) <= 0x10FFFF
        )
        for char in text
    ):
        raise WbxmlError("WBXML text contains an invalid XML character")
    return text


def _table_string(table: bytes, offset: int) -> bytes:
    if offset >= len(table):
        raise WbxmlError("WBXML string table offset is out of range")
    end = table.find(b"\x00", offset)
    if end < 0:
        raise WbxmlError("unterminated WBXML string table entry")
    return table[offset:end]


def _mb_uint32(value: int) -> bytes:
    if not 0 <= value <= MAX_UINT32:
        raise WbxmlError("WBXML integer exceeds uint32")
    parts = [value & 0x7F]
    value >>= 7
    while value:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(parts))


class _Output:
    def __init__(self, limit: int) -> None:
        self.data = bytearray()
        self.limit = limit

    def write(self, data: bytes) -> None:
        # STR_T can repeat a large entry many times in a tiny input.
        if len(data) > self.limit - len(self.data):
            raise WbxmlError("WBXML output exceeds size limit")
        self.data.extend(data)


class _Decoder:
    def __init__(self, payload: bytes) -> None:
        if len(payload) > MAX_INPUT_BYTES:
            raise WbxmlError("WBXML input exceeds size limit")
        self.payload = payload
        self.offset = 0
        self.table = b""
        self.output = _Output(MAX_XML_BYTES)

    def _take(self, length: int) -> bytes:
        if length > len(self.payload) - self.offset:
            raise WbxmlError("truncated WBXML input")
        start = self.offset
        self.offset += length
        return self.payload[start : self.offset]

    def _byte(self) -> int:
        return self._take(1)[0]

    def _integer(self) -> int:
        value = 0
        for _ in range(MAX_MB_UINT32_BYTES):
            byte = self._byte()
            value = (value << 7) | (byte & 0x7F)
            if value > MAX_UINT32:
                raise WbxmlError("WBXML integer exceeds uint32")
            if not byte & 0x80:
                return value
        raise WbxmlError("WBXML integer exceeds five bytes")

    def _inline(self) -> bytes:
        end = self.payload.find(b"\x00", self.offset)
        if end < 0:
            raise WbxmlError("unterminated WBXML inline string")
        raw = self._take(end - self.offset)
        self._take(1)
        return raw

    def decode(self) -> bytes:
        if self._byte() not in (VERSION_12, VERSION_13):
            raise WbxmlError("unsupported WBXML version")
        public_id = self._integer()
        public_offset = self._integer() if public_id == 0 else None
        if self._integer() != CHARSET_UTF8:
            raise WbxmlError("unsupported WBXML charset")
        self.table = self._take(self._integer())
        if public_offset is not None:
            identifier = _utf8(_table_string(self.table, public_offset))
            public_id = next(
                (key for key, (public_name, _) in PUBLIC_IDS.items() if public_name == identifier),
                0,
            )
        if public_id not in PUBLIC_IDS:
            raise WbxmlError("unsupported WBXML public identifier")
        namespace = PUBLIC_IDS[public_id][1]
        page = 0
        stack: list[str] = []
        elements = 0
        while self.offset < len(self.payload):
            if elements and not stack:
                raise WbxmlError("trailing content after WBXML root")
            token = self._byte()
            if token == SWITCH_PAGE:
                page = self._byte()
                if page >= len(TAG_PAGES):
                    raise WbxmlError("unsupported WBXML code page")
            elif token == END:
                if not stack:
                    raise WbxmlError("unexpected WBXML END")
                self.output.write(f"</{stack.pop()}>".encode())
            elif token in (STR_I, STR_T, OPAQUE):
                if not stack:
                    raise WbxmlError("WBXML text outside the root")
                if token == STR_I:
                    raw = self._inline()
                elif token == STR_T:
                    raw = _table_string(self.table, self._integer())
                else:
                    raw = self._take(self._integer())
                self.output.write(escape(_utf8(raw), {"\r": "&#13;"}).encode())
            else:
                if token in (0x84, 0xC4) or (token & 0x3F >= 5 and token & ATTRIBUTES):
                    raise WbxmlError("WBXML attributes are not supported")
                if token & 0x3F < 5:
                    raise WbxmlError("unsupported WBXML global token")
                name = TAG_PAGES[page].get(token & 0x3F)
                if name is None:
                    raise WbxmlError("unknown WBXML tag token")
                if len(stack) >= MAX_DEPTH:
                    raise WbxmlError("WBXML nesting exceeds depth limit")
                elements += 1
                if elements > MAX_ELEMENTS:
                    raise WbxmlError("WBXML element count exceeds limit")
                qualified = name if page == 0 else f"metinf:{name}"
                declaration = ""
                if elements == 1:
                    if page != 0 or name != "SyncML":
                        raise WbxmlError("WBXML root must be SyncML")
                    declaration = f' xmlns="{namespace}" xmlns:metinf="{METINF_NS}"'
                suffix = ">" if token & CONTENT else "/>"
                self.output.write(f"<{qualified}{declaration}{suffix}".encode())
                if token & CONTENT:
                    stack.append(qualified)
        if stack:
            raise WbxmlError("missing WBXML END")
        if not elements:
            raise WbxmlError("WBXML root is missing")
        return bytes(self.output.data)


class _TreeBuilder(ElementTree.TreeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.elements = 0

    def start(self, tag: str, attrs: dict[str, str]) -> ElementTree.Element:
        self.depth += 1
        self.elements += 1
        if self.depth > MAX_ENCODE_DEPTH:
            raise WbxmlError("WBXML nesting exceeds depth limit")
        if self.elements > MAX_ENCODE_ELEMENTS:
            raise WbxmlError("WBXML element count exceeds limit")
        if attrs:
            raise WbxmlError("WBXML attributes are not supported")
        return super().start(tag, attrs)

    def end(self, tag: str) -> ElementTree.Element:
        self.depth -= 1
        return super().end(tag)

    def doctype(self, _name: str, _pubid: str | None, _system: str | None) -> None:
        # The encoder only needs XML elements, never DTD-defined entities.
        raise WbxmlError("XML document types are not supported")


def decode(payload: bytes) -> bytes:
    """Convert WBXML to UTF-8 XML for the existing hardened SyncML parser."""
    return _Decoder(payload).decode()


def encode(
    payload: bytes,
    *,
    version: int = VERSION_13,
    public_id: int | str | None = None,
    use_string_table: bool = False,
    opaque: bool = False,
) -> bytes:
    """Encode XML, optionally sharing repeated strings or using OPAQUE text."""
    if len(payload) > MAX_ENCODE_XML_BYTES:
        raise WbxmlError("XML input exceeds size limit")
    if version not in (VERSION_12, VERSION_13):
        raise WbxmlError("unsupported WBXML version")
    try:
        root = ElementTree.fromstring(payload, parser=ElementTree.XMLParser(target=_TreeBuilder()))
    except ElementTree.ParseError as exc:
        raise WbxmlError("malformed XML for WBXML encoding") from exc
    namespace, _, name = root.tag.rpartition("}")
    namespace = namespace.removeprefix("{")
    if name != "SyncML":
        raise WbxmlError("WBXML root must be SyncML")
    if public_id is None:
        public_id = next(
            (key for key, (_, ns) in PUBLIC_IDS.items() if ns == namespace), PUBLIC_ID_12
        )
    if isinstance(public_id, str):
        numeric_id = next(
            (key for key, (public_name, _) in PUBLIC_IDS.items() if public_name == public_id), 0
        )
    else:
        numeric_id = public_id
    if numeric_id not in PUBLIC_IDS:
        raise WbxmlError("unsupported WBXML public identifier")
    syncml_namespace = PUBLIC_IDS[numeric_id][1]
    if namespace and namespace != syncml_namespace:
        raise WbxmlError("XML namespace does not match WBXML public identifier")

    table = bytearray()
    offsets: dict[str, int] = {}
    if isinstance(public_id, str):
        offsets[public_id] = 0
        table.extend(public_id.encode() + b"\x00")
    if use_string_table and not opaque:
        counts = Counter(
            text for element in root.iter() for text in (element.text, element.tail) if text
        )
        for text, count in counts.items():
            if count > 1 and len(text.encode()) > 3 and text not in offsets:
                offsets[text] = len(table)
                table.extend(text.encode() + b"\x00")

    output = _Output(MAX_ENCODE_BYTES)
    output.write(bytes([version]))
    output.write(b"\x00\x00" if isinstance(public_id, str) else _mb_uint32(public_id))
    output.write(_mb_uint32(CHARSET_UTF8) + _mb_uint32(len(table)) + table)
    page = 0

    def text_content(text: str | None) -> None:
        if not text:
            return
        raw = text.encode()
        if opaque:
            output.write(bytes([OPAQUE]) + _mb_uint32(len(raw)) + raw)
        elif text in offsets:
            output.write(bytes([STR_T]) + _mb_uint32(offsets[text]))
        else:
            output.write(bytes([STR_I]) + raw + b"\x00")

    def element_content(element: ElementTree.Element) -> None:
        nonlocal page
        ns, _, local = element.tag.rpartition("}")
        ns = ns.removeprefix("{")
        if ns not in ("", syncml_namespace, METINF_NS):
            raise WbxmlError("unsupported XML namespace for WBXML")
        tag_page = 1 if ns == METINF_NS else 0
        token = TAG_TOKENS[tag_page].get(local)
        if token is None:
            raise WbxmlError("unknown XML tag for WBXML")
        if tag_page != page:
            output.write(bytes([SWITCH_PAGE, tag_page]))
            page = tag_page
        has_content = bool(element.text) or len(element) > 0
        output.write(bytes([token | CONTENT if has_content else token]))
        if has_content:
            text_content(element.text)
            for child in element:
                element_content(child)
                text_content(child.tail)
            output.write(bytes([END]))

    element_content(root)
    return bytes(output.data)
