"""Declarative provisioning parameter catalogue.

The RCS configuration surface is roughly 150 named parameters spread over a
nested characteristic tree. Hard-coding them in Python would make specification
coverage unauditable and every correction a code change. Instead every parameter
is declared once in YAML with its characteristic path, type, default, spec
reference and a ``verified`` flag:

.. code-block:: yaml

    - path: APPLICATION:ap2002/MESSAGING/FT
      parm: MaxSizeFileTr
      type: int
      unit: KB
      default: "10240"
      spec: RCC.07 A.1.4 FT
      verified: false

``docs/spec-coverage.md`` is generated from the same file, so the repository can
state honestly how many parameters have been cross-checked against the pinned
specification edition instead of making an unprovable compliance claim.

Profile overlays (``catalog/omacp/profiles/*.yaml``) add, override or remove
entries for a specific ``rcs_profile`` value.
"""

from __future__ import annotations

import dataclasses
import functools
import pathlib
import re
from typing import Any, Final, Literal

import yaml

from acs.errors import CatalogError

CATALOG_ROOT: Final = pathlib.Path(__file__).resolve().parents[2] / "catalog" / "omacp"

ParmType = Literal["chr", "int", "bool01", "enum"]

_VALID_TYPES: Final[frozenset[str]] = frozenset({"chr", "int", "bool01", "enum"})
_PATH_RE: Final = re.compile(r"^[A-Za-z0-9_:\-.]+(?:/[A-Za-z0-9_:\-.]+)*$")
_PLACEHOLDER_RE: Final = re.compile(r"\{([a-z_]+)\}")


@dataclasses.dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One provisioning parameter declaration."""

    path: str
    """Slash-separated characteristic path, e.g. ``APPLICATION:ap2002/SERVICES``."""
    parm: str
    """Parameter name, spelled exactly as the specification spells it."""
    type: ParmType = "chr"
    default: str = ""
    unit: str = ""
    values: tuple[str, ...] = ()
    spec: str = ""
    verified: bool = False
    required: bool = False
    profiles: tuple[str, ...] = ()
    """Restrict the entry to these ``rcs_profile`` values (empty = all)."""
    doc: str = ""

    @property
    def key(self) -> str:
        return f"{self.path}/{self.parm}"

    @property
    def app_id(self) -> str | None:
        head = self.path.split("/", 1)[0]
        if head.startswith("APPLICATION:"):
            return head.split(":", 1)[1]
        return None

    def applies_to(self, profile: str) -> bool:
        return not self.profiles or profile in self.profiles

    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER_RE.findall(self.default))


@dataclasses.dataclass(frozen=True, slots=True)
class Catalog:
    """A loaded, validated catalogue."""

    entries: tuple[CatalogEntry, ...]
    meta: dict[str, Any]

    def for_profile(self, profile: str) -> tuple[CatalogEntry, ...]:
        return tuple(e for e in self.entries if e.applies_to(profile))

    def by_key(self) -> dict[str, CatalogEntry]:
        return {e.key: e for e in self.entries}

    @property
    def verified_count(self) -> int:
        return sum(1 for e in self.entries if e.verified)

    def app_ids(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries:
            app_id = entry.app_id
            if app_id and app_id not in seen:
                seen.append(app_id)
        return seen


def _coerce_entry(raw: dict[str, Any], source: str) -> CatalogEntry:
    try:
        path = str(raw["path"]).strip()
        parm = str(raw["parm"]).strip()
    except KeyError as exc:
        raise CatalogError(f"{source}: entry missing required key {exc}") from exc

    if not _PATH_RE.match(path):
        raise CatalogError(f"{source}: invalid characteristic path {path!r}")
    if not parm:
        raise CatalogError(f"{source}: empty parm name at {path}")

    parm_type = str(raw.get("type", "chr"))
    if parm_type not in _VALID_TYPES:
        raise CatalogError(f"{source}: unknown type {parm_type!r} for {path}/{parm}")

    default = raw.get("default", "")
    default = "" if default is None else str(default)

    values = tuple(str(v) for v in raw.get("values", ()) or ())
    if parm_type == "enum" and not values:
        raise CatalogError(f"{source}: enum {path}/{parm} declares no values")
    if parm_type == "bool01" and default and default not in ("0", "1"):
        raise CatalogError(f"{source}: bool01 {path}/{parm} default must be 0 or 1")
    if parm_type == "int" and default and not _PLACEHOLDER_RE.search(default):
        try:
            int(default)
        except ValueError as exc:
            raise CatalogError(f"{source}: int {path}/{parm} default is not an integer") from exc

    return CatalogEntry(
        path=path,
        parm=parm,
        type=parm_type,  # type: ignore[arg-type]
        default=default,
        unit=str(raw.get("unit", "")),
        values=values,
        spec=str(raw.get("spec", "")),
        verified=bool(raw.get("verified", False)),
        required=bool(raw.get("required", False)),
        profiles=tuple(str(p) for p in raw.get("profiles", ()) or ()),
        doc=str(raw.get("doc", "")),
    )


def _load_yaml(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        raise CatalogError(f"catalogue file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CatalogError(f"{path.name}: top level must be a mapping")
    return data


def load_catalog(profile: str = "", root: pathlib.Path | None = None) -> Catalog:
    """Load ``base.yaml`` and, if present, the overlay for ``profile``.

    Overlay semantics, keyed on ``path``/``parm``:

    * an entry not present in the base is **added**;
    * an entry already present is **replaced**;
    * ``remove: true`` **deletes** the base entry.
    """
    base_dir = root or CATALOG_ROOT
    base = _load_yaml(base_dir / "base.yaml")
    meta = dict(base.get("meta") or {})
    raw_entries = base.get("entries") or []
    if not isinstance(raw_entries, list) or not raw_entries:
        raise CatalogError("base.yaml declares no entries")

    merged: dict[str, CatalogEntry] = {}
    order: list[str] = []
    for raw in raw_entries:
        entry = _coerce_entry(raw, "base.yaml")
        if entry.key in merged:
            raise CatalogError(f"base.yaml: duplicate entry {entry.key}")
        merged[entry.key] = entry
        order.append(entry.key)

    if profile:
        overlay_path = base_dir / "profiles" / f"{profile}.yaml"
        if overlay_path.is_file():
            overlay = _load_yaml(overlay_path)
            meta.setdefault("profiles", [])
            meta["profile"] = profile
            meta["profile_meta"] = overlay.get("meta") or {}
            for raw in overlay.get("entries") or []:
                key = f"{raw.get('path')}/{raw.get('parm')}"
                if raw.get("remove"):
                    merged.pop(key, None)
                    if key in order:
                        order.remove(key)
                    continue
                entry = _coerce_entry(raw, overlay_path.name)
                if entry.key not in merged:
                    order.append(entry.key)
                merged[entry.key] = entry

    return Catalog(entries=tuple(merged[k] for k in order), meta=meta)


@functools.lru_cache(maxsize=16)
def get_catalog(profile: str = "") -> Catalog:
    """Cached catalogue accessor. Loaded once per profile at first use."""
    return load_catalog(profile)


def available_profiles(root: pathlib.Path | None = None) -> list[str]:
    base_dir = (root or CATALOG_ROOT) / "profiles"
    if not base_dir.is_dir():
        return []
    return sorted(p.stem for p in base_dir.glob("*.yaml"))
