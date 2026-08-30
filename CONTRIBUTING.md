# Contributing

## Getting set up

```bash
make install     # .venv with runtime and dev dependencies (Python 3.11)
make check       # everything CI runs
```

## Before opening a pull request

```bash
make fmt         # ruff format + autofix
make check       # lint, mypy --strict, tests with the coverage gate, cfn-lint,
                 # shellcheck, and the spec-coverage freshness check
```

`make check` must be clean. The coverage gate is 88%; the suite currently sits at
93%.

## The most useful contribution

**Correcting a parameter against a specification you hold.**

`docs/spec-coverage.md` lists 116 OMA-CP parameters and 47 OMA-DM nodes, of which
25 and 23 respectively are marked as cross-checked. The rest are implemented from
public descriptions and from configurations widely deployed in the field.

If you hold RCC.07 or RCC.14 and can confirm a name, a type or a default:

1. edit the entry in `src/acs/catalog/omacp/base.yaml` or
   `src/acs/catalog/omadm/*.yaml`;
2. set `verified: true` and make `spec:` name the clause;
3. record the edition you used in `docs/scope.md` under "pinned specification
   editions";
4. `make coverage-doc` to regenerate the coverage document;
5. `make test`.

**Do not paste specification text into this repository.** RCC.07, RCC.14 and the
OMA specifications are licensed documents. A clause reference is enough.

Setting `verified: true` without naming an edition in `docs/scope.md` will be asked
to change: the flag is the repository's honesty mechanism and is worthless if it
means "looks right to me".

## Adding a provisioning parameter

Data change only:

```yaml
- path: APPLICATION:ap2002/MESSAGING/CHAT
  parm: NewParameter
  type: bool01            # chr | int | bool01 | enum
  default: "1"
  spec: RCC.07 <clause>
  verified: false
```

Validation is strict and fails at load: unknown type, an `enum` with no `values`, a
non-integer default on an `int`, an invalid characteristic path, a duplicate entry.

## Adding a management object

Drop a YAML file into `src/acs/catalog/omadm/`; the numeric prefix sets load order.
See [docs/oma-dm.md](docs/oma-dm.md#adding-a-management-object). No server code
changes — and
`tests/test_dm_motree.py::test_a_new_management_object_needs_no_code` keeps it that
way.

## Conventions

- `from __future__ import annotations` at the top of every module.
- Comments explain *why*, not *what*. A comment restating the code will be removed;
  a comment recording a specification subtlety or a security reason is valuable.
- Wire-level names keep the specification's casing exactly. Clients compare
  literally, so `AutAccept`, `ftAutAccept` and `MaxSize` stay as they are. `ruff`
  is configured not to complain.
- Tests are named as sentences describing the behaviour, and mark protocol
  requirements with `@pytest.mark.spec`.
- Type annotations are mandatory; `mypy --strict` covers the whole source tree.
- Line length 100.

## Testing expectations

New behaviour needs a test. A bug fix needs a test that fails before it.

Things the suite deliberately asserts, which should not regress:

- no raw IMSI, IMEI or MSISDN reaches a log or a metric dimension;
- XML entity resolution stays disabled on both parsers;
- the admin API returns `503` with no token configured;
- a production-unsafe configuration refuses to start;
- generated documents are deterministic;
- adding a management object requires no code.

## Security

Do not open a public issue for an exploitable finding. See
[SECURITY.md](SECURITY.md).

## Commits and pull requests

Imperative subject lines under about 70 characters. Explain the reasoning in the
body, especially for anything touching version semantics, identity resolution or
PII handling. Keep a pull request to one concern.
