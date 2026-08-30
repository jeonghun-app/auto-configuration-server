"""Meta-tests over the conformance registry.

The registry only has value if it can fail. A document that lists requirements and
says "implemented" beside each one is a rubber stamp; these tests are what make it
an assertion.

Six independent ways this file breaks the build:

1. a requirement names a test that does not exist, or was renamed;
2. a requirement names a source symbol that does not exist, or was renamed;
3. a requirement claims a status without the evidence the status demands
   (enforced in the loader);
4. the set of mandatory gaps differs from the frozen list below, so neither a new
   gap nor a silent upgrade can pass unnoticed;
5. a row uses compliance or certification language;
6. the generated document is stale.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from acs import conformance
from acs.errors import CatalogError

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Mandatory requirements that are not fully implemented, as of this commit.
#:
#: This list is deliberately explicit. Closing a gap, or discovering a new one,
#: requires editing this constant, which makes the change visible in review.
#: Nobody can quietly downgrade a requirement to make the suite green.
KNOWN_MANDATORY_GAPS: frozenset[str] = frozenset(
    {
        # OMA-DM
        "OMADM-HDR-VERPROTO-VALIDATE",
        "OMADM-HDR-MSGID",
        "OMADM-SIZE-SPLITTING",
        "OMADM-FLOW-CORRELATION",
        "OMADM-STATUS-5XX",
        "OMADM-AUTH-NONCE-ROTATION",
        "OMADM-AUTH-SERVER-TO-CLIENT",
        "OMADM-TREE-ACL",
        "OMADM-MO-DEVDETAIL-URI-LIMITS",
        "OMADM-ENC-WBXML",
        # RCC.14 / OMA-CP
        "RCC14-REQ-PARAMETERS",
        "RCC14-RESP-503",
        "RCC14-VERS-NEGATIVE",
        "RCC14-AUTH-MSISDN-FLOW",
        "OMACP-DOC-MNC-LENGTH",
        "OMACP-DOC-SEMANTIC-VALIDATION",
        "RCC14-PRIV-TLS",
    }
)


@pytest.fixture(scope="module")
def requirements() -> tuple[conformance.Requirement, ...]:
    return conformance.all_requirements()


# ------------------------------------------------------------------ loading
def test_registry_loads_both_specification_families() -> None:
    families = {s.id for s in conformance.load_all()}
    assert families == {"omadm", "rcc14"}


def test_registry_is_substantial(requirements: tuple[conformance.Requirement, ...]) -> None:
    assert len(requirements) >= 80


def test_requirement_ids_are_unique(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    ids = [r.id for r in requirements]
    assert len(set(ids)) == len(ids)


# ---------------------------------------------------- evidence really exists
def test_every_named_test_resolves_to_a_real_function(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    """The primary evidence gate, and the one that always applies.

    Resolved by parsing the test file, so it catches a renamed or deleted test no
    matter how pytest was invoked — including a single-file run, where a
    collection-based check would see almost nothing.
    """
    missing = sorted(
        f"{r.id} -> {node_id}"
        for r in requirements
        for node_id in r.tests
        if not conformance.symbol_exists(node_id, REPO_ROOT)
    )
    assert not missing, "conformance registry names tests that do not exist:\n" + "\n".join(missing)


def test_every_named_test_was_actually_collected(
    requirements: tuple[conformance.Requirement, ...],
    collected_node_ids: frozenset[str],
) -> None:
    """Second gate: a named test must also be collectable by pytest.

    A test can exist in a file and still never run — for example if its module
    fails to import, or a class-level skip hides it. Only files that this run
    collected are judged, so a targeted run stays useful.
    """
    collected_files = {node_id.split("::", 1)[0] for node_id in collected_node_ids}
    # A parametrised test is collected as name[param], so compare on the base id.
    collected_base = {node_id.split("[", 1)[0] for node_id in collected_node_ids}
    missing = sorted(
        f"{r.id} -> {node_id}"
        for r in requirements
        for node_id in r.tests
        if node_id.split("::", 1)[0] in collected_files and node_id not in collected_base
    )
    assert not missing, (
        "conformance registry names tests that exist but were not collected:\n" + "\n".join(missing)
    )


def test_the_full_suite_collects_every_named_test(
    requirements: tuple[conformance.Requirement, ...],
    collected_node_ids: frozenset[str],
) -> None:
    """On a full run, every cited test must have been collected.

    Skipped on a partial run so that ``pytest tests/test_conformance_registry.py``
    remains usable while developing.
    """
    test_files = {p.name for p in (REPO_ROOT / "tests").glob("test_*.py")}
    collected_files = {
        pathlib.Path(node_id.split("::", 1)[0]).name for node_id in collected_node_ids
    }
    if not test_files <= collected_files:
        pytest.skip("partial test run; the static resolution check still applies")

    collected_base = {node_id.split("[", 1)[0] for node_id in collected_node_ids}
    cited = {node_id for r in requirements for node_id in r.tests}
    assert cited <= collected_base, sorted(cited - collected_base)


def test_every_named_source_symbol_exists(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    missing = sorted(
        f"{r.id} -> {anchor}"
        for r in requirements
        for anchor in r.implemented_by
        if not conformance.symbol_exists(anchor, REPO_ROOT)
    )
    assert not missing, "conformance registry names symbols that do not exist:\n" + "\n".join(
        missing
    )


def test_implemented_requirements_name_both_a_symbol_and_a_test(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    for requirement in requirements:
        if requirement.status == "implemented":
            assert requirement.tests, requirement.id
            assert requirement.implemented_by, requirement.id


def test_gaps_state_a_gap_and_an_impact(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    for requirement in requirements:
        if requirement.is_gap:
            assert requirement.gap, requirement.id
            assert requirement.impact, requirement.id


# ------------------------------------------------------- the frozen gap list
def test_mandatory_gaps_match_the_frozen_list(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    actual = {r.id for r in requirements if r.blocks_conformance}
    newly_broken = sorted(actual - KNOWN_MANDATORY_GAPS)
    silently_fixed = sorted(KNOWN_MANDATORY_GAPS - actual)
    assert not newly_broken, (
        "new mandatory conformance gaps appeared. Fix them, or add them to "
        f"KNOWN_MANDATORY_GAPS with a reason: {newly_broken}"
    )
    assert not silently_fixed, (
        "these mandatory gaps are now closed. Remove them from "
        f"KNOWN_MANDATORY_GAPS so the improvement is recorded: {silently_fixed}"
    )


def test_there_are_still_mandatory_gaps(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    """A registry with no gaps at all would mean nobody looked hard enough.

    This is not a joke assertion: it fails if someone marks everything
    implemented, which is the most likely way this artifact gets corrupted.
    """
    assert any(r.blocks_conformance for r in requirements)


# --------------------------------------------------------------- honesty
def test_every_implemented_row_disclaims_certification(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    """The wording for an implemented row must say what it is not.

    The loader already refuses compliance language in the authored fields; this
    checks the other direction, that "implemented" is never presented bare.
    """
    for requirement in requirements:
        if requirement.status == "implemented":
            assert "No certification is claimed" in requirement.claim_wording(), requirement.id


def test_authored_fields_cannot_contain_compliance_language(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    # Belt and braces: the loader rejects these, so nothing should reach here.
    for requirement in requirements:
        authored = " ".join(
            [requirement.title, requirement.gap, requirement.impact, requirement.note]
        ).lower()
        for word in conformance.FORBIDDEN_CLAIM_WORDS:
            assert word not in authored, f"{requirement.id} claims too much"


def test_no_level_is_marked_as_verified_against_a_licensed_edition(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    """Nobody here holds the conformance requirement tables.

    If a future contributor pins an edition and verifies a classification, they
    must also update docs/scope.md and this test — deliberately, not by accident.
    """
    assert not [r.id for r in requirements if r.level_verified]


def test_no_family_claims_a_pinned_specification_edition() -> None:
    for requirement_set in conformance.load_all():
        assert requirement_set.spec_edition_pinned is False, requirement_set.id


def test_nothing_claims_interoperability_testing(
    requirements: tuple[conformance.Requirement, ...],
) -> None:
    # No real handset has ever talked to this server, so no row may claim it.
    assert not [r.id for r in requirements if r.verification == "interop-tested"]


def test_claim_wording_is_specific_per_status() -> None:
    by_id = conformance.by_id()
    assert "No certification is claimed" in by_id["OMADM-CMD-GET"].claim_wording()
    assert by_id["OMADM-ENC-WBXML"].claim_wording().startswith("Not implemented.")
    assert by_id["OMADM-HDR-MSGID"].claim_wording().startswith("Partially implemented.")


# ------------------------------------------------------------ loader rules
def write_registry(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    (tmp_path / "x.yaml").write_text(
        "meta:\n"
        "  id: x\n"
        "  title: X\n"
        "  spec: X spec\n"
        "  role: server\n"
        "requirements:\n" + body,
        encoding="utf-8",
    )
    return tmp_path


EVIDENCE_TEST = (
    "tests/test_conformance_registry.py::" "test_registry_loads_both_specification_families"
)

BASE_ROW = f"""  - id: X-ONE
    title: A thing
    spec: X 1.0
    level: mandatory
    status: implemented
    verification: behaviour-tested
    implemented_by: [src/acs/conformance.py::load_all]
    tests: [{EVIDENCE_TEST}]
"""


def test_a_well_formed_row_loads(tmp_path: pathlib.Path) -> None:
    assert len(conformance.all_requirements(write_registry(tmp_path, BASE_ROW))) == 1


def test_implemented_without_a_test_is_refused(tmp_path: pathlib.Path) -> None:
    body = BASE_ROW.replace(f"    tests: [{EVIDENCE_TEST}]\n", "")
    with pytest.raises(CatalogError, match="names no test"):
        conformance.all_requirements(write_registry(tmp_path, body))


def test_implemented_without_a_source_symbol_is_refused(tmp_path: pathlib.Path) -> None:
    body = BASE_ROW.replace("    implemented_by: [src/acs/conformance.py::load_all]\n", "")
    with pytest.raises(CatalogError, match="names no source symbol"):
        conformance.all_requirements(write_registry(tmp_path, body))


def test_a_gap_without_an_impact_is_refused(tmp_path: pathlib.Path) -> None:
    body = """  - id: X-TWO
    title: A missing thing
    spec: X 1.0
    level: mandatory
    status: not-implemented
    verification: code-review-only
    gap: it is missing
"""
    with pytest.raises(CatalogError, match="does not state the impact"):
        conformance.all_requirements(write_registry(tmp_path, body))


def test_a_gap_without_a_gap_description_is_refused(tmp_path: pathlib.Path) -> None:
    body = """  - id: X-THREE
    title: A missing thing
    spec: X 1.0
    level: mandatory
    status: not-implemented
    verification: code-review-only
    impact: something breaks
"""
    with pytest.raises(CatalogError, match="describes no gap"):
        conformance.all_requirements(write_registry(tmp_path, body))


def test_certification_language_is_refused(tmp_path: pathlib.Path) -> None:
    body = BASE_ROW.replace("    title: A thing\n", "    title: A fully compliant thing\n")
    with pytest.raises(CatalogError, match="makes no compliance"):
        conformance.all_requirements(write_registry(tmp_path, body))


def test_unknown_status_is_refused(tmp_path: pathlib.Path) -> None:
    body = BASE_ROW.replace("status: implemented", "status: probably-fine")
    with pytest.raises(CatalogError, match="unknown status"):
        conformance.all_requirements(write_registry(tmp_path, body))


def test_unknown_level_is_refused(tmp_path: pathlib.Path) -> None:
    body = BASE_ROW.replace("level: mandatory", "level: quite-important")
    with pytest.raises(CatalogError, match="unknown level"):
        conformance.all_requirements(write_registry(tmp_path, body))


def test_unknown_verification_is_refused(tmp_path: pathlib.Path) -> None:
    body = BASE_ROW.replace("verification: behaviour-tested", "verification: vibes")
    with pytest.raises(CatalogError, match="unknown verification"):
        conformance.all_requirements(write_registry(tmp_path, body))


def test_malformed_id_is_refused(tmp_path: pathlib.Path) -> None:
    body = BASE_ROW.replace("id: X-ONE", "id: lowercase-thing")
    with pytest.raises(CatalogError, match="malformed requirement id"):
        conformance.all_requirements(write_registry(tmp_path, body))


def test_duplicate_id_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(CatalogError, match="duplicate requirement id"):
        conformance.all_requirements(write_registry(tmp_path, BASE_ROW + BASE_ROW))


def test_empty_registry_directory_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(CatalogError, match="no requirement files"):
        conformance.all_requirements(tmp_path)


def test_missing_meta_is_refused(tmp_path: pathlib.Path) -> None:
    (tmp_path / "y.yaml").write_text("meta:\n  id: y\nrequirements: []\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="meta.title is required"):
        conformance.all_requirements(tmp_path)


# ------------------------------------------------------- symbol resolution
def test_symbol_resolution_finds_functions_and_methods() -> None:
    assert conformance.symbol_exists("src/acs/conformance.py::load_all", REPO_ROOT)
    assert conformance.symbol_exists(
        "src/acs/protocol/omadm/session.py::DmService._ack_only", REPO_ROOT
    )
    assert conformance.symbol_exists("src/acs/catalog/omadm/01-devinfo.yaml", REPO_ROOT)


def test_symbol_resolution_rejects_what_is_absent() -> None:
    assert not conformance.symbol_exists("src/acs/conformance.py::no_such_function", REPO_ROOT)
    assert not conformance.symbol_exists("src/acs/nope.py::x", REPO_ROOT)
    assert not conformance.symbol_exists(
        "src/acs/protocol/omadm/session.py::DmService.no_such_method", REPO_ROOT
    )


# ------------------------------------------------------- generated document
def test_generated_document_is_current() -> None:
    """docs/conformance.md must match the registry.

    Same gate as docs/spec-coverage.md: the document is generated, so a
    hand-edited or stale copy is a build failure.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_conformance.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_strict_mode_reports_the_mandatory_gaps() -> None:
    """--strict must exit non-zero while mandatory gaps remain.

    This is the check that stops the report reading as a pass. It is expected to
    fail today, and the test asserts that it does.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_conformance.py"), "--strict"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 1
    assert "mandatory" in (result.stdout + result.stderr).lower()
