# ADR 0006 — `400` for a malformed configuration request, configurable

**Status:** accepted

## Context

RCC.14 defines what to do when the ACS cannot identify a subscriber (`511`), when a
subscriber is not entitled (`403`), and when configuration is delivered (`200`). It
is much less clear about a request that is structurally invalid — a non-numeric
IMSI, an over-long `terminal_model`, a duplicated `OTP` parameter.

Deployed ACS implementations differ. Some answer `400`. Some answer `403`, which
makes a client treat itself as barred. Some answer `200` with a `VERS` of `0`,
which disables the client.

## Decision

Default to `400`, and make it configurable through
`ACS_MALFORMED_REQUEST_STATUS`.

## Rationale

`400` is the honest HTTP answer: the request was wrong, not the subscriber. It also
has the safest client behaviour of the three — a client that receives `400` retries
or reports an error, whereas `403` makes it mark itself as not entitled and `VERS=0`
switches RCS off. For a class of failure most likely caused by a client bug or an
attacker probing, the response should not disable the handset.

It is configurable because an operator integrating with a client fleet that expects
`403` needs to match that fleet, and forcing a code change for a single status code
would be poor design.

## What counts as malformed

From `parse_config_query`:

- `IMSI` that is not 5–15 digits;
- `IMEI`/`IMEISV` that is not 14–16 digits;
- `msisdn` that will not normalise to E.164;
- a non-alphanumeric `OTP`;
- `SMS_port` outside 0–65535, or any non-integer numeric field;
- a value longer than the per-field cap;
- a repeated identity parameter (`vers`, `IMSI`, `IMEI`, `msisdn`, `token`, `OTP`).

Deliberately *not* malformed:

- a negative `vers` — clients echo back the disable value they were given;
- a non-Luhn IMEI — field-test devices have them, and rejecting one locks a real
  handset out of provisioning;
- an unknown parameter — recorded and logged by name, never echoed back;
- lower-case spellings of `imsi`, `imei`, `otp` — real clients send them.

## Consequences

- The decision is documented and testable rather than implicit
  (`tests/test_api_config.py::test_malformed_status_is_configurable`).
- Unknown parameters are tolerated, so a newer client talking to an older ACS still
  provisions.
