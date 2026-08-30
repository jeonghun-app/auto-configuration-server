"""Specification families that are in scope but **not assessed**.

The conformance registry (:mod:`acs.conformance`) records requirements we have
read and judged. This module records the opposite: specification families someone
has asked this server to satisfy, where **the document is not held**, so no
requirement, level or count can honestly be stated.

The distinction is the whole point. Writing speculative requirement rows for an
unseen document would put guesses in the same table as 113 requirements that were
actually read, and would give a false denominator — "19 not implemented" would
silently start meaning "19 known-missing behaviours plus some number of things we
imagined". A family with no document gets no rows at all; it gets an honest
"not assessed", and it makes ``--strict`` fail for a *separately named* reason.

What this module deliberately cannot express: a clause number, a parameter name, a
requirement, or a count. :func:`validate_no_fabricated_citations` refuses
citation-shaped text while ``document_held`` is false, so the promise not to invent
Korean specification content is enforced by the build rather than by good
intentions.
"""

from __future__ import annotations

import dataclasses
import functools
import pathlib
import re
from typing import Any, Final

import yaml

from acs.errors import CatalogError

SPECSCOPE_ROOT: Final = pathlib.Path(__file__).resolve().parent / "catalog" / "specscope"

#: Patterns that look like a citation of a document we do not hold. If any of
#: these appear while ``document_held`` is false, the text is a guess dressed as a
#: reference and the loader refuses it.
CITATION_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    (r"TTAK", "a TTA standard number"),
    (r"TTAS", "a TTA standard number"),
    (r"TTAE", "a TTA standard number"),
    (r"§", "a clause marker"),
    (r"\bclause\s+\d", "a clause number"),
    (r"\bsection\s+\d+\.\d", "a section number"),
    (r"\b\d+\.\d+\.\d+\b", "a dotted clause number"),
    (r"\bAnnex\s+[A-Z]\b", "an annex reference"),
)

_ID_RE: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_STATES: Final[frozenset[str]] = frozenset({"not-assessed", "assessed", "not-applicable"})


@dataclasses.dataclass(frozen=True, slots=True)
class SpecFamily:
    """A specification family and the state of our assessment of it."""

    id: str
    title: str
    jurisdiction: str
    state: str
    document_held: bool
    why_in_scope: str
    obtain_from: str
    publicly_knowable: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    related_gaps: tuple[str, ...] = ()
    note: str = ""

    @property
    def blocks_assessment(self) -> bool:
        return self.state == "not-assessed"

    def summary(self) -> str:
        if self.state == "assessed":
            return "Assessed; see the conformance registry."
        if self.state == "not-applicable":
            return f"Out of scope. {self.note}".strip()
        return (
            "Not assessed: the document is not held, so no requirement, level or "
            "count can be stated."
        )


def validate_no_fabricated_citations(family: SpecFamily, source: str) -> None:
    """Refuse citation-shaped text for a family whose document is not held."""
    if family.document_held:
        return
    haystack = " ".join(
        [
            family.title,
            family.why_in_scope,
            family.obtain_from,
            family.note,
            *family.publicly_knowable,
            *family.open_questions,
        ]
    )
    for pattern, description in CITATION_PATTERNS:
        if re.search(pattern, haystack, re.IGNORECASE):
            raise CatalogError(
                f"{source}: family {family.id} is marked document_held: false but its "
                f"text contains {description}. A reference to a document nobody here "
                "holds is a guess; remove it or set document_held: true and cite the "
                "edition in docs/scope.md."
            )


def _coerce(raw: dict[str, Any], source: str) -> SpecFamily:
    for key in ("id", "title", "jurisdiction", "state", "why_in_scope", "obtain_from"):
        if not raw.get(key):
            raise CatalogError(f"{source}: family {raw.get('id', '?')} is missing '{key}'")

    family_id = str(raw["id"]).strip()
    if not _ID_RE.match(family_id):
        raise CatalogError(f"{source}: malformed family id {family_id!r}")

    state = str(raw["state"])
    if state not in _STATES:
        raise CatalogError(f"{source}: {family_id} has unknown state {state!r}")

    document_held = bool(raw.get("document_held", False))
    if state == "assessed" and not document_held:
        raise CatalogError(
            f"{source}: {family_id} claims to be assessed without holding the document"
        )
    if state == "not-assessed" and not raw.get("open_questions"):
        raise CatalogError(
            f"{source}: {family_id} is not assessed but lists no open questions. "
            "State what the document would have to tell us."
        )

    family = SpecFamily(
        id=family_id,
        title=str(raw["title"]),
        jurisdiction=str(raw["jurisdiction"]),
        state=state,
        document_held=document_held,
        why_in_scope=str(raw["why_in_scope"]).strip(),
        obtain_from=str(raw["obtain_from"]).strip(),
        publicly_knowable=tuple(str(x).strip() for x in raw.get("publicly_knowable", ()) or ()),
        open_questions=tuple(str(x).strip() for x in raw.get("open_questions", ()) or ()),
        related_gaps=tuple(str(x).strip() for x in raw.get("related_gaps", ()) or ()),
        note=str(raw.get("note", "") or "").strip(),
    )
    validate_no_fabricated_citations(family, source)
    return family


def load_families(root: pathlib.Path | None = None) -> tuple[SpecFamily, ...]:
    base = root or SPECSCOPE_ROOT
    if not base.is_dir():
        raise CatalogError(f"specification scope directory not found: {base}")
    files = sorted(base.glob("*.yaml"))
    if not files:
        raise CatalogError(f"no specification scope files in {base}")

    families: list[SpecFamily] = []
    seen: set[str] = set()
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise CatalogError(f"{path.name}: top level must be a mapping")
        raw_families = data.get("families") or []
        if not raw_families:
            raise CatalogError(f"{path.name}: declares no families")
        for raw in raw_families:
            family = _coerce(raw, path.name)
            if family.id in seen:
                raise CatalogError(f"{path.name}: duplicate family id {family.id}")
            seen.add(family.id)
            families.append(family)
    return tuple(families)


@functools.lru_cache(maxsize=1)
def get_families() -> tuple[SpecFamily, ...]:
    return load_families()


def unassessed(root: pathlib.Path | None = None) -> tuple[SpecFamily, ...]:
    families = load_families(root) if root else get_families()
    return tuple(f for f in families if f.blocks_assessment)
