## What this changes

<!-- One paragraph. If it touches version semantics, identity resolution or PII
     handling, explain the reasoning, not just the change. -->

## Specification impact

- [ ] No change to wire behaviour
- [ ] Adds or corrects a parameter (catalogue only)
- [ ] Changes response codes or version semantics
- [ ] Adds or changes a management object

If a `verified` flag was set to `true`, name the specification edition used and
record it in `docs/scope.md`:

## Verification

- [ ] `make check` clean
- [ ] `make coverage-doc` run if a catalogue changed
- [ ] New behaviour has a test; a bug fix has a test that failed before it
- [ ] `scripts/verify_stack.py` run against a local container, if the flow changed

## Privacy

- [ ] No raw IMSI, IMEI, MSISDN, OTP or token is written to a log, metric or error
- [ ] Any new identifier in a test uses a reserved test range (MCC 001)
