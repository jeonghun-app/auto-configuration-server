# ADR 0005 — Fail-closed defaults and startup validation

**Status:** accepted

## Context

An ACS has an unusual failure mode: it can break every handset that talks to it. A
`VERS` of `-2` tells a client to delete its configuration and stop asking. An empty
configuration document switches RCS off. A forged identity header hands one
subscriber's IMS credentials to another. An open OTP endpoint spends the operator's
money.

Most of these are configuration mistakes, not code defects.

## Decision

Every security-relevant setting defaults to the safe value, and the service refuses
to start when the configuration is unsafe for the environment it says it is in.

| Setting | Default | Effect of the default |
| --- | --- | --- |
| `ACS_ADMIN_TOKEN` | empty | Every admin route returns `503`. No default token to guess |
| `ACS_TRUSTED_PROXY_CIDRS` | empty | Header-enrichment identity is off entirely |
| `ACS_GBA_ENABLED` | `false` | No fake bootstrap |
| `ACS_DEV_ENDPOINTS_ENABLED` | `false` | No mock SMS outbox |
| `ACS_PII_LOG_MODE` | `mask` | Identifiers never logged in the clear |
| `ACS_STORE_BACKEND` | `memory` | Safe for a laptop, refused in staging/prod |
| `EnableAlbAccessLogs` | `false` | No bucket of subscriber-identifying request lines |

`Settings.validate_startup()` returns fatal problems and `create_app` raises on
them. In `staging` or `prod` it refuses:

- `store_backend=memory` — OTP state would not be shared between tasks;
- `dev_endpoints_enabled=true`;
- `sms_provider=mock`;
- `pii_log_mode=none`, and `hash` without a secret.

Catalogues are loaded and validated in the lifespan handler, so a malformed
catalogue fails the container rather than degrading to an empty document.

`hash_id()` refuses to hash without a key, because an unkeyed hash of a 15-digit
IMSI is brute-forceable and would give false assurance.

`build_bsf_client()` returns `UnconfiguredBsfClient` in production, which raises,
rather than the mock.

## Rationale

Crashing at startup is loud, immediate and affects one deployment. A service that
boots with a bad configuration is silent and affects every handset. For this
workload the first is strictly better.

Refusing at deploy time follows the same reasoning: `scripts/deploy.sh` refuses
`--allowed-cidr 0.0.0.0/0` and warns when no certificate is supplied.

## Consequences

- A first-time operator has to set `ACS_ADMIN_TOKEN` before the admin API works.
  This is intended, and the `503` response says why.
- Production configuration errors surface as a task that will not start and an
  unhealthy target, not as subtly wrong provisioning. The ECS deployment circuit
  breaker rolls back automatically.
- `tests/test_api_config.py::test_invalid_production_configuration_refuses_to_start`
  pins this behaviour.
