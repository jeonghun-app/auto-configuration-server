"""Conformance requirement registry.

Answers one question, verifiably: *which requirements of the OMA-DM and RCC.14
specifications does this server accommodate, and which does it not?*

The registry is data (``src/acs/catalog/conformance/*.yaml``) for the same reason
the parameter catalogues are: a claim you cannot count is a claim you cannot
audit. Each requirement carries four independent axes, deliberately kept apart:

``level``
    Is the requirement mandatory, optional or conditional **for a server**?
``level_verified``
    Was that classification read out of the licensed specification, or is it this
    project's engineering judgement? It is ``false`` everywhere today, because
    nobody here holds RCC.14 or the OMA DM conformance requirement tables.
``status``
    Is the behaviour implemented, partial, not implemented, or not applicable?
``verification``
    *How* do we know — a passing test, a code review, or nothing stronger than a
    public description of the specification?

Keeping ``status`` and ``verification`` apart is what stops "we implement the
message exchange" from being read as "we are certified". Nothing in this
repository is certified, and :func:`Requirement.claim_wording` will not let a row
say otherwise.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import pathlib
import re
from typing import Any, Final, Literal

import yaml

from acs.errors import CatalogError

CONFORMANCE_ROOT: Final = pathlib.Path(__file__).resolve().parent / "catalog" / "conformance"
REPO_ROOT: Final = pathlib.Path(__file__).resolve().parents[2]

Level = Literal["mandatory", "optional", "conditional"]
Status = Literal["implemented", "partial", "not-implemented", "not-applicable"]
Verification = Literal[
    "behaviour-tested",
    "code-review-only",
    "public-description-only",
    "interop-tested",
    "not-verified",
]

_LEVELS: Final[frozenset[str]] = frozenset({"mandatory", "optional", "conditional"})
_STATUSES: Final[frozenset[str]] = frozenset(
    {"implemented", "partial", "not-implemented", "not-applicable"}
)
_VERIFICATIONS: Final[frozenset[str]] = frozenset(
    {
        "behaviour-tested",
        "code-review-only",
        "public-description-only",
        "interop-tested",
        "not-verified",
    }
)
_ID_RE: Final = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$")

#: Words that would turn an honest status into a compliance claim.
FORBIDDEN_CLAIM_WORDS: Final[tuple[str, ...]] = (
    "certified",
    "certification",
    "fully compliant",
    "fully conformant",
    "guaranteed",
    "gsma approved",
)


@dataclasses.dataclass(frozen=True, slots=True)
class Requirement:
    """One specification requirement and the evidence for its status."""

    id: str
    title: str
    spec: str
    level: Level
    status: Status
    verification: Verification
    level_verified: bool = False
    implemented_by: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    gap: str = ""
    impact: str = ""
    note: str = ""
    family: str = ""

    @property
    def is_gap(self) -> bool:
        return self.status in ("partial", "not-implemented")

    @property
    def blocks_conformance(self) -> bool:
        """A mandatory requirement that is not fully implemented."""
        return self.level == "mandatory" and self.status in ("partial", "not-implemented")

    def claim_wording(self) -> str:
        """The only sentence this project is entitled to write about the row."""
        if self.status == "implemented":
            return (
                f"Implemented; {self.verification.replace('-', ' ')}. "
                "No certification is claimed."
            )
        if self.status == "partial":
            return f"Partially implemented. {self.gap}"
        if self.status == "not-applicable":
            return f"Not applicable to this server. {self.note or self.gap}".strip()
        return f"Not implemented. {self.gap}"


@dataclasses.dataclass(frozen=True, slots=True)
class RequirementSet:
    """All requirements declared by one specification family."""

    id: str
    title: str
    spec: str
    role: str
    spec_edition_pinned: bool
    requirements: tuple[Requirement, ...]

    def counts(self) -> dict[str, int]:
        out = {status: 0 for status in sorted(_STATUSES)}
        for requirement in self.requirements:
            out[requirement.status] += 1
        return out

    @property
    def mandatory_gaps(self) -> tuple[Requirement, ...]:
        return tuple(r for r in self.requirements if r.blocks_conformance)


def _require(raw: dict[str, Any], key: str, source: str) -> Any:
    if key not in raw or raw[key] in (None, ""):
        raise CatalogError(f"{source}: requirement {raw.get('id', '?')} is missing '{key}'")
    return raw[key]


def _coerce(raw: dict[str, Any], source: str, family: str) -> Requirement:
    requirement_id = str(_require(raw, "id", source)).strip()
    if not _ID_RE.match(requirement_id):
        raise CatalogError(f"{source}: malformed requirement id {requirement_id!r}")

    level = str(_require(raw, "level", source))
    if level not in _LEVELS:
        raise CatalogError(f"{source}: {requirement_id} has unknown level {level!r}")

    status = str(_require(raw, "status", source))
    if status not in _STATUSES:
        raise CatalogError(f"{source}: {requirement_id} has unknown status {status!r}")

    verification = str(_require(raw, "verification", source))
    if verification not in _VERIFICATIONS:
        raise CatalogError(f"{source}: {requirement_id} has unknown verification {verification!r}")

    tests = tuple(str(t) for t in raw.get("tests", ()) or ())
    implemented_by = tuple(str(s) for s in raw.get("implemented_by", ()) or ())
    gap = str(raw.get("gap", "") or "")
    impact = str(raw.get("impact", "") or "")

    # The rules that stop a row from being a free pass.
    if status in ("implemented", "partial") and not tests:
        raise CatalogError(
            f"{source}: {requirement_id} is {status} but names no test. "
            "A status without evidence is an opinion."
        )
    if status in ("implemented", "partial") and not implemented_by:
        raise CatalogError(f"{source}: {requirement_id} is {status} but names no source symbol")
    if status in ("partial", "not-implemented") and not gap:
        raise CatalogError(f"{source}: {requirement_id} is {status} but describes no gap")
    if status in ("partial", "not-implemented") and not impact:
        raise CatalogError(f"{source}: {requirement_id} is {status} but does not state the impact")

    combined = " ".join([str(raw.get("title", "")), gap, impact, str(raw.get("note", ""))]).lower()
    for word in FORBIDDEN_CLAIM_WORDS:
        if word in combined:
            raise CatalogError(
                f"{source}: {requirement_id} uses the phrase {word!r}. This repository "
                "makes no compliance or certification claim."
            )

    return Requirement(
        id=requirement_id,
        title=str(_require(raw, "title", source)),
        spec=str(_require(raw, "spec", source)),
        level=level,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        verification=verification,  # type: ignore[arg-type]
        level_verified=bool(raw.get("level_verified", False)),
        implemented_by=implemented_by,
        tests=tests,
        gap=gap,
        impact=impact,
        note=str(raw.get("note", "") or ""),
        family=family,
    )


def load_set(path: pathlib.Path) -> RequirementSet:
    """Load and validate one requirement file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CatalogError(f"{path.name}: top level must be a mapping")
    meta = data.get("meta") or {}
    for key in ("id", "title", "spec", "role"):
        if not meta.get(key):
            raise CatalogError(f"{path.name}: meta.{key} is required")

    raw_requirements = data.get("requirements") or []
    if not raw_requirements:
        raise CatalogError(f"{path.name}: declares no requirements")

    seen: set[str] = set()
    requirements: list[Requirement] = []
    for raw in raw_requirements:
        requirement = _coerce(raw, path.name, str(meta["id"]))
        if requirement.id in seen:
            raise CatalogError(f"{path.name}: duplicate requirement id {requirement.id}")
        seen.add(requirement.id)
        requirements.append(requirement)

    return RequirementSet(
        id=str(meta["id"]),
        title=str(meta["title"]),
        spec=str(meta["spec"]),
        role=str(meta["role"]),
        spec_edition_pinned=bool(meta.get("spec_edition_pinned", False)),
        requirements=tuple(requirements),
    )


def load_all(root: pathlib.Path | None = None) -> tuple[RequirementSet, ...]:
    base = root or CONFORMANCE_ROOT
    if not base.is_dir():
        raise CatalogError(f"conformance registry directory not found: {base}")
    files = sorted(base.glob("*.yaml"))
    if not files:
        raise CatalogError(f"no requirement files in {base}")
    sets = tuple(load_set(path) for path in files)

    all_ids = [r.id for s in sets for r in s.requirements]
    if len(set(all_ids)) != len(all_ids):
        raise CatalogError("requirement ids must be unique across all families")
    return sets


@functools.lru_cache(maxsize=1)
def get_all() -> tuple[RequirementSet, ...]:
    return load_all()


def all_requirements(root: pathlib.Path | None = None) -> tuple[Requirement, ...]:
    sets = load_all(root) if root else get_all()
    return tuple(r for s in sets for r in s.requirements)


def by_id(root: pathlib.Path | None = None) -> dict[str, Requirement]:
    return {r.id: r for r in all_requirements(root)}


# --------------------------------------------------------------- code anchors
def symbol_exists(anchor: str, repo_root: pathlib.Path | None = None) -> bool:
    """Check that ``path::Symbol`` or ``path::Class.method`` really exists.

    Resolved by parsing the file rather than importing it: an AST walk has no
    import side effects and works for methods nested in classes.
    """
    root = repo_root or REPO_ROOT
    path_part, _, symbol_part = anchor.partition("::")
    target = root / path_part
    if not target.is_file():
        return False
    if not symbol_part:
        return True

    try:
        tree = ast.parse(target.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - the repo would not import either
        return False

    wanted = symbol_part.split(".")

    def names_of(node: ast.stmt) -> list[str]:
        """Every name this statement binds: a def, a class, or an assignment."""
        name = getattr(node, "name", None)
        if isinstance(name, str):
            return [name]
        if isinstance(node, ast.Assign):
            return [t.id for t in node.targets if isinstance(t, ast.Name)]
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return [node.target.id]
        return []

    def walk(nodes: list[ast.stmt], remaining: list[str]) -> bool:
        head, rest = remaining[0], remaining[1:]
        for node in nodes:
            if head not in names_of(node):
                continue
            if not rest:
                return True
            body = getattr(node, "body", None)
            if isinstance(body, list):
                return walk(body, rest)
        return False

    return walk(tree.body, wanted)
