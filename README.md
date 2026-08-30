# GSMA RCS Auto Configuration Server (ACS)

[![CI](https://github.com/jeonghun-app/auto-configuration-server/actions/workflows/ci.yml/badge.svg)](https://github.com/jeonghun-app/auto-configuration-server/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11-blue.svg)](pyproject.toml)

A container-based Auto Configuration Server for GSMA RCS, deployable on AWS with
one command.

Two planes in one service:

| Plane | Protocol | Purpose |
| --- | --- | --- |
| **RCS configuration** | GSMA RCC.14 HTTP flow, OMA Client Provisioning (`wap-provisioningdoc` 1.1) | Provisions RCS clients: IMS identity, messaging, file transfer, capability discovery, chatbots |
| **Device management** | OMA-DM, SyncML DM 1.2 | Manages VoLTE settings and device inventory after bootstrap, and is extended by dropping in a YAML management object |

The two are linked: the OMA-CP document contains the `w7` characteristic that
bootstraps the OMA-DM account, so a device provisioned for RCS can immediately be
managed over DM. That chain is verified end to end by `scripts/verify_stack.py`.

Korean version of this document: [README.ko.md](README.ko.md).

---

## Quick start

```bash
make install                 # create .venv and install dependencies
make test                    # 338 tests
make docker-run              # build and run the container on :8080
make verify                  # end-to-end check of both planes
```

Expected result:

```
18 checks passed, 0 failed     # RCS auto-configuration (RCC.14 / OMA-CP)
14 checks passed, 0 failed     # OMA-DM device management (SyncML DM 1.2)
RESULT: PASS — both planes behave as specified
```

The full local stack, using the same DynamoDB code path as production:

```bash
make up                      # ACS + amazon/dynamodb-local
make verify
make down
```

## Deploy to AWS

Every backing service is an AWS managed service.

| Concern | Service |
| --- | --- |
| Compute | ECS Fargate |
| Ingress and TLS | Application Load Balancer + ACM |
| State | DynamoDB (single table, TTL-expired OTP challenges and DM sessions) |
| Secrets | Secrets Manager (admin token, PII hash key) |
| Logs | CloudWatch Logs (structured JSON) |
| Metrics | CloudWatch, via embedded metric format on stdout — no `PutMetricData` |
| SMS | AWS End User Messaging SMS, or Amazon SNS |
| Registry | ECR (immutable tags, scan on push) |
| Infrastructure | CloudFormation |

```bash
scripts/deploy.sh \
  --allowed-cidr 203.0.113.10/32 \
  --certificate-arn arn:aws:acm:ap-northeast-2:123456789012:certificate/abc123
```

The script builds the image, pushes it to ECR, deploys both stacks, waits for
health, and runs the end-to-end verification against the live load balancer.
`scripts/teardown.sh` removes it again; the DynamoDB table, secrets and images are
retained on purpose.

`--allowed-cidr` is mandatory and `0.0.0.0/0` is refused. The ACS receives IMSI,
IMEI and MSISDN in query strings and exposes an OTP endpoint that costs real money
to abuse, so it should not be world-reachable until you intend handsets to reach
it. Details and the private-subnet hardening variant: [docs/aws-deployment.md](docs/aws-deployment.md).

## How the RCC.14 flow works

```
        client                                   ACS
          │  GET /config?vers=0&IMSI=…&IMEI=…      │
          ├───────────────────────────────────────►│  identity unproven
          │                                        │  → generate OTP, send SMS
          │◄───────────────────────────────────────┤  200 OK, Content-Length: 0
          │                                        │
          │  GET /config?vers=0&IMSI=…&OTP=142760  │
          ├───────────────────────────────────────►│  OTP verified and consumed
          │◄───────────────────────────────────────┤  200 OK + wap-provisioningdoc
          │                                        │     VERS/version = 1
          │  (validity expires)                    │     TOKEN, ap2001, ap2002, w7
          │  GET /config?vers=1&token=…            │
          ├───────────────────────────────────────►│  client already current
          │◄───────────────────────────────────────┤  200 OK + VERS only
```

Status codes:

| Code | Meaning |
| --- | --- |
| `200` with an XML body | Configuration delivered |
| `200` with `Content-Length: 0` | OTP sent; repeat the identical request adding `OTP=` |
| `200` with only `VERS` | The client already holds the current revision |
| `401` + `WWW-Authenticate: Digest … AKAv1-MD5` | GBA bootstrap challenge (off by default) |
| `403` | Subscriber known but not entitled |
| `429` + `Retry-After` | OTP rate limit |
| `503` + `Retry-After` | Temporarily unable to serve |
| `511` | Cannot identify the subscriber; retry over the mobile network or use the MSISDN entry flow |

Configuration versions are operational instructions, not just revisions:

| `VERS/version` | Client action |
| --- | --- |
| `> 0` | Valid revision; store and apply |
| `0` | Configuration invalid, RCS off; re-query only on a trigger |
| `-1` | Disable, delete configuration, re-query at the next trigger |
| `-2` | Disable, delete configuration, do not re-query until factory reset or SIM swap |
| `-3` | Dormant: keep configuration, retry after `validity` |
| `-4` | Permanently barred from provisioning |

The whole mapping lives in one reviewable table in
[`src/acs/protocol/vers.py`](src/acs/protocol/vers.py), because the interpretation
of `-1` to `-4` differs between RCC.14 releases and vendors and is the single most
commonly misimplemented part of an ACS.

## Specification coverage, stated honestly

Provisioning parameters are **declared in YAML, not coded**:

```yaml
- path: APPLICATION:ap2002/MESSAGING/FT
  parm: MaxSizeFileTr
  type: int
  unit: KB
  default: "10240"
  spec: RCC.07 A.1.4 FT
  verified: false
```

[`docs/spec-coverage.md`](docs/spec-coverage.md) is generated from those files, so
the repository can state exactly what it covers instead of claiming compliance it
cannot prove:

| Surface | Entries | Cross-checked against a pinned spec edition |
| --- | --- | --- |
| OMA-CP parameters | 116 | 25 |
| OMA-DM nodes | 47 | 23 |
| Version semantics rows | 5 | 1 |

`verified: false` means the entry is implemented from public descriptions of
RCC.07/RCC.14 and from configurations widely deployed in the field. It is
structurally correct, typed, rendered and tested — but this project makes **no
claim of GSMA certification**. RCC.07 and RCC.14 are licensed documents; pin the
edition you hold in [`docs/scope.md`](docs/scope.md) and flip `verified: true` as
you check entries off. A single-line YAML edit is all a correction takes.

Adding a parameter, an operator profile, or an entire managed service is a data
change. `GET /admin/coverage` reports the same numbers at runtime.

## Extending to OMA-DM and VoLTE

The DM plane is driven by management object definitions in
`src/acs/catalog/omadm/`:

| File | URN | Contents |
| --- | --- | --- |
| `01-devinfo.yaml` | `urn:oma:mo:oma-dm-devinfo:1.0` | Device identity |
| `02-devdetail.yaml` | `urn:oma:mo:oma-dm-devdetail:1.0` | Firmware, software, URI limits |
| `03-3gpp-ims.yaml` | `urn:oma:mo:ext-3gpp-ims:1.0` | IMS + VoLTE (voice domain preference, SMSoIP, ICSI, AMR-WB, ViLTE, single registration) |
| `04-rcs-ext.yaml` | `urn:acs:mo:rcs-ext:1.0` | RCS service switches, changeable without a full re-provision |

A node declares who owns it: `source: device` nodes are read with `Get` to build a
device inventory, `source: server` nodes are pushed with `Replace`. Adding
firmware update (FUMO), a vendor MO, or more VoLTE parameters means adding a YAML
file — no server code changes. `GET /dm/mo` lists what is loaded.

See [docs/oma-dm.md](docs/oma-dm.md).

## API surface

| Method and path | Purpose |
| --- | --- |
| `GET /`, `/config`, `/rcs/config` | RCC.14 configuration request (deployed clients differ on the path) |
| `POST` on the same paths | OTP step for clients that avoid putting the OTP in a query string |
| `POST /dm` | OMA-DM SyncML session |
| `GET /dm/mo` | Loaded management objects |
| `GET /msisdn`, `POST /msisdn`, `POST /msisdn/verify` | MSISDN entry web flow used after a `511` |
| `GET /healthz` | Liveness — no AWS calls, so a dependency hiccup cannot deregister healthy tasks |
| `GET /readyz` | Readiness — store reachability and catalogue integrity |
| `GET/PUT/DELETE /admin/subscribers…` | Subscriber administration |
| `POST /admin/subscribers/{imsi}/invalidate\|disable\|enable\|revoke-tokens` | Operational actions |
| `GET /admin/devices` | Device inventory from RCC.14 parameters and DM DevInfo |
| `GET /admin/coverage` | Live specification coverage |
| `GET /dev/sms` | Mock SMS outbox — development only, refused in staging and production |

The admin API **fails closed**: with `ACS_ADMIN_TOKEN` unset, every admin route
returns `503`. There is no default token to guess.

## Configuration

All settings come from `ACS_`-prefixed environment variables; see
[.env.example](.env.example) for the full list. The defaults that matter:

| Variable | Default | Why |
| --- | --- | --- |
| `ACS_ADMIN_TOKEN` | *(empty)* | Admin API disabled until set |
| `ACS_TRUSTED_PROXY_CIDRS` | *(empty)* | Header-enrichment identity disabled; an identity header from an untrusted peer is forgeable |
| `ACS_GBA_ENABLED` | `false` | GBA needs a real BSF; enabled in production without one, the service fails closed rather than faking a bootstrap |
| `ACS_DEV_ENDPOINTS_ENABLED` | `false` | The mock SMS outbox must never exist in production |
| `ACS_PII_LOG_MODE` | `mask` | Subscriber identifiers are never logged in the clear |
| `ACS_STORE_BACKEND` | `memory` | Refused in staging/prod: in-memory OTP state breaks the moment there is more than one task |

Startup validation refuses to boot on a configuration that would be unsafe for
the declared environment. A misconfigured ACS that starts anyway can switch RCS
off on every handset that talks to it.

## Security and privacy

This service handles IMSI, IMEI and MSISDN. Design decisions that follow from
that:

- **No PII in logs.** Identifiers are masked or HMAC-pseudonymised, uvicorn's
  access log is disabled (its log line contains the whole query string), and a
  test asserts no raw identifier appears in captured output.
- **No PII in metric dimensions.** Unbounded cardinality would be both a leak and
  a CloudWatch bill.
- **ALB access logs off by default.** An RCC.14 request line contains IMSI, IMEI,
  MSISDN, OTP and token; enabling them creates a bucket of subscriber data.
- **OTP abuse controls.** Per-MSISDN cooldown, daily cap, bounded verification
  attempts, single use, constant-time comparison, and a CloudWatch alarm on send
  volume.
- **Tokens hashed at rest**, bound to IMSI and IMEI, individually revocable.
- **No subscriber enumeration.** Known and unknown identities get the same
  response shape.
- **XXE closed** on both XML parsers; documents are built with `lxml`, never
  string templates.
- **Container hardening.** Non-root UID 10001, read-only root filesystem, base
  image pinned by digest, no shell for the service user.

See [SECURITY.md](SECURITY.md) and [docs/threat-model.md](docs/threat-model.md).

## What this project cannot do

Stated plainly, because an ACS that pretends is worse than one that admits:

| Item | Why | What is here instead |
| --- | --- | --- |
| Port-addressed (silent) OTP SMS | Needs a UDH-capable operator SMSC over SMPP. No AWS SMS service can send it. | The interface carries `SMS_port` end to end, the AWS providers refuse rather than downgrade, and `SmppSmsSender.build_udh()` implements the header |
| Real GBA / AKA | Needs a USIM, a BSF over Ub and Zn, and an HSS | The HTTP challenge/response shape, a `BsfClient` port, and a deterministic mock |
| Real operator header enrichment | Needs an operator packet gateway | Trusted-proxy gated header, off by default |
| Being reachable at `config.rcs.mncXXX.mccYYY.pub.3gppnetwork.org` | That DNS zone is operator and GSMA controlled | The deploy output tells you the CNAME to create |
| A real handset | Not available in CI | Two protocol-correct client simulators that fail the build on a spec violation |

Full list: [docs/limitations.md](docs/limitations.md).

## Repository layout

```
src/acs/
  app.py                     FastAPI application factory
  config.py                  settings, with fail-closed defaults
  observability.py           JSON logging, PII redaction, CloudWatch EMF metrics
  api/                       HTTP routers
  auth/                      tokens, SMS OTP, header enrichment, GBA
  domain/                    entities and the RCC.14 decision flow
  protocol/
    request.py               RCC.14 query parsing
    vers.py                  configuration version semantics
    omacp/                   OMA-CP catalogue, builder, XML writer
    omadm/                   SyncML DM parser, MO tree, session state machine
  catalog/omacp/             provisioning parameters (YAML)
  catalog/omadm/             management objects (YAML)
  sms/                       AWS End User Messaging, SNS, SMPP stub, mock
  store/                     DynamoDB and in-memory backends
tools/                       RCS and OMA-DM client simulators
scripts/                     deploy, teardown, verify, seed, coverage generator
infra/                       CloudFormation (ECR and application stacks)
tests/                       338 tests
docs/                        scope, protocol, OMA-DM, AWS, limitations, ADRs
```

## Verification

```bash
make check      # lint + mypy strict + tests with coverage gate + cfn-lint
                # + shellcheck + spec-coverage freshness
```

Current state, measured on this repository:

| Check | Result |
| --- | --- |
| `pytest` | 338 passed |
| Coverage | 93% |
| `mypy --strict` | clean, 46 source files |
| `ruff` (lint + format) | clean |
| `cfn-lint` | clean |
| Container | builds, runs as UID 10001, `/healthz` 200 |
| End-to-end (in-memory backend) | 32 checks passed |
| End-to-end (DynamoDB backend, containerized) | 32 checks passed |

## Documentation

| Document | Contents |
| --- | --- |
| [docs/scope.md](docs/scope.md) | Agreed scope: in, out, and what cannot be done |
| [docs/protocol.md](docs/protocol.md) | Wire-level RCC.14 behaviour |
| [docs/oma-dm.md](docs/oma-dm.md) | DM session flow and how to add a management object |
| [docs/spec-coverage.md](docs/spec-coverage.md) | Generated coverage report |
| [docs/aws-deployment.md](docs/aws-deployment.md) | Deployment, cost, hardening |
| [docs/limitations.md](docs/limitations.md) | Everything this cannot do |
| [docs/threat-model.md](docs/threat-model.md) | Assets, attackers, mitigations |
| [docs/runbook.md](docs/runbook.md) | Operational procedures |
| [docs/adr/](docs/adr/) | Architecture decision records |

## License

Apache-2.0. See [LICENSE](LICENSE).

RCC.07, RCC.14 and the OMA specifications are the property of the GSMA and the
Open Mobile Alliance. This repository contains no specification text.
