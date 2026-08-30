# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
