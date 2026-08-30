# Architecture decision records

| ADR | Decision |
| --- | --- |
| [0001](0001-stack.md) | Python 3.11 and FastAPI |
| [0002](0002-datastore.md) | DynamoDB as the only production store |
| [0003](0003-declarative-catalogue.md) | Provisioning parameters and management objects declared in YAML |
| [0004](0004-oma-dm-in-the-same-service.md) | The OMA-DM plane lives in the same service, bootstrapped by OMA-CP `w7` |
| [0005](0005-fail-closed-defaults.md) | Fail-closed defaults and startup validation |
| [0006](0006-malformed-request-status.md) | `400` for a malformed configuration request, configurable |
