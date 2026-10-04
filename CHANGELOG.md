# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.4.0] — 2026-10-04

Two of the limitations recorded since 1.0.0 are lifted — WBXML-encoded DM
sessions and port-addressed OTP over an operator SMSC — and the OTP limits now
hold under concurrency. The dependencies are brought up to date, including a
python-multipart release that fixes seven published advisories, and releases are
now published from a tag, with a container image on GHCR.

### Added

- **WBXML SyncML DM sessions.** `POST /dm` accepts
  `application/vnd.syncml.dm+wbxml` and answers in the encoding and WBXML version
  the request arrived in, including authentication challenges. Some production DM
  clients speak only WBXML, and until now they got `415`. A bounded,
  standard-library WBXML 1.2/1.3 codec converts to and from the XML the session
  code already handles, so both encodings share one parser, one authentication
  path and one state machine, and a session may switch encoding between requests.
  The codec is deliberately narrow: the SyncML and MetInf code pages, UTF-8,
  inline and string-table strings, and `OPAQUE` only when it holds UTF-8 text.
  Attributes, literal tags, extensions, `ENTITY`, processing instructions, other
  code pages and binary `OPAQUE` are refused with `400` rather than guessed at.
  Every length, depth and element count is a named constant, and a violation is a
  `4xx`, never a `500`. See [docs/oma-dm.md](docs/oma-dm.md#wbxml-encoding).
- The WBXML response is compact: XML indentation is not carried into it as inline
  strings, which made up 63% of an init response and sit where the DTD allows only
  elements. Leaf values keep their whitespace.
- `tools/dm_client_sim.py --wbxml` runs the same DM session over WBXML.
- **Port-addressed (silent) OTP over SMPP 3.4.** `ACS_SMS_PROVIDER=smpp` sends the
  OTP through an operator SMSC: one `bind_transceiver` / `submit_sm` / `unbind`
  session per message, not pooled, because OTPs are rare and must not depend on a
  connection surviving across tasks. With `SMS_port` the message is 8-bit data
  with UDHI set and the 16-bit application port header ahead of the OTP template;
  a text OTP uses the GSM default alphabet only when that is byte-for-byte safe,
  otherwise UCS-2. A message longer than one SMS is refused, not concatenated. It
  has been exercised **only against an in-process fake SMSC**, never an operator
  SMSC or a handset, so `RCC14-AUTH-OTP-PORT` is `partial`, not implemented. The
  AWS providers still refuse `SMS_port` rather than downgrade it.
- `ACS_SMPP_*` settings: host, port (1–65535), `system_id`, password (held as a
  secret, never logged), `system_type`, source address and TON/NPI, destination
  TON/NPI, TLS (**on by default**, because SMPP sends the password in clear), a CA
  bundle for an operator private CA, and a per-response timeout above 0 and at most
  60 seconds — capped because the request waits on it. Selecting `smpp` without a
  host, `system_id` and password refuses to start in staging and production, and
  a password longer than 8 characters or outside printable ASCII refuses to start
  in any environment; a trailing newline from a secret file is stripped. See
  [.env.example](.env.example).
- CloudFormation `SmsProvider=smpp` with `SmppHost`, `SmppPort`, `SmppSystemId`,
  `SmppTls` and `SmppSmscCidr`. A template rule refuses `smpp` without a host,
  `system_id` and SMSC range; the tasks gain one egress rule, to that range on the
  SMPP port only; the password is a stack secret (`SmppPasswordSecretArn` output)
  injected through ECS Secrets. `scripts/deploy.sh` gains `--smpp-host`,
  `--smpp-port`, `--smpp-system-id`, `--smpp-cidr` and `--smpp-tls`, and never
  takes the password as a flag.
- Metrics `OtpDeliveryFailed`, `OtpStoreContention` and `DmEncodingError`.
- **Releases from a tag.** Pushing `vX.Y.Z` runs `make check`, refuses a tag that
  differs from the declared version, has no CHANGELOG section or is not on `main`,
  then publishes `ghcr.io/jeonghun-app/auto-configuration-server` for
  `linux/amd64` and `linux/arm64` with an SBOM and provenance, and creates the
  GitHub Release from this file. `X.Y.Z` is never overwritten; `X.Y` and `latest`
  only move forward. `scripts/release_notes.py` runs the same checks locally.
  Procedure and rollback: [docs/releasing.md](docs/releasing.md).
- A code of conduct ([CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), Contributor
  Covenant 2.1), and issue forms in place of blank issues. The bug form warns
  against pasting real IMSI, IMEI, MSISDN, OTPs and tokens, which a blank issue
  invites; the specification-correction form asks for the edition and clause and
  a confirmation that no licensed text was pasted. Security reports are routed to
  private advisories, and [SECURITY.md](SECURITY.md) says where that form is.

### Changed

- **The admin JSON API requires a JSON `Content-Type`.** FastAPI 0.132 and later
  check it: `PUT /admin/subscribers/{imsi}` with a JSON body and no
  `Content-Type`, or `text/plain`, now answers `422` and stores nothing, where
  1.3.0 answered `200`. Kept on purpose — the API parses a body as JSON only when
  the client says it is JSON — and pinned by a test.
- **`;` no longer separates URL-encoded form fields.** python-multipart 0.0.30 and
  later split only on `&`, which closes a smuggling and a quadratic-parsing
  advisory (see Security). This affects every form body: the console login, the
  MSISDN entry and OTP forms, and the `POST` variant of the configuration
  request. Every client in the repository already uses `&`.
- An SMS delivery failure answers `503` with `detail=otp_delivery_failed`,
  `Retry-After: 60` and the metric `OtpDeliveryFailed`, and deletes the challenge.
  A provider that cannot send the requested mode keeps `OtpDeliveryUnsupported`
  with `Retry-After: 3600`: an SMSC outage is usually transient, and a one-hour
  wait would be the wrong instruction to the client.
- The configuration flow runs in a worker thread, so a slow SMSC session (up to its
  timeout) no longer stalls every other request on the task.
- The `/dm` body is read as a stream and cut off at 512 KiB, so an oversized
  request is refused before it is buffered.
- OTP send history moves from one DynamoDB row per send (`OTPSEND#<msisdn>`) to
  one versioned item (`OTPQUOTA#<msisdn>`/`SENDS`), and each challenge carries a
  random `challenge_id`. The new item is seeded from the last 24 hours of
  `OTPSEND#` rows, so daily counts carry over the upgrade, and 1.4.x keeps writing
  the legacy row as well so that 1.3 tasks still count its sends; removing that
  dual write is tracked in #28. No migration step is needed, and no new IAM action.
  See the amendment to [ADR 0002](docs/adr/0002-datastore.md).
- `IdentityMethod`, `IdentityDecision` and `VersAction` derive from
  `enum.StrEnum`. Every rendered value already used `.value`, so no log line,
  metric dimension or document changes.
- Dependencies: fastapi 0.115.6 → 0.141.1 (Starlette 0.41.3 → 1.7.0), uvicorn
  0.34.0 → 0.54.0, pydantic 2.10.4 → 2.13.5, pydantic-settings 2.7.1 → 2.15.0,
  lxml 5.3.0 → 6.1.3, boto3 1.35.90 → 1.43.106, PyYAML 6.0.2 → 6.0.3,
  python-multipart 0.0.20 → 0.0.32. Development tools: pytest 9, pytest-cov 7,
  ruff 0.16, mypy 2.3, cfn-lint 1.57, moto 5.2. CI actions: checkout,
  setup-python and upload-artifact 7, each still pinned to a commit SHA.
- A configuration refused at startup still stops the container with exit status
  1, as in 1.3.0, measured in the container. `create_app` raises while uvicorn
  loads it, so uvicorn 0.50's exit status 3, which it uses for its own startup
  failures such as a port that cannot be bound, does not apply.
- The base image digest moves to the current Python 3.11 patch build (3.11.17).
  The interpreter stays 3.11, and Dependabot no longer proposes a new Python minor
  as an image bump: the interpreter moves with `requires-python`, CI, mypy, ruff
  and the Makefile together, or not at all.

### Fixed

- **Concurrent OTP requests could exceed the daily cap, and one code could verify
  twice.** The OTP flow read the challenge and the send count, decided, and wrote
  back in separate calls. That was reachable across ECS tasks sharing the table:
  eight concurrent requests under a daily cap of one sent five to eight SMS. The
  store now offers two atomic operations and nothing else changes a challenge —
  `issue_otp` (a conditional `TransactWriteItems` on DynamoDB) and `replace_otp`
  (a compare-and-swap; consuming a code is a conditional delete) — so each
  concurrent wrong guess spends exactly one attempt, and a code verifies once.
  After five lost rounds an issue answers `503` with `Retry-After: 5` and the
  metric `OtpStoreContention`, never the "pending" signal for an SMS that was not
  sent; a verification that loses five rounds is not verified. DynamoDB throttling,
  capacity and validation errors are now raised, where every cancelled transaction
  used to be retried as contention and could end as `200` pending with no SMS sent.
- A failed SMS send deleted whichever challenge the MSISDN held by then. The send
  can take seconds, so a late failure could delete a newer challenge whose code
  was already on its way. Only the challenge issued for that send is deleted now.
- The MSISDN entry web flow left the challenge in place when delivery failed,
  holding the resend cooldown for a code that never left. It is deleted, as in the
  RCC.14 flow, and the page is unchanged, so it still does not reveal which
  numbers exist.
- A DM credential containing non-ASCII characters raised during comparison instead
  of failing as a mismatch.

### Security

- **python-multipart 0.0.20 → 0.0.32.** 0.0.20 is affected by seven published
  advisories, all fixed by 0.0.31: GHSA-wp53-j4wj-2cfg (CVE-2026-24486, high,
  arbitrary file write in a non-default configuration), GHSA-mj87-hwqh-73pj
  (CVE-2026-40347, medium, DoS through a large preamble or epilogue),
  GHSA-pp6c-gr5w-3c5g (CVE-2026-42561, high, DoS through unbounded part headers),
  GHSA-5rvq-cxj2-64vf (CVE-2026-53539, high, quadratic query-string parsing with
  `;`), GHSA-6jv3-5f52-599m (CVE-2026-53538, low, `;` field-separator smuggling),
  GHSA-vffw-93wf-4j4q (CVE-2026-53537, low, RFC 2231 parameter smuggling) and
  GHSA-v9pg-7xvm-68hf (CVE-2026-53540, low, negative `Content-Length` buffering).
  Starlette parses every form body with it, so the URL-encoded CPU DoS was
  reachable without authentication.
- **lxml 6.1.3.** 6.1.3 fixes external parameter entities being parsed by
  default when `resolve_entities="internal"` (LP#2165901), and 6.1 carries the
  upstream XXE fixes for `iterparse`. Neither path was reachable here: both lxml
  parsers set `resolve_entities=False`, `load_dtd` and `no_network` explicitly,
  and a new test puts an entity reference in element text to show it stays
  unexpanded. Taken anyway, so a later change to a parser option cannot reopen
  them.
- The SMSC's `message_id` is untrusted and could carry the MSISDN or the OTP. It
  is logged and audited only as a keyed digest (`smpp-<16 hex>`) whose key exists
  only in the process. SMPP errors name a `command_status` only from a fixed table
  of SMPP 3.4 codes, and never quote sequence numbers, lengths or command ids,
  which an SMSC carrying the OTP chooses freely.
- With `SmsProvider=smpp` the tasks no longer hold `sms-voice:SendTextMessage` and
  `sns:Publish`, since they never call AWS SMS.
- The WBXML codec bounds integers, nesting (64 levels), elements (16,384), input
  (512 KiB) and decoded size (2 MiB), the last of which also bounds expansion
  through repeated string-table references.
- FastAPI is held below 0.142. 0.142 adds OpenTelemetry instrumentation that, once
  an SDK is installed, records `url.query` on every span with only cloud signature
  keys redacted — IMSI, IMEI, MSISDN, OTP and the provisioning token would leave
  the process. It is taken when `create_app` switches that telemetry off with a
  test that proves it (#29).
- Header-enrichment trust is pinned behind uvicorn's proxy-header rewrite: two
  contract tests run the app inside uvicorn's own `ProxyHeadersMiddleware` with the
  image's settings, and fail if trust is decided on the caller-controlled
  left-most `X-Forwarded-For` entry instead of the one the load balancer appended.

### Upgrade notes

This is a minor release although two kinds of request are now refused, because
only a request that never followed the documented contract — a JSON body without a
JSON `Content-Type`, or `;` as a form-field separator — is refused, and every
client in the repository already follows it; see
[docs/releasing.md](docs/releasing.md#versioning).

- **Admin API clients must send `Content-Type: application/json`** with JSON
  bodies, or get `422`. The console is unaffected.
- **Form bodies must separate fields with `&`.** A `;` is now part of the
  preceding field's value, so `vers=0;IMSI=…` arrives as one field. Check any
  operator tooling that posts to the console or to the configuration endpoint.
- **New metrics** in the `RcsAcs` namespace, with no alarm in the stack: add one
  if you want to be paged. `OtpDeliveryFailed` is an SMS provider or SMSC failure;
  `OtpStoreContention` is OTP issue losing five conditional-write rounds, and
  sustained values mean one MSISDN is being hammered or the table is unhealthy;
  `DmEncodingError` is the server failing to encode its own WBXML response, a
  server fault that leaves the session and device state unchanged. See
  [docs/runbook.md](docs/runbook.md).
- **Port-addressed OTP over SMPP is opt-in** and is not proven against a real
  SMSC. To enable it, deploy with `--sms-provider smpp --smpp-host <host>
  --smpp-system-id <id> --smpp-cidr <range>` (and `--smpp-port`, `--smpp-tls
  false` only on a private link). The stack creates the password secret with a
  random 8-character placeholder, so every OTP answers `503` until you put the
  operator-issued password into the secret named by `SmppPasswordSecretArn` and
  force a new deployment of the service; secrets are read at task start. Rotate it
  the same way. A value longer than 8 characters or outside printable ASCII stops
  the new tasks at startup, and the deployment circuit breaker rolls back to the
  running ones. Test with the operator's SMSC and a real client before relying on
  it: the user data after the port header is the configured OTP template, and the
  content an RCS client expects there is not taken from a pinned RCC.14 edition.
- **`scripts/deploy.sh` passes the new `Smpp*` parameters on every run**, like the
  existing ones, so a later deploy without the `--smpp-*` flags resets them to
  their defaults. Repeat them on every deploy and rollback of an SMPP stack.
- **The OTP limits are atomic only once every task runs 1.4.0.** During a rolling
  deployment the 1.3 tasks keep their non-atomic check; the dual-written
  `OTPSEND#` rows let them see the 1.4 tasks' sends, but concurrent requests that
  land on 1.3 tasks can still exceed the cap until the rollout completes. Rolling
  back to 1.3.0 keeps the daily counts for the same reason; 1.3 ignores the new
  `challenge_id` and `OTPQUOTA#` item.
- **The container image is now on GHCR**:
  `docker pull ghcr.io/jeonghun-app/auto-configuration-server:1.4.0` (pin the
  digest printed in the release notes). `scripts/deploy.sh` still builds the
  checked-out tree and pushes to its own ECR repository; it does not pull from GHCR.
- No stored record needs migrating and no `ACS_*` setting changes meaning. The
  supported Python is still 3.11.

### Deliberately not done

- **No SMS concatenation.** An OTP never needs more than one SMS, and a split
  port-addressed message is one more thing a client can reassemble wrongly.
- **No pooled SMPP bind.** OTPs are rare, and delivery must not depend on a
  long-lived connection surviving across tasks; a short session per OTP does not.
- **No general WBXML.** Binary `OPAQUE` and the other constructs above are
  refused, because a guessed decoding would hand the session code a value the
  device never sent.
- **No Python 3.14 base image** (Dependabot #2 declined). Production would run an
  interpreter CI never tests.

### Known limitations

- **DM responses are not split to fit the client's `MaxMsgSize`**, in either
  encoding (#20). This predates 1.4.0; the WBXML encoder's output limits are
  resource bounds, not message splitting.
- Port-addressed OTP over SMPP has not met a real SMSC or handset, and
  server-initiated DM sessions are still not possible: the SMPP client sends only
  the OTP, not a DM notification. See [docs/limitations.md](docs/limitations.md).

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

[Unreleased]: https://github.com/jeonghun-app/auto-configuration-server/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/jeonghun-app/auto-configuration-server/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/jeonghun-app/auto-configuration-server/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/jeonghun-app/auto-configuration-server/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/jeonghun-app/auto-configuration-server/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/jeonghun-app/auto-configuration-server/releases/tag/v1.0.0
