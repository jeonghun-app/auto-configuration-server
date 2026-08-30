"""OMA-DM Management Object tree.

A DM server manages a device by reading and writing nodes in a tree addressed by
URI (``./DevInfo/DevId``, ``./3GPP_IMS/1/PCSCF`` ...). Which nodes exist is
defined by Management Objects, each identified by a URN.

Like the OMA-CP catalogue, the MO definitions are **declarative YAML** rather
than code, for the same reasons: coverage is countable, adding a new MO for a new
service is a data change, and every node can carry a specification reference and
a ``verified`` flag.

Adding VoLTE, a vendor MO or a firmware-update MO therefore means dropping a file
into ``src/acs/catalog/omadm/``; no code change is required.

Node ``source`` values:

``device``
    The device owns the value. The server issues ``Get`` and records the result
    (device inventory: manufacturer, model, firmware).
``server``
    The server owns the value. It issues ``Replace``/``Add`` to push it
    (IMS/VoLTE/RCS configuration).
"""

from __future__ import annotations

import dataclasses
import functools
import pathlib
import re
import string
from collections.abc import Iterable
from typing import Any, Final, Literal

import yaml

from acs.errors import CatalogError

CATALOG_ROOT: Final = pathlib.Path(__file__).resolve().parents[2] / "catalog" / "omadm"

NodeFormat = Literal["chr", "int", "bool", "b64", "node", "xml"]
NodeSource = Literal["device", "server"]

_VALID_FORMATS: Final[frozenset[str]] = frozenset({"chr", "int", "bool", "b64", "node", "xml"})
_URI_RE: Final = re.compile(r"^\./[A-Za-z0-9_\-./]*$")


class _SafeFormatter(string.Formatter):
    def get_value(  # noqa: ARG002
        self,
        key: object,
        args: object,  # noqa: ARG002 - fixed stdlib signature
        kwargs: object,
    ) -> object:
        if isinstance(key, str) and isinstance(kwargs, dict):
            return kwargs.get(key, "{" + key + "}")
        return ""  # pragma: no cover


_FORMATTER = _SafeFormatter()


@dataclasses.dataclass(frozen=True, slots=True)
class MoNode:
    """One node of a management object."""

    uri: str
    format: NodeFormat = "chr"
    type: str = "text/plain"
    source: NodeSource = "server"
    default: str = ""
    access: tuple[str, ...] = ("Get",)
    spec: str = ""
    verified: bool = False
    doc: str = ""
    feature: str = ""
    """Optional feature gate, e.g. ``volte`` or ``rcs``."""

    @property
    def parent(self) -> str:
        return self.uri.rsplit("/", 1)[0] or "."

    @property
    def name(self) -> str:
        return self.uri.rsplit("/", 1)[-1]

    @property
    def is_interior(self) -> bool:
        return self.format == "node"


@dataclasses.dataclass(frozen=True, slots=True)
class ManagementObject:
    """A management object: a URN, a root URI and its nodes."""

    id: str
    urn: str
    root: str
    title: str
    nodes: tuple[MoNode, ...]
    spec: str = ""

    def node_count(self) -> int:
        return len(self.nodes)


@dataclasses.dataclass(frozen=True)
class MoTree:
    """The registry of all loaded management objects.

    Not ``slots=True``: :func:`functools.cached_property` needs an instance
    ``__dict__``.
    """

    objects: tuple[ManagementObject, ...]

    @functools.cached_property
    def _by_uri(self) -> dict[str, MoNode]:
        return {node.uri: node for mo in self.objects for node in mo.nodes}

    def all_nodes(self) -> tuple[MoNode, ...]:
        return tuple(node for mo in self.objects for node in mo.nodes)

    def node(self, uri: str) -> MoNode | None:
        return self._by_uri.get(uri.rstrip("/"))

    def object_for(self, uri: str) -> ManagementObject | None:
        for mo in self.objects:
            if uri == mo.root or uri.startswith(mo.root + "/"):
                return mo
        return None

    def children(self, uri: str) -> list[MoNode]:
        prefix = uri.rstrip("/") + "/"
        return [
            node
            for node in self.all_nodes()
            if node.uri.startswith(prefix) and "/" not in node.uri[len(prefix) :]
        ]

    def device_query_uris(self, limit: int = 0) -> list[str]:
        """Nodes the server should ``Get`` to build a device inventory."""
        uris = [n.uri for n in self.all_nodes() if n.source == "device" and not n.is_interior]
        return uris[:limit] if limit else uris

    def server_nodes(self, features: Iterable[str] = ()) -> list[MoNode]:
        """Nodes the server pushes, filtered by enabled features."""
        enabled = set(features)
        return [
            node
            for node in self.all_nodes()
            if node.source == "server"
            and not node.is_interior
            and (not node.feature or node.feature in enabled)
        ]

    def render(self, node: MoNode, context: dict[str, str], overrides: dict[str, str]) -> str:
        raw = overrides.get(node.uri, node.default)
        if "{" not in raw:
            return raw
        return _FORMATTER.vformat(raw, (), context)

    @property
    def urns(self) -> list[str]:
        return [mo.urn for mo in self.objects]

    @property
    def verified_count(self) -> int:
        return sum(1 for node in self.all_nodes() if node.verified)


def _coerce_node(raw: dict[str, Any], source_file: str, root: str) -> MoNode:
    try:
        uri = str(raw["uri"]).strip()
    except KeyError as exc:
        raise CatalogError(f"{source_file}: node missing 'uri'") from exc
    if not _URI_RE.match(uri):
        raise CatalogError(f"{source_file}: invalid node URI {uri!r}")
    if not (uri == root or uri.startswith(root.rstrip("/") + "/")):
        raise CatalogError(f"{source_file}: node {uri} is outside MO root {root}")

    fmt = str(raw.get("format", "chr"))
    if fmt not in _VALID_FORMATS:
        raise CatalogError(f"{source_file}: unknown format {fmt!r} for {uri}")

    node_source = str(raw.get("source", "server"))
    if node_source not in ("device", "server"):
        raise CatalogError(f"{source_file}: unknown source {node_source!r} for {uri}")

    default = raw.get("default", "")
    default = "" if default is None else str(default)
    if fmt == "int" and default and "{" not in default:
        try:
            int(default)
        except ValueError as exc:
            raise CatalogError(f"{source_file}: int node {uri} default is not an integer") from exc
    if fmt == "bool" and default and default not in ("true", "false"):
        raise CatalogError(f"{source_file}: bool node {uri} default must be 'true' or 'false'")

    return MoNode(
        uri=uri,
        format=fmt,  # type: ignore[arg-type]
        type=str(raw.get("type", "node" if fmt == "node" else "text/plain")),
        source=node_source,  # type: ignore[arg-type]
        default=default,
        access=tuple(str(a) for a in raw.get("access", ("Get",))),
        spec=str(raw.get("spec", "")),
        verified=bool(raw.get("verified", False)),
        doc=str(raw.get("doc", "")),
        feature=str(raw.get("feature", "")),
    )


def _load_object(path: pathlib.Path) -> ManagementObject:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CatalogError(f"{path.name}: top level must be a mapping")
    meta = data.get("meta") or {}
    for key in ("id", "urn", "root"):
        if not meta.get(key):
            raise CatalogError(f"{path.name}: meta.{key} is required")
    root = str(meta["root"]).rstrip("/")
    raw_nodes = data.get("nodes") or []
    if not raw_nodes:
        raise CatalogError(f"{path.name}: declares no nodes")

    nodes: list[MoNode] = []
    seen: set[str] = set()
    for raw in raw_nodes:
        node = _coerce_node(raw, path.name, root)
        if node.uri in seen:
            raise CatalogError(f"{path.name}: duplicate node {node.uri}")
        seen.add(node.uri)
        nodes.append(node)

    return ManagementObject(
        id=str(meta["id"]),
        urn=str(meta["urn"]),
        root=root,
        title=str(meta.get("title", meta["id"])),
        spec=str(meta.get("spec", "")),
        nodes=tuple(nodes),
    )


def load_tree(root: pathlib.Path | None = None) -> MoTree:
    """Load every management object definition, sorted by file name."""
    base = root or CATALOG_ROOT
    if not base.is_dir():
        raise CatalogError(f"OMA-DM catalogue directory not found: {base}")
    files = sorted(base.glob("*.yaml"))
    if not files:
        raise CatalogError(f"no management objects declared in {base}")
    objects = [_load_object(path) for path in files]

    roots = [mo.root for mo in objects]
    if len(set(roots)) != len(roots):
        raise CatalogError("two management objects declare the same root URI")
    return MoTree(objects=tuple(objects))


@functools.lru_cache(maxsize=1)
def get_tree() -> MoTree:
    """Cached tree accessor; loaded once at first use and validated then."""
    return load_tree()


def build_context(
    imsi: str,
    msisdn: str,
    device_id: str,
    ims_domain: str,
    impi: str,
    impu: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    context = {
        "imsi": imsi,
        "msisdn": msisdn,
        "msisdn_national": msisdn.lstrip("+"),
        "device_id": device_id,
        "ims_domain": ims_domain,
        "impi": impi,
        "impu": impu,
    }
    if extra:
        context.update(extra)
    return context
