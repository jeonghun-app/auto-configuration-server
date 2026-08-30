# Security policy

## Reporting a vulnerability

Report privately through GitHub's
[private vulnerability reporting](https://github.com/jeonghun-app/auto-configuration-server/security/advisories/new).
Do not open a public issue for anything exploitable.

Please include the version or commit, reproduction steps, and the impact you
believe it has. Expect an acknowledgement within a few working days.

## Scope

In scope: this repository's source, container image, CloudFormation templates and
scripts.

Out of scope: the GSMA and OMA specifications themselves, and third-party
dependencies (report those upstream, though a note here is welcome).

## This service handles subscriber data

The ACS receives IMSI, IMEI and MSISDN, and returns IMS credentials. Treat any
finding that exposes those as high severity, including one that only exposes them
to an operator's own logs.

Specific attention is welcome on:

- anything that lets one subscriber obtain another's configuration document;
- any path that writes a raw identifier, OTP or token into a log, metric, trace or
  error message;
- OTP or token brute-force, replay, or a bypass of the send quotas;
- forging an identity header from an untrusted peer;
- XML parsing (both `wap-provisioningdoc` and SyncML) — XXE, entity expansion,
  unbounded depth;
- anything that lets an unauthenticated caller cause a `VERS` of `-2` to be served,
  since that disables RCS on the receiving handset.

## What is already known and documented

Please read [docs/limitations.md](docs/limitations.md) and
[docs/threat-model.md](docs/threat-model.md) first. Accepted residual risks
recorded there — response timing differences, a single shared admin token, no WAF
in the default stack, the possibility of an HTTP-only deployment — are not new
findings, though a concrete exploit that raises their severity is.

## Handling data in reports

Use identifiers from reserved test ranges (MCC `001`, MNC `01`) in reproduction
steps. Do not include real subscriber identifiers in an issue or an advisory.

## Deployment hardening

Before exposing a deployment to the internet:

- set `ACS_ADMIN_TOKEN`, or every admin route stays at `503` (this is the default);
- deploy with an ACM certificate — without one the ALB serves cleartext and the
  query string contains subscriber identifiers and the OTP;
- keep `--allowed-cidr` narrow; `scripts/deploy.sh` refuses `0.0.0.0/0`;
- attach a WAF rate-based rule;
- set an account SMS spending limit;
- leave `EnableAlbAccessLogs` at `false` unless you have a retention policy for
  subscriber data;
- leave `ACS_TRUSTED_PROXY_CIDRS` empty unless a real operator gateway fronts the
  service.
