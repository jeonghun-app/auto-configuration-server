# ADR 0003 — Provisioning parameters and management objects declared in YAML

**Status:** accepted

## Context

The brief was to fill the specification "completely, by agreement". The RCS
configuration surface is roughly 150 named parameters in a nested characteristic
tree, plus a management object tree for OMA-DM.

Two problems follow:

1. **Completeness is an auditing question, not a coding one.** If the parameters
   live inside Python functions that build XML, nobody can answer "which parts of
   the specification are covered, and which were verified against the document?"
2. **RCC.07 and RCC.14 are licensed documents.** They cannot be shipped, and the
   implementation is partly derived from public descriptions and from operator
   configurations seen in the field. Some entries are therefore uncertain, and that
   uncertainty must be visible rather than hidden.

## Decision

Declare every parameter once, in YAML, with a specification reference and a
`verified` flag. Code walks the declaration generically.

```yaml
- path: APPLICATION:ap2002/MESSAGING/FT
  parm: MaxSizeFileTr
  type: int
  unit: KB
  default: "10240"
  spec: RCC.07 A.1.4 FT
  verified: false
```

The same treatment for OMA-DM management objects (`src/acs/catalog/omadm/*.yaml`)
and for the configuration version semantics, which are a table with a citation per
row in `src/acs/protocol/vers.py`.

`docs/spec-coverage.md` is generated from these files by
`scripts/gen_spec_coverage.py`, and CI fails if it is stale.

## Consequences

**Good**

- Coverage is countable, and `GET /admin/coverage` reports it at runtime.
- A correction is a one-line YAML edit, reviewable by someone who holds the
  specification but does not read Python.
- Operator variants are overlay files (`profiles/UP_2.4.yaml`,
  `profiles/joyn_blackbird.yaml`) that add, override or remove entries.
- Adding an entire managed service is a data change.
- The README can state "25 of 116 cross-checked" instead of claiming compliance
  it cannot prove.

**Costs**

- Types are validated at load time rather than by the type checker. Mitigated by
  strict validation that fails startup: unknown type, enum without values,
  non-integer default on an `int`, invalid path, duplicate entry.
- A malformed catalogue must never degrade to an empty document — an empty document
  would switch RCS off on every handset that received it. Hence startup
  validation in `AppState.warm_catalogues`, called from the lifespan handler and
  from `/readyz`.
- Placeholder rendering (`{impi}`, `{ims_domain}`) is a small template language.
  A test asserts no entry uses a placeholder outside the known set.
