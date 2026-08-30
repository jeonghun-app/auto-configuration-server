"""Specification families that are in scope but not assessed.

The point of this registry is to record honestly that a document is not held. The
most valuable test here is the one that refuses citation-shaped text: it turns
"we will not invent Korean specification content" from a promise into a build
failure.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from acs import specscope
from acs.conformance import by_id
from acs.errors import CatalogError

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

EXPECTED_FAMILIES = frozenset({"kr-tta-omadm", "kr-mno-interworking", "kr-domestic-other"})


@pytest.fixture(scope="module")
def families() -> tuple[specscope.SpecFamily, ...]:
    return specscope.load_families()


def test_the_declared_families_are_the_expected_ones(
    families: tuple[specscope.SpecFamily, ...],
) -> None:
    assert {f.id for f in families} == EXPECTED_FAMILIES


def test_all_three_are_unassessed(families: tuple[specscope.SpecFamily, ...]) -> None:
    # None of these documents is held, so none of them can be assessed.
    assert {f.id for f in specscope.unassessed()} == EXPECTED_FAMILIES
    assert not [f for f in families if f.document_held]


def test_no_family_carries_requirement_rows(
    families: tuple[specscope.SpecFamily, ...],
) -> None:
    """An unassessed family must not leak into the conformance counts.

    A requirement row implies a requirement was read. These were not.
    """
    requirement_ids = set(by_id())
    for family in families:
        assert family.id not in requirement_ids
        # Related gaps must be real requirement ids, not invented ones.
        for gap in family.related_gaps:
            assert gap in requirement_ids, f"{family.id} cites unknown requirement {gap}"


def test_every_unassessed_family_states_open_questions(
    families: tuple[specscope.SpecFamily, ...],
) -> None:
    for family in families:
        if family.blocks_assessment:
            assert family.open_questions, family.id
            assert family.obtain_from, family.id


def test_summary_never_implies_knowledge(
    families: tuple[specscope.SpecFamily, ...],
) -> None:
    for family in families:
        assert "not held" in family.summary() or family.state != "not-assessed"


# ------------------------------------------------- the anti-fabrication gate
@pytest.mark.parametrize(
    "poison",
    [
        "TTAK.KO-06.1234",
        "TTAS.KO-11.9999",
        "TTAE.OT-12.0001",
        "see clause 7 of the standard",
        "section 5.3 requires it",
        "requirement 4.2.1 applies",
        "Annex B lists the values",
        "as specified in §6",
    ],
)
def test_citation_shaped_text_is_refused(tmp_path: pathlib.Path, poison: str) -> None:
    """Nobody here holds these documents, so a citation is a guess.

    Each string below is the shape a fabricated reference would take. The loader
    must refuse all of them while document_held is false.
    """
    (tmp_path / "x.yaml").write_text(
        "families:\n"
        "  - id: made-up\n"
        "    title: A family\n"
        "    jurisdiction: Nowhere\n"
        "    state: not-assessed\n"
        "    document_held: false\n"
        f"    why_in_scope: {poison}\n"
        "    obtain_from: somewhere\n"
        "    open_questions: [what does it say]\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="document_held: false"):
        specscope.load_families(tmp_path)


def test_a_citation_is_allowed_once_the_document_is_held(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.yaml").write_text(
        "families:\n"
        "  - id: held\n"
        "    title: A family we actually have\n"
        "    jurisdiction: Nowhere\n"
        "    state: assessed\n"
        "    document_held: true\n"
        "    why_in_scope: clause 7 applies and we have read it\n"
        "    obtain_from: already held\n",
        encoding="utf-8",
    )
    assert specscope.load_families(tmp_path)[0].document_held is True


def test_the_real_registry_passes_its_own_citation_gate(
    families: tuple[specscope.SpecFamily, ...],
) -> None:
    for family in families:
        specscope.validate_no_fabricated_citations(family, "korea.yaml")


# --------------------------------------------------------------- loader rules
def test_assessed_without_the_document_is_refused(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.yaml").write_text(
        "families:\n"
        "  - id: liar\n"
        "    title: A family\n"
        "    jurisdiction: Nowhere\n"
        "    state: assessed\n"
        "    document_held: false\n"
        "    why_in_scope: because\n"
        "    obtain_from: somewhere\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="assessed without holding the document"):
        specscope.load_families(tmp_path)


def test_unassessed_without_open_questions_is_refused(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.yaml").write_text(
        "families:\n"
        "  - id: lazy\n"
        "    title: A family\n"
        "    jurisdiction: Nowhere\n"
        "    state: not-assessed\n"
        "    document_held: false\n"
        "    why_in_scope: because\n"
        "    obtain_from: somewhere\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="lists no open questions"):
        specscope.load_families(tmp_path)


def test_missing_required_field_is_refused(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.yaml").write_text(
        "families:\n  - id: thin\n    title: A family\n", encoding="utf-8"
    )
    with pytest.raises(CatalogError, match="missing 'jurisdiction'"):
        specscope.load_families(tmp_path)


def test_unknown_state_is_refused(tmp_path: pathlib.Path) -> None:
    (tmp_path / "x.yaml").write_text(
        "families:\n"
        "  - id: odd\n"
        "    title: A family\n"
        "    jurisdiction: Nowhere\n"
        "    state: probably-fine\n"
        "    why_in_scope: because\n"
        "    obtain_from: somewhere\n",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError, match="unknown state"):
        specscope.load_families(tmp_path)


def test_empty_directory_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(CatalogError, match="no specification scope files"):
        specscope.load_families(tmp_path)


def test_duplicate_family_is_refused(tmp_path: pathlib.Path) -> None:
    row = (
        "  - id: twice\n"
        "    title: A family\n"
        "    jurisdiction: Nowhere\n"
        "    state: not-assessed\n"
        "    document_held: false\n"
        "    why_in_scope: because\n"
        "    obtain_from: somewhere\n"
        "    open_questions: [what]\n"
    )
    (tmp_path / "x.yaml").write_text("families:\n" + row + row, encoding="utf-8")
    with pytest.raises(CatalogError, match="duplicate family id"):
        specscope.load_families(tmp_path)


# ------------------------------------------------------------ strict reporting
def test_strict_mode_reports_unassessed_families_separately() -> None:
    """A known-missing behaviour and an unread specification are different things.

    They must never be summed into one number.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_conformance.py"), "--strict"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "unassessed specification families: 3" in combined
    assert "mandatory requirements are not fully implemented" in combined
    assert "specification families are not assessed" in combined


def test_generated_document_lists_the_unassessed_families() -> None:
    document = (REPO_ROOT / "docs" / "conformance.md").read_text(encoding="utf-8")
    assert "Specification families not assessed" in document
    for family_id in EXPECTED_FAMILIES:
        assert family_id in document
    assert "the document is not held" in document
