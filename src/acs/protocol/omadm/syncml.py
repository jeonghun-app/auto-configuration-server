"""SyncML Device Management 1.2 message parsing and generation.

OMA-DM carries management commands inside SyncML packages. One DM *session*
spans several HTTP POST round trips:

===========  ==========================================================
Package 1    client -> server: ``Alert`` 1200/1201 plus ``Replace`` with
             ``./DevInfo`` (device identity, manufacturer, model)
Package 2    server -> client: ``Status`` for everything received, then the
             server's commands (``Get``, ``Replace``, ``Add``, ``Exec``)
Package 3    client -> server: ``Status`` / ``Results`` for those commands
Package 4    server -> client: more commands, or ``Final`` with nothing left
             to do, which ends the session
===========  ==========================================================

Parsing is namespace-tolerant on purpose: real DM clients disagree about whether
``syncml:metinf`` elements carry a prefix, and rejecting a handset over a
namespace declaration would be a self-inflicted interoperability failure.

WBXML (the binary SyncML encoding, ``application/vnd.syncml.dm+wbxml``) is not
implemented; see :data:`SUPPORTED_CONTENT_TYPES`.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Iterable
from typing import Final

from lxml import etree

VER_DTD: Final = "1.2"
VER_PROTO: Final = "DM/1.2"
SYNCML_NS: Final = "SYNCML:SYNCML1.2"
METINF_NS: Final = "syncml:metinf"

CONTENT_TYPE_XML: Final = "application/vnd.syncml.dm+xml"
CONTENT_TYPE_WBXML: Final = "application/vnd.syncml.dm+wbxml"
SUPPORTED_CONTENT_TYPES: Final[tuple[str, ...]] = (CONTENT_TYPE_XML, "text/xml", "application/xml")

# ---- OMA-DM alert codes ---------------------------------------------------
ALERT_SERVER_INITIATED_MGMT: Final = "1200"
ALERT_CLIENT_INITIATED_MGMT: Final = "1201"
ALERT_NEXT_MESSAGE: Final = "1222"
ALERT_SESSION_ABORT: Final = "1223"
ALERT_END_OF_SESSION: Final = "1226"
ALERT_GENERIC: Final = "1226"

# ---- OMA-DM status codes -------------------------------------------------
STATUS_OK: Final = "200"
STATUS_ITEM_ADDED: Final = "201"
STATUS_ACCEPTED_FOR_PROCESSING: Final = "202"
STATUS_AUTH_ACCEPTED: Final = "212"
STATUS_CHUNKED_ITEM_ACCEPTED: Final = "213"
STATUS_OPERATION_CANCELLED: Final = "214"
STATUS_NOT_EXECUTED: Final = "215"
STATUS_INVALID_CREDENTIALS: Final = "401"
STATUS_FORBIDDEN: Final = "403"
STATUS_NOT_FOUND: Final = "404"
STATUS_OPTIONAL_FEATURE_NOT_SUPPORTED: Final = "406"
STATUS_MISSING_CREDENTIALS: Final = "407"
STATUS_UNSUPPORTED_MEDIA_TYPE: Final = "415"
STATUS_ALREADY_EXISTS: Final = "418"
STATUS_PERMISSION_DENIED: Final = "425"
STATUS_COMMAND_FAILED: Final = "500"

AUTH_BASIC: Final = "syncml:auth-basic"
AUTH_MD5: Final = "syncml:auth-md5"


def _local(tag: object) -> str:
    """Return the local name of an element tag, ignoring any namespace."""
    if not isinstance(tag, str):
        return ""
    return str(tag).rsplit("}", 1)[-1]


def _text(element: etree._Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return str(element.text).strip()


def _find(parent: etree._Element, name: str) -> etree._Element | None:
    for child in parent:
        if _local(child.tag) == name:
            return child
    return None


def _find_all(parent: etree._Element, name: str) -> list[etree._Element]:
    return [child for child in parent if _local(child.tag) == name]


def _deep_text(parent: etree._Element, name: str) -> str:
    for element in parent.iter():
        if _local(element.tag) == name:
            return _text(element)
    return ""


# ---------------------------------------------------------------- data model
@dataclasses.dataclass(slots=True)
class Credentials:
    """A ``<Cred>`` element."""

    type: str = ""
    format: str = "b64"
    data: str = ""

    def decode_basic(self) -> tuple[str, str] | None:
        """Return ``(username, password)`` for ``syncml:auth-basic``."""
        if self.type != AUTH_BASIC or not self.data:
            return None
        try:
            raw = base64.b64decode(self.data + "==", validate=False).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return None
        username, sep, password = raw.partition(":")
        if not sep:
            return None
        return username, password


@dataclasses.dataclass(slots=True)
class Item:
    """A command ``<Item>``."""

    target: str = ""
    source: str = ""
    data: str = ""
    format: str = ""
    type: str = ""

    @property
    def uri(self) -> str:
        return self.target or self.source


@dataclasses.dataclass(slots=True)
class Command:
    """A DM command or status."""

    name: str
    cmd_id: int = 0
    items: list[Item] = dataclasses.field(default_factory=list)
    data: str = ""
    cmd_ref: str = ""
    msg_ref: str = ""
    cmd: str = ""
    """For ``Status``/``Results``: the command being reported on."""

    @property
    def first_uri(self) -> str:
        return self.items[0].uri if self.items else ""


@dataclasses.dataclass(slots=True)
class SyncHdr:
    session_id: str = ""
    msg_id: str = ""
    target: str = ""
    source: str = ""
    source_name: str = ""
    max_msg_size: int = 0
    credentials: Credentials | None = None
    ver_dtd: str = VER_DTD
    ver_proto: str = VER_PROTO


@dataclasses.dataclass(slots=True)
class SyncMlMessage:
    header: SyncHdr
    commands: list[Command] = dataclasses.field(default_factory=list)
    final: bool = False

    def of(self, *names: str) -> list[Command]:
        wanted = set(names)
        return [c for c in self.commands if c.name in wanted]

    def alert_codes(self) -> list[str]:
        return [c.data for c in self.of("Alert") if c.data]

    def has_alert(self, code: str) -> bool:
        return code in self.alert_codes()


class SyncMlParseError(ValueError):
    """The payload is not a usable SyncML DM message."""


# -------------------------------------------------------------------- parser
def parse(payload: bytes) -> SyncMlMessage:
    """Parse a SyncML DM package.

    External entities and network access are disabled: a DM payload arrives from
    an untrusted device, so XXE protection is mandatory.
    """
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        recover=False,
    )
    try:
        root = etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SyncMlParseError(f"malformed SyncML: {exc}") from exc

    if _local(root.tag) != "SyncML":
        raise SyncMlParseError(f"root element must be SyncML, got {_local(root.tag)!r}")

    hdr_element = _find(root, "SyncHdr")
    if hdr_element is None:
        raise SyncMlParseError("SyncHdr is missing")
    body_element = _find(root, "SyncBody")
    if body_element is None:
        raise SyncMlParseError("SyncBody is missing")

    header = _parse_header(hdr_element)
    commands: list[Command] = []
    final = False
    for child in body_element:
        name = _local(child.tag)
        if name == "Final":
            final = True
            continue
        commands.append(_parse_command(name, child))
    return SyncMlMessage(header=header, commands=commands, final=final)


def _parse_header(element: etree._Element) -> SyncHdr:
    target = _find(element, "Target")
    source = _find(element, "Source")
    meta = _find(element, "Meta")
    cred_element = _find(element, "Cred")

    credentials: Credentials | None = None
    if cred_element is not None:
        credentials = Credentials(
            type=_deep_text(cred_element, "Type"),
            format=_deep_text(cred_element, "Format") or "b64",
            data=_deep_text(cred_element, "Data"),
        )

    max_msg_size = 0
    if meta is not None:
        raw = _deep_text(meta, "MaxMsgSize")
        if raw.isdigit():
            max_msg_size = int(raw)

    return SyncHdr(
        session_id=_text(_find(element, "SessionID")),
        msg_id=_text(_find(element, "MsgID")),
        target=_deep_text(target, "LocURI") if target is not None else "",
        source=_deep_text(source, "LocURI") if source is not None else "",
        source_name=_deep_text(source, "LocName") if source is not None else "",
        max_msg_size=max_msg_size,
        credentials=credentials,
        ver_dtd=_text(_find(element, "VerDTD")) or VER_DTD,
        ver_proto=_text(_find(element, "VerProto")) or VER_PROTO,
    )


def _parse_command(name: str, element: etree._Element) -> Command:
    cmd_id_raw = _text(_find(element, "CmdID"))
    command = Command(
        name=name,
        cmd_id=int(cmd_id_raw) if cmd_id_raw.isdigit() else 0,
        cmd_ref=_text(_find(element, "CmdRef")),
        msg_ref=_text(_find(element, "MsgRef")),
        cmd=_text(_find(element, "Cmd")),
    )
    direct_data = _find(element, "Data")
    if direct_data is not None:
        command.data = _text(direct_data)
    for item_element in _find_all(element, "Item"):
        command.items.append(_parse_item(item_element))
    return command


def _parse_item(element: etree._Element) -> Item:
    target = _find(element, "Target")
    source = _find(element, "Source")
    meta = _find(element, "Meta")
    return Item(
        target=_deep_text(target, "LocURI") if target is not None else "",
        source=_deep_text(source, "LocURI") if source is not None else "",
        data=_text(_find(element, "Data")),
        format=_deep_text(meta, "Format") if meta is not None else "",
        type=_deep_text(meta, "Type") if meta is not None else "",
    )


# ----------------------------------------------------------------- generator
class SyncMlBuilder:
    """Build a server-side SyncML DM package."""

    def __init__(
        self,
        session_id: str,
        msg_id: int,
        target: str,
        source: str,
        max_msg_size: int = 16384,
    ) -> None:
        self._session_id = session_id
        self._msg_id = msg_id
        self._target = target
        self._source = source
        self._max_msg_size = max_msg_size
        self._commands: list[etree._Element] = []
        self._next_cmd_id = 1
        self._challenge: tuple[str, str] | None = None

    # -- helpers
    def _cmd_id(self) -> str:
        value = str(self._next_cmd_id)
        self._next_cmd_id += 1
        return value

    @staticmethod
    def _sub(parent: etree._Element, tag: str, text: str | None = None) -> etree._Element:
        element = etree.SubElement(parent, tag)
        if text is not None:
            element.text = text
        return element

    def _meta(self, parent: etree._Element, fmt: str = "", type_: str = "") -> None:
        if not fmt and not type_:
            return
        meta = self._sub(parent, "Meta")
        if fmt:
            element = etree.SubElement(meta, f"{{{METINF_NS}}}Format")
            element.text = fmt
        if type_:
            element = etree.SubElement(meta, f"{{{METINF_NS}}}Type")
            element.text = type_

    # -- commands
    def status(
        self,
        cmd: str,
        msg_ref: str,
        cmd_ref: str,
        code: str,
        target_ref: str = "",
        challenge: tuple[str, str] | None = None,
    ) -> SyncMlBuilder:
        element = etree.Element("Status")
        self._sub(element, "CmdID", self._cmd_id())
        self._sub(element, "MsgRef", msg_ref or "1")
        self._sub(element, "CmdRef", cmd_ref or "0")
        self._sub(element, "Cmd", cmd)
        if target_ref:
            self._sub(element, "TargetRef", target_ref)
        if challenge is not None:
            auth_type, nonce = challenge
            chal = self._sub(element, "Chal")
            meta = self._sub(chal, "Meta")
            etree.SubElement(meta, f"{{{METINF_NS}}}Format").text = "b64"
            etree.SubElement(meta, f"{{{METINF_NS}}}Type").text = auth_type
            if nonce:
                etree.SubElement(meta, f"{{{METINF_NS}}}NextNonce").text = nonce
        self._sub(element, "Data", code)
        self._commands.append(element)
        return self

    def get(self, uris: Iterable[str]) -> SyncMlBuilder:
        uri_list = list(uris)
        if not uri_list:
            return self
        element = etree.Element("Get")
        self._sub(element, "CmdID", self._cmd_id())
        for uri in uri_list:
            item = self._sub(element, "Item")
            target = self._sub(item, "Target")
            self._sub(target, "LocURI", uri)
        self._commands.append(element)
        return self

    def replace(self, values: Iterable[tuple[str, str, str, str]]) -> SyncMlBuilder:
        """Add a ``Replace`` command. Values are ``(uri, data, format, type)``."""
        value_list = list(values)
        if not value_list:
            return self
        element = etree.Element("Replace")
        self._sub(element, "CmdID", self._cmd_id())
        for uri, data, fmt, type_ in value_list:
            item = self._sub(element, "Item")
            target = self._sub(item, "Target")
            self._sub(target, "LocURI", uri)
            self._meta(item, fmt, type_)
            self._sub(item, "Data", data)
        self._commands.append(element)
        return self

    def add(self, values: Iterable[tuple[str, str, str, str]]) -> SyncMlBuilder:
        value_list = list(values)
        if not value_list:
            return self
        element = etree.Element("Add")
        self._sub(element, "CmdID", self._cmd_id())
        for uri, data, fmt, type_ in value_list:
            item = self._sub(element, "Item")
            target = self._sub(item, "Target")
            self._sub(target, "LocURI", uri)
            self._meta(item, fmt, type_ or ("node" if fmt == "node" else "text/plain"))
            if fmt != "node":
                self._sub(item, "Data", data)
        self._commands.append(element)
        return self

    def exec_(self, uri: str, data: str = "") -> SyncMlBuilder:
        element = etree.Element("Exec")
        self._sub(element, "CmdID", self._cmd_id())
        item = self._sub(element, "Item")
        target = self._sub(item, "Target")
        self._sub(target, "LocURI", uri)
        if data:
            self._sub(item, "Data", data)
        self._commands.append(element)
        return self

    def alert(self, code: str) -> SyncMlBuilder:
        element = etree.Element("Alert")
        self._sub(element, "CmdID", self._cmd_id())
        self._sub(element, "Data", code)
        self._commands.append(element)
        return self

    @property
    def command_count(self) -> int:
        return sum(1 for c in self._commands if c.tag != "Status")

    # -- output
    def build(self, final: bool = True) -> bytes:
        root = etree.Element("SyncML", nsmap={None: SYNCML_NS})
        header = self._sub(root, "SyncHdr")
        self._sub(header, "VerDTD", VER_DTD)
        self._sub(header, "VerProto", VER_PROTO)
        self._sub(header, "SessionID", self._session_id)
        self._sub(header, "MsgID", str(self._msg_id))
        target = self._sub(header, "Target")
        self._sub(target, "LocURI", self._target)
        source = self._sub(header, "Source")
        self._sub(source, "LocURI", self._source)
        meta = self._sub(header, "Meta")
        etree.SubElement(meta, f"{{{METINF_NS}}}MaxMsgSize").text = str(self._max_msg_size)

        body = self._sub(root, "SyncBody")
        for command in self._commands:
            body.append(command)
        if final:
            self._sub(body, "Final")

        payload: bytes = etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", pretty_print=True
        )
        return payload
