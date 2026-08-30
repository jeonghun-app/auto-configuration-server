# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] — 2026-08-30

### Added

- **Specification scope registry** (`src/acs/catalog/specscope/`). Three families
  were raised — a TTA standard for OMA-DM based terminal management, the Korean
  three-operator RCS interworking specification, and unnamed Korean domestic
  specifications — and none of those documents is held, so none can be assessed.
  They are recorded as `not-assessed` with what is publicly knowable, what only
  the document can answer, and where to obtain it. They deliberately carry **no
  requirement rows**: a row would imply a requirement had been read, and would put
  guesses in the same table as 113 requirements that were actually read.
- `--strict` now fails for two independent, separately named reasons — mandatory
  gaps and unassessed families — never summed into one number.
- The unassessed families are reported by `GET /admin/conformance`, by the console
  conformance page, and in `docs/conformance.md`.
- **An anti-fabrication gate.** The loader refuses citation-shaped text — a TTA
  standard number, a clause or section number, an annex reference — while a
  family's document is not held. Eight fabrication attempts are tested. The
  promise not to invent Korean specification content is now enforced by the build.

### Fixed

- **A national-format MSISDN produced an invalid E.164 number.** The Korean
  national form `01012345678` normalised to `+01012345678`: no country code begins
  with zero, so that value is not a phone number, would never match a subscriber
  record or an OTP challenge key, and would sit in the database looking plausible.
  Reachable from the RCC.14 request parser, the public 511 recovery flow, the admin
  API and the console. Such a number is now refused, and with
  `ACS_DEFAULT_COUNTRY_CODE` set it is converted instead — `82` turns
  `01012345678` into `+821012345678`. Empty by default, because guessing a country
  would silently provision the wrong subscriber.

### Deliberately not done

- **No Korean operator profile overlay.** A profile overlay can contain nothing but
  values served to handsets, and `available_profiles()` advertises any file
  immediately in the console and as a valid `rcs_profile` on the wire. A file of
  invented defaults would be worse than no file: a wrong `MaxSizeFileTr` or
  `ftDefaultMech` breaks file transfer on a real handset silently.
- **No new conformance status value.** Adding `unknown` to the status set would let
  a future contributor mark a real OMA-DM requirement `unknown` and remove it from
  the mandatory-gap count without editing the frozen list — a hole in the one gate
  that prevents quiet downgrades.

## [1.2.0] — 2026-08-30

### Added

- **Operator console** at `/admin/ui`, deployed with the service so an
  installation comes with a management page and not only a JSON API. Server
  rendered, no JavaScript, five pages:
  - **Numbers** — subscribers searchable by MSISDN or IMSI, with entitlement,
    profile, VoLTE, IMEI allowlist, forced configuration version, and the
    operational actions (bump version, enable, revoke tokens, issue a token,
    delete).
  - **Parameters per number** — override any of the 116 OMA-CP parameters or the
    47 OMA-DM nodes for one subscriber, selected from the catalogues. An
    uncatalogued key is refused, because a typo would otherwise sit in the record
    doing nothing.
  - **Devices** — the inventory built from RCC.14 parameters and from every
    management node a handset returned over OMA-DM, linked back to its number.
  - **Parameters catalogue** — everything the server can send, filterable, with
    each entry's reference and `verified` flag.
  - **Conformance** — the requirement registry including the gaps.
- Security: fail-closed without `ACS_ADMIN_TOKEN`; an HMAC-signed, expiring,
  `HttpOnly`, `SameSite=Strict` session cookie signed with the admin token so
  rotating it invalidates every session; CSRF on every mutating form; a CSP of
  `default-src 'none'` that forbids scripts; `no-store` on every page; and every
  rendered value escaped, with a test driving an XSS payload through the device
  pages because a management object value comes from an untrusted handset.

## [1.1.0] — 2026-08-30

A conformance audit of both specification planes, and the fixes it produced.

### Added

- **Conformance registry.** 113 requirements across OMA-DM 1.2 and RCC.14/OMA-CP
  declared in `src/acs/catalog/conformance/`, each with a level, an
  implementation status, the evidence behind it and, for anything less than
  implemented, the gap and its impact. `docs/conformance.md` is generated from it
  and `GET /admin/conformance` reports it at runtime.
- **Meta-tests that make the registry able to fail**: a cited test is resolved
  both statically and against pytest's collection, implementing symbols are
  resolved by AST, a status without evidence is refused by the loader, the
  mandatory-gap set is frozen in a constant so neither a new gap nor a silent
  upgrade passes unnoticed, compliance wording is rejected, and the generated
  document is freshness-gated. All six were verified by deliberately breaking
  them.
- Wire-level conformance evidence tests (`tests/test_conformance_protocol.py`)
  that assert what goes out on the wire rather than that a constant exists.
- `POST /admin/subscribers/{imsi}/issue-token` for pre-provisioning, and for
  verifying a deployment where the OTP cannot be read.
- `make conformance`, `make conformance-doc`, and two new CI steps.

### Fixed

- **GBA authentication bypass.** `_resolve_gba` authenticated on the B-TID in the
  `Authorization` username directive without ever verifying the Digest response,
  so anyone who had seen a B-TID could provision as that subscriber.
  `gba.verify_authorization` now recomputes the response with `Ks_NAF` and checks
  that the nonce is one this server issued, using stateless HMAC-signed nonces.
  `ACS_GBA_NONCE_SECRET` is now required when GBA is enabled. GBA is off by
  default, so a default deployment was never exposed.
- **The DM server claimed to perform commands it had not.** `Delete`, `Copy`,
  `Sequence`, `Atomic`, `Exec` and unrecognised commands were answered `200`;
  they are now answered `406`.
- **Interior nodes were never created.** A `Replace` on `./3GPP_IMS/1/Timer_T1`
  gets `404` on a device where that instance does not exist, silently abandoning
  the whole configuration push. Interior nodes are now `Add`ed parent-first, and
  `418` already-exists is treated as success.
- `Alert` 1223 now aborts the session and discards its state.
- The client's `MaxMsgSize` was parsed and discarded; the server no longer
  advertises more than the client accepts.
- Missing DM credentials now produce `407`, not `401`.
- DM sessions were keyed on the client-chosen `SessionID` alone, so two handsets
  picking the same value shared one server-side session. The key is now
  namespaced by device.
- `POST` on the configuration endpoint now reads form parameters, so the OTP can
  actually be kept out of the query string as documented.
- An `md5` session now carries a `Chal` on success rather than leaving the
  previous credential replayable with no further exchange.

### Changed

- `docs/scope.md` no longer claims `verified: true` means a pinned-edition
  cross-check while also stating that no edition is pinned.
- The README no longer presents the MSISDN entry flow as complete; it collects and
  verifies a number but does not yet finish provisioning
  (`RCC14-AUTH-MSISDN-FLOW`).
- Device identifiers are redacted under any field name, including the OMA-DM
  `DevId`.

## [1.0.0] — 2026-08-30

First release.

### RCS configuration (RCC.14 / RCC.07, OMA-CP)

- HTTP configuration endpoint on `/`, `/config` and `/rcs/config`, with `POST`
  accepted for the OTP step.
- Full query parameter parsing with type and length validation, lower-case alias
  tolerance, repeated `app=` support, and rejection of duplicated identity
  parameters.
- Response semantics: `200` with a document, `200` with an empty body as the OTP
  pending signal, `200` with `VERS` only when the client is current, `400`, `401`
  (GBA), `403`, `429`, `503`, `511`.
- Configuration version semantics for `> 0`, `0`, `-1`, `-2`, `-3`, `-4`, as one
  reviewable table with a citation per row.
- `wap-provisioningdoc` 1.1 generation for `VERS`, `TOKEN`, `MSG`, the IMS
  application (`ap2001`), the RCS application (`ap2002`) with SERVICES, MESSAGING
  (CHAT, FT, StandaloneMsg, MessageStore, Chatbot), IM, CAPDISCOVERY, PRESENCE,
  XDMS, OTHER, TRANSPORTPROTO, APN, and the OMA-DM account (`w7`).
- 116 provisioning parameters declared in YAML with specification references and a
  `verified` flag; profile overlays for UP 2.4, UP 1.0 and joyn blackbird.
- Identity resolution chain: provisioning token, operator header enrichment, GBA,
  SMS OTP, and an accessible MSISDN entry web flow.
- Tokens: 256-bit, stored hashed, IMSI/IMEI bound, expiring, revocable.
- OTP: hashed with the MSISDN, single use, TTL-bounded, attempt-limited,
  cooldown and daily cap per MSISDN.

### Device management (OMA-DM, SyncML DM 1.2)

- `POST /dm` session endpoint with the full package flow and per-command `Status`.
- `Alert`, `Get`, `Replace`, `Add`, `Exec`, `Results`, `Status`; `Alert` 1226 ends a
  session; a missing initial alert is a protocol error.
- `syncml:auth-basic` and `syncml:auth-md5` with a server nonce and `Chal`,
  bootstrapped by the OMA-CP `w7` characteristic.
- Declarative management object tree: DevInfo, DevDetail, the 3GPP IMS MO with the
  VoLTE parameter set, and an RCS extension MO — 47 nodes.
- Device inventory built from `Get`/`Results`, exposed at `GET /admin/devices`.
- Session state in the shared store, TTL-expired.
- `GET /dm/mo` lists the loaded management objects.
- WBXML refused with `415` rather than answered incorrectly.

### AWS

- CloudFormation for ECR and the application: VPC, ALB with optional ACM, ECS
  Fargate, DynamoDB single table with a GSI and TTL, Secrets Manager, CloudWatch
  Logs, target-tracking autoscaling, three alarms.
- `scripts/deploy.sh` — build, push, deploy, wait for health, verify. Refuses
  `--allowed-cidr 0.0.0.0/0`.
- `scripts/teardown.sh` — retains the table, secrets and images, and prints what is
  left.
- CloudWatch metrics through embedded metric format; no `PutMetricData`.
- AWS End User Messaging SMS and Amazon SNS providers; both refuse
  port-addressed delivery rather than downgrading it.

### Security and privacy

- No PII in logs or metric dimensions; uvicorn's access log disabled; a test
  asserts it against captured output.
- Fail-closed defaults, and startup validation that refuses a production-unsafe
  configuration.
- XXE closed on both XML parsers.
- Container: non-root UID 10001, read-only root filesystem, base image pinned by
  digest.

### Verification

- 338 tests, 93% coverage, `mypy --strict` clean, `ruff` clean, `cfn-lint` clean.
- Two client simulators (`tools/rcs_client_sim.py`, `tools/dm_client_sim.py`) that
  exit non-zero on a specification violation.
- `scripts/verify_stack.py` — 32 checks end to end, including harvesting the OMA-DM
  password from the `w7` characteristic and using it for a real DM session.
- Verified against the container with both the in-memory and DynamoDB backends.

### Known limitations

Port-addressed OTP SMS, real GBA, real header enrichment, WBXML,
server-initiated DM sessions, and 91 of 116 OMA-CP parameters not yet
cross-checked against a licensed specification edition. See
[docs/limitations.md](docs/limitations.md).
