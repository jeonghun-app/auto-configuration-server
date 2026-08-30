# Scope

The brief was "fill the specification completely, by agreement". This document is
that agreement. It states what is in scope, what is deliberately out, and what
cannot be done in any environment without an operator network.

## Pinned specification editions

`verified: true` in the catalogues is intended to mean "cross-checked against the
edition named here".

**No edition is currently pinned, so today it means something weaker:** the entry
was cross-checked against public descriptions of the specification and against
operator configurations widely deployed in the field. That is a real check, but it
is not a clause citation. Record the editions you actually hold, and re-confirm
the flagged entries against them, before treating `verified: true` as normative.

The same caveat governs `docs/conformance.md`: every requirement there carries
`level_verified: false`, because the mandatory/optional classification is this
project's judgement rather than a reading of the conformance requirement tables.

| Specification | Edition used for this implementation | Status |
| --- | --- | --- |
| GSMA RCC.14 — Service Provider Device Configuration | *not pinned* | Implemented from public descriptions of the RCC.14 HTTP configuration flow |
| GSMA RCC.07 — RCS Advanced Communications Services and Client Specification | *not pinned* | Parameter names and grouping from public descriptions and deployed operator configurations |
| GSMA Universal Profile | 1.0 and 2.4 modelled as profile overlays | Overlay structure verified, individual values not |
| OMA Client Provisioning, Provisioning Content 1.1 | 1.1 | Document structure verified |
| OMA Device Management 1.2 (SyncML DM) | 1.2 | Package flow, command set and authentication verified |
| 3GPP TS 24.167 — IMS Management Object | — | Node names verified against the public MO structure |
| 3GPP TS 33.220 — GBA | — | HTTP challenge shape only; see [limitations.md](limitations.md) |
| RFC 6585 §6 — HTTP 511 | — | Verified |

RCC.07 and RCC.14 are licensed GSMA documents. This repository contains no
specification text, and the honest statement of coverage is
[spec-coverage.md](spec-coverage.md), which is generated from the catalogues.

## In scope

**RCS configuration (OMA-CP over the RCC.14 HTTP flow)**

- Configuration request parsing: every documented query parameter, with type and
  length validation, alias tolerance for clients that disagree on casing, and
  rejection of duplicated identity parameters.
- Response semantics: `200` with a document, `200` with an empty body as the OTP
  pending signal, `200` with `VERS` only when the client is current, `400`,
  `401` (GBA), `403`, `429`, `503` and `511`.
- Configuration version semantics for `> 0`, `0`, `-1`, `-2`, `-3`, `-4`, and
  `validity`.
- Document generation: `VERS`, `TOKEN`, `MSG`, the IMS application (`ap2001`),
  the RCS application (`ap2002`) with SERVICES, MESSAGING (CHAT, FT,
  StandaloneMsg, MessageStore, Chatbot), IM, CAPDISCOVERY, PRESENCE, XDMS,
  OTHER, TRANSPORTPROTO, APN and a service-provider extension, plus the OMA-DM
  account (`w7`).
- Per-`rcs_profile` overlays and per-subscriber parameter overrides.
- Identity resolution: provisioning token, operator header enrichment, GBA
  (with the Digest response verified, not just the B-TID), and SMS OTP. The
  MSISDN entry web flow collects and verifies a number but does not yet complete
  provisioning — see `RCC14-AUTH-MSISDN-FLOW` in `docs/conformance.md`.
- Token lifecycle: issue, IMSI/IMEI binding, expiry, individual and bulk
  revocation.

**Device management (OMA-DM, SyncML DM 1.2)**

- Session flow across packages 1 to 4+, with `Status` for every received
  command and `Final` handling.
- Commands: `Alert`, `Get`, `Replace`, `Add`, `Exec`, `Results`, `Status`.
- Authentication: `syncml:auth-basic` and `syncml:auth-md5` with a server nonce
  and `Chal`, credentials bootstrapped by the OMA-CP `w7` characteristic.
- A declarative management object tree: DevInfo, DevDetail, the 3GPP IMS MO
  including the VoLTE parameters, and an RCS extension MO.
- Device inventory built from `Get`/`Results`.
- Session state in the shared store so a session survives load balancing.

**Operations**

- Admin API for subscribers, devices and coverage, failing closed.
- Health and readiness endpoints separated by blast radius.
- Structured JSON logs with PII redaction, and CloudWatch EMF metrics.
- One-command AWS deployment, teardown, and end-to-end verification.
- Two client simulators that fail the build on a specification violation.

## Out of scope

Out of scope because they are different products, not because they are hard:

| Not implemented | Belongs to |
| --- | --- |
| IMS core: P-CSCF, S-CSCF, I-CSCF, HSS | IMS network |
| SIP registration, MSRP sessions, RTP media | RCS client and IMS core |
| XDMS / XCAP server | XDM server (the ACS only provisions its URI) |
| Presence server | Presence/XDM infrastructure |
| Message store (IMAP/CPM) | Message store service |
| File transfer HTTP content server | Content server (the ACS only provisions its URI) |
| Chatbot platform and directory | Chatbot infrastructure |
| Firmware images and delivery | FUMO plus a content server; the MO can be added as YAML |
| WBXML encoding of SyncML | Refused explicitly with `415` rather than answered wrongly |
| Server-initiated DM notification (WAP Push / SMS trigger) | Needs an operator SMSC; `Alert` 1200 is accepted if a client starts the session |

## Cannot be done in any environment without an operator network

See [limitations.md](limitations.md) for the full treatment. Summary:

- Port-addressed binary OTP SMS (no AWS SMS service can set a UDH).
- Real GBA/AKA (needs a USIM, BSF and HSS).
- Real header enrichment (needs an operator packet gateway).
- Serving `config.rcs.mnc<MNC>.mcc<MCC>.pub.3gppnetwork.org` (operator/GSMA DNS).
- Handset interoperability testing (no device in CI).

## Definition of done, and whether it was met

| Criterion | Met | Evidence |
| --- | --- | --- |
| RCC.14 flow implemented including every status code and version value | yes | `tests/test_provisioning_service.py`, `tests/test_api_config.py`, `tests/test_vers.py` |
| OMA-CP document generated and structurally validated | yes | `tests/test_omacp_builder.py` |
| Extensible to OMA-DM | yes | `src/acs/protocol/omadm/`, `tests/test_dm_*.py` |
| VoLTE parameters manageable | yes | `src/acs/catalog/omadm/03-3gpp-ims.yaml`, DM push asserted in tests |
| Container based | yes | `Dockerfile`, non-root, digest-pinned, read-only root filesystem |
| Deployable on AWS with one command | yes | `scripts/deploy.sh`, `infra/*.yaml`, cfn-lint clean |
| Tests written and passing | yes | 409 tests, 93% coverage |
| Correct operation verified | yes, locally | `scripts/verify_stack.py`: 32 checks against the container, with both the in-memory and DynamoDB backends |
| Correct operation verified on AWS | yes | Deployed to us-east-1 (ECS Fargate + ALB + DynamoDB + Secrets Manager) and verified live: 29 checks, no raw identifier in CloudWatch Logs, EMF metrics extracted into the `RcsAcs` namespace |
| Every parameter cross-checked against a licensed spec edition | no | 25 of 116 OMA-CP parameters; see [spec-coverage.md](spec-coverage.md). Stated openly rather than claimed |
| Both specifications accommodated, requirement by requirement | partly, and counted | 113 requirements registered in [conformance.md](conformance.md): 80 implemented, 14 partial, 19 not implemented, of which 17 are classified mandatory. `scripts/gen_conformance.py --strict` exits non-zero because of them |
