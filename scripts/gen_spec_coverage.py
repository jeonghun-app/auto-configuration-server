#!/usr/bin/env python3
"""Generate ``docs/spec-coverage.md`` from the catalogues.

Coverage is a property of the data, not of prose, so the document is generated.
That keeps the repository honest: it can state exactly how many parameters have
been cross-checked against the pinned specification edition instead of making an
unprovable "fully compliant" claim.

    python scripts/gen_spec_coverage.py [--check]

``--check`` fails when the committed document is stale, which is what CI runs.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from acs.protocol.omacp.catalog import available_profiles, load_catalog  # noqa: E402
from acs.protocol.omadm.motree import load_tree  # noqa: E402
from acs.protocol.vers import VERS_RULES  # noqa: E402

OUTPUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "spec-coverage.md"

HEADER = """<!-- GENERATED FILE — edit the catalogues, then run scripts/gen_spec_coverage.py -->
# Specification coverage

This document is generated from the declarative catalogues:

* `src/acs/catalog/omacp/` — OMA-CP provisioning parameters (RCC.14 / RCC.07)
* `src/acs/catalog/omadm/` — OMA-DM management objects
* `src/acs/protocol/vers.py` — configuration version semantics

## How to read `verified`

`verified: true` means the entry has been cross-checked against the pinned
specification edition named in `docs/scope.md`. `verified: false` means the entry
is implemented from public descriptions of the specification and from
configurations that are widely deployed in the field: it is structurally correct,
typed and tested, but the repository does **not** claim clause-level
certification for it.

Nothing here is a GSMA certification. See `docs/limitations.md`.
"""


def bar(verified: int, total: int) -> str:
    if not total:
        return "n/a"
    percent = 100.0 * verified / total
    filled = int(round(percent / 5))
    return f"`{'#' * filled}{'.' * (20 - filled)}` {verified}/{total} ({percent:.0f}%)"


def render() -> str:
    catalog = load_catalog()
    tree = load_tree()
    lines: list[str] = [HEADER]

    lines.append("## Summary\n")
    lines.append("| Surface | Entries | Cross-checked |")
    lines.append("| --- | --- | --- |")
    lines.append(
        f"| OMA-CP parameters | {len(catalog.entries)} | "
        f"{bar(catalog.verified_count, len(catalog.entries))} |"
    )
    nodes = tree.all_nodes()
    lines.append(f"| OMA-DM nodes | {len(nodes)} | {bar(tree.verified_count, len(nodes))} |")
    verified_rules = sum(1 for rule in VERS_RULES if rule.verified)
    lines.append(
        f"| Version semantics rows | {len(VERS_RULES)} | {bar(verified_rules, len(VERS_RULES))} |"
    )
    lines.append("")

    profiles = ", ".join("`" + p + "`" for p in available_profiles())
    lines.append(f"Available RCS profiles: {profiles}\n")

    # ---- version semantics
    lines.append("## Configuration version semantics\n")
    lines.append(
        "| `VERS/version` | Client action | Deletes config | May re-query "
        "| Reference | Cross-checked |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for rule in VERS_RULES:
        lines.append(
            f"| `{rule.version}` | {rule.action.value} | "
            f"{'yes' if rule.delete_config else 'no'} | "
            f"{'yes' if rule.may_requery else 'no'} | {rule.spec_ref} | "
            f"{'yes' if rule.verified else 'no'} |"
        )
    lines.append("")
    lines.append(
        "> The interpretation of `-1` to `-4` differs between RCC.14 releases and "
        "between vendor implementations. It is encoded as a single reviewable table "
        "in `src/acs/protocol/vers.py` for exactly that reason.\n"
    )

    # ---- OMA-CP
    lines.append("## OMA-CP parameters\n")
    grouped: dict[str, list[object]] = {}
    for entry in catalog.entries:
        grouped.setdefault(entry.path, []).append(entry)

    for path in grouped:
        entries = grouped[path]
        lines.append(f"### `{path}`\n")
        lines.append("| Parameter | Type | Default | Reference | Cross-checked |")
        lines.append("| --- | --- | --- | --- | --- |")
        for entry in entries:
            default = getattr(entry, "default", "")
            shown = f"`{default}`" if default else "_(omitted)_"
            unit = getattr(entry, "unit", "")
            if unit:
                shown += f" {unit}"
            lines.append(
                f"| `{entry.parm}` | {entry.type} | {shown} | {entry.spec} | "  # type: ignore[attr-defined]
                f"{'yes' if entry.verified else 'no'} |"  # type: ignore[attr-defined]
            )
        lines.append("")

    # ---- OMA-DM
    lines.append("## OMA-DM management objects\n")
    for mo in tree.objects:
        lines.append(f"### {mo.title}\n")
        lines.append(f"* URN: `{mo.urn}`")
        lines.append(f"* Root: `{mo.root}`")
        lines.append(f"* Reference: {mo.spec or 'n/a'}\n")
        lines.append("| Node | Format | Owner | Default | Feature | Cross-checked |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for node in mo.nodes:
            default = f"`{node.default}`" if node.default else "_(none)_"
            lines.append(
                f"| `{node.uri}` | {node.format} | {node.source} | {default} | "
                f"{node.feature or '-'} | {'yes' if node.verified else 'no'} |"
            )
        lines.append("")

    lines.append("## Extending coverage\n")
    lines.append(
        "Adding a parameter is a data change: append an entry to "
        "`src/acs/catalog/omacp/base.yaml` (or a profile overlay) and regenerate this "
        "document. Adding a whole new managed service — VoLTE extensions, firmware "
        "update, a vendor MO — means dropping a YAML file into "
        "`src/acs/catalog/omadm/`. Neither requires touching server code.\n"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the specification coverage document")
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args(argv)

    content = render()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != content:
            print(
                "docs/spec-coverage.md is stale; run scripts/gen_spec_coverage.py",
                file=sys.stderr,
            )
            return 1
        print("docs/spec-coverage.md is up to date")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(OUTPUT.parents[1])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
