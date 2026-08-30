#!/usr/bin/env python3
"""Generate ``docs/conformance.md`` from the requirement registry.

    python scripts/gen_conformance.py            # write the document
    python scripts/gen_conformance.py --check    # fail if the document is stale
    python scripts/gen_conformance.py --strict   # fail while mandatory gaps remain

``--check`` is the CI gate that keeps the document honest. ``--strict`` is the one
that stops it reading as a pass: it exits non-zero for as long as any mandatory
requirement is unimplemented, which is the case today and is stated as such.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from acs.conformance import Requirement, RequirementSet, load_all  # noqa: E402
from acs.specscope import SpecFamily, load_families  # noqa: E402

OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "conformance.md"

STATUS_MARK = {
    "implemented": "yes",
    "partial": "partial",
    "not-implemented": "no",
    "not-applicable": "n/a",
}

PREAMBLE = """<!-- GENERATED FILE — edit src/acs/catalog/conformance/*.yaml, then run
     scripts/gen_conformance.py -->
# Conformance registry

Answers one question, requirement by requirement: **which parts of the OMA-DM and
RCC.14 specifications does this server accommodate, and which does it not?**

## Read this before reading the tables

Four things are kept deliberately separate, because collapsing them is how a
document like this becomes misleading:

| Column | What it means |
| --- | --- |
| **Level** | Whether the requirement is mandatory, optional or conditional *for a
  server*. **This is this project's engineering judgement, not a citation.** |
| **Status** | What the code does: implemented, partial, not implemented. |
| **Evidence** | *How we know*: a passing test that asserts the wire behaviour, or
  only a code review. |
| **Gap / impact** | For anything less than implemented, what is missing and what breaks. |

No specification edition is pinned. Nobody working on this repository holds
RCC.14, RCC.07 or the OMA DM conformance requirement tables, so the Level column
could be wrong. `level_verified` is `false` on every row and a test enforces that.

**Nothing here is certified.** "Implemented" means the message exchange is
implemented and a test asserts it. It does not mean GSMA or OMA has confirmed
anything, and it does not mean a real handset has been tried — no real device has
ever talked to this server. See [limitations.md](limitations.md).

Parameter *spelling* coverage is counted separately in
[spec-coverage.md](spec-coverage.md).

## How this document can fail

It is generated and gated, not written:

* every requirement claiming a status must name a test, and
  `tests/test_conformance_registry.py` checks those names against the tests pytest
  actually collected — a renamed test breaks the build;
* every named source symbol is resolved by parsing the file;
* the set of mandatory gaps is frozen in a constant, so neither a new gap nor a
  silent upgrade to "implemented" can pass unnoticed;
* compliance and certification wording is rejected by the loader;
* `--check` fails if this file is stale.
"""


def summary_table(sets: tuple[RequirementSet, ...]) -> list[str]:
    lines = [
        "## Summary\n",
        "| Specification | Requirements | Implemented | Partial | Not implemented "
        "| Mandatory gaps |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for requirement_set in sets:
        counts = requirement_set.counts()
        lines.append(
            f"| {requirement_set.title} | {len(requirement_set.requirements)} "
            f"| {counts['implemented']} | {counts['partial']} "
            f"| {counts['not-implemented']} | {len(requirement_set.mandatory_gaps)} |"
        )
    total = [r for s in sets for r in s.requirements]
    gaps = [r for r in total if r.blocks_conformance]
    lines.append(
        f"| **Total** | **{len(total)}** "
        f"| **{sum(1 for r in total if r.status == 'implemented')}** "
        f"| **{sum(1 for r in total if r.status == 'partial')}** "
        f"| **{sum(1 for r in total if r.status == 'not-implemented')}** "
        f"| **{len(gaps)}** |"
    )
    lines.append("")
    pending = [f for f in load_families() if f.blocks_assessment]
    lines.append(
        f"**Overall: not fully conformant.** {len(gaps)} requirements this project "
        "classifies as mandatory are not fully implemented, and "
        f"{len(pending)} further specification families could not be assessed at all "
        "because the documents are not held. Both are listed below, and "
        "`scripts/gen_conformance.py --strict` exits non-zero for each reason "
        "separately.\n"
    )
    return lines


def unassessed_section(families: tuple[SpecFamily, ...]) -> list[str]:
    pending = [f for f in families if f.blocks_assessment]
    lines = ["## Specification families not assessed\n"]
    if not pending:
        lines.append("None: every declared family has been assessed.\n")
        return lines
    lines.append(
        "These were asked for and **cannot be assessed here, because the document is "
        "not held**. They deliberately carry no requirement rows: a row would imply a "
        "requirement had been read. Counting them separately keeps the numbers above "
        "meaningful.\n"
    )
    for family in pending:
        lines.append(f"### `{family.id}` — {family.title}\n")
        lines.append(f"* Jurisdiction: {family.jurisdiction}")
        lines.append("* State: **not assessed**, document not held")
        lines.append(f"* Why in scope: {family.why_in_scope}")
        lines.append(f"* How to obtain: {family.obtain_from}")
        if family.publicly_knowable:
            lines.append("* Knowable without the document:")
            lines.extend(f"  * {item}" for item in family.publicly_knowable)
        lines.append("* Only the document can answer:")
        lines.extend(f"  * {item}" for item in family.open_questions)
        if family.related_gaps:
            lines.append(
                "* Existing gaps an assessment would most likely touch: "
                + ", ".join(f"`{gap}`" for gap in family.related_gaps)
            )
        if family.note:
            lines.append(f"* Note: {family.note}")
        lines.append("")
    return lines


def gap_section(sets: tuple[RequirementSet, ...]) -> list[str]:
    gaps = [r for s in sets for r in s.requirements if r.blocks_conformance]
    lines = ["## Mandatory gaps\n"]
    if not gaps:  # pragma: no cover - not the current state
        lines.append("None.\n")
        return lines
    for requirement in gaps:
        lines.append(f"### `{requirement.id}` — {requirement.title}\n")
        lines.append(f"* Status: **{requirement.status}** ({requirement.verification})")
        lines.append(f"* Reference: {requirement.spec}")
        lines.append(f"* Gap: {requirement.gap.strip()}")
        lines.append(f"* Impact: {requirement.impact.strip()}\n")
    return lines


def requirement_rows(requirement: Requirement) -> str:
    tests = "<br>".join(f"`{t.split('::')[-1]}`" for t in requirement.tests) or "—"
    detail = requirement.gap.strip() or requirement.note.strip() or "—"
    return (
        f"| `{requirement.id}` | {requirement.title} | {requirement.level} "
        f"| {STATUS_MARK[requirement.status]} | {requirement.verification} "
        f"| {tests} | {detail} |"
    )


def family_section(requirement_set: RequirementSet) -> list[str]:
    lines = [
        f"## {requirement_set.title}\n",
        f"* Specification: {requirement_set.spec}",
        f"* Role audited: {requirement_set.role}",
        f"* Edition pinned: {'yes' if requirement_set.spec_edition_pinned else '**no**'}\n",
        "| Id | Requirement | Level | Status | Evidence | Tests | Gap or note |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(requirement_rows(r) for r in requirement_set.requirements)
    lines.append("")
    return lines


def render() -> str:
    sets = load_all()
    families = load_families()
    lines: list[str] = [PREAMBLE]
    lines.extend(summary_table(sets))
    lines.extend(unassessed_section(families))
    lines.extend(gap_section(sets))
    for requirement_set in sets:
        lines.extend(family_section(requirement_set))
    lines.append("## Extending this registry\n")
    lines.append(
        "Add a row to `src/acs/catalog/conformance/omadm.yaml` or `rcc14.yaml`, name "
        "the test that proves it, and run `make conformance-doc`. The loader refuses "
        "a row that claims a status without evidence, and refuses compliance wording "
        "outright.\n"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the conformance registry document")
    parser.add_argument("--check", action="store_true", help="fail if the document is stale")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail while any mandatory requirement is not fully implemented",
    )
    args = parser.parse_args(argv)

    sets = load_all()
    gaps = [r for s in sets for r in s.requirements if r.blocks_conformance]
    pending = [f for f in load_families() if f.blocks_assessment]

    if args.strict:
        total = sum(len(s.requirements) for s in sets)
        implemented = sum(1 for s in sets for r in s.requirements if r.status == "implemented")
        print(f"requirements: {total}, implemented: {implemented}, mandatory gaps: {len(gaps)}")
        for requirement in gaps:
            print(f"  {requirement.status:16} {requirement.id}: {requirement.title}")
        if pending:
            print(f"unassessed specification families: {len(pending)} (document not held)")
            for family in pending:
                print(f"  not-assessed     {family.id}: {family.title}")
        # Two independent reasons, never summed into one number: a known-missing
        # behaviour and an unread specification are not the same kind of thing.
        reasons = []
        if gaps:
            reasons.append(f"{len(gaps)} mandatory requirements are not fully implemented")
        if pending:
            reasons.append(f"{len(pending)} specification families are not assessed")
        if reasons:
            print("\nSTRICT CONFORMANCE: FAIL — " + "; ".join(reasons) + ".", file=sys.stderr)
            return 1
        print("\nSTRICT CONFORMANCE: PASS")
        return 0

    content = render()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != content:
            print(
                "docs/conformance.md is stale; run scripts/gen_conformance.py",
                file=sys.stderr,
            )
            return 1
        print("docs/conformance.md is up to date")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"wrote docs/conformance.md ({len(gaps)} mandatory gaps recorded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
