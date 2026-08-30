# ADR 0004 — The OMA-DM plane lives in the same service

**Status:** accepted

## Context

The requirement was that the ACS be extensible to the OMA-DM specification, so
that VoLTE settings and all devices can be managed later, in addition to the base
RCS configuration function.

OMA-CP (what an RCS ACS speaks) and OMA-DM are different protocols. OMA-CP is a
one-shot HTTP response carrying a `wap-provisioningdoc`. OMA-DM is a multi-round
SyncML session with commands, statuses, authentication and a management tree.

Options considered:

1. One service, two endpoints.
2. Two services sharing a data store.
3. OMA-CP only, with OMA-DM "designed for" but not built.

## Decision

Option 1: one service. `GET /config` serves OMA-CP, `POST /dm` serves OMA-DM, and
the OMA-CP document carries the `w7` characteristic that bootstraps the DM account.

## Rationale

The two planes are not independent — they are a sequence. A device is provisioned
for RCS first, and the standard way it learns about a DM server is the OMA-CP `w7`
bootstrap, which contains the DM server URI and credentials. Splitting the planes
across services means the RCS service must generate and hand over credentials that
the DM service will later verify, so they share subscriber state, secrets and
lifecycle anyway. That is one service with two endpoints, deployed twice.

Keeping them together also makes the chain testable. `verify_stack.py` provisions
over RCC.14, reads `AAUTHSECRET` out of the returned document, and authenticates a
real DM session with it. That end-to-end assertion is the strongest evidence that
the extensibility requirement is actually met rather than merely claimed.

Option 3 was rejected because "designed for" is unverifiable. A YAML schema with no
server behind it does not prove a device can be managed.

## Design points

- **Shared subscriber record.** `Subscriber.dm_password` is generated once at RCS
  provisioning time. `Subscriber.volte_enabled` gates the VoLTE nodes.
- **Shared device record.** `Device` is populated from RCC.14 parameters
  (`terminal_vendor`, `terminal_model`) and from DM `./DevInfo` / `./DevDetail`, so
  `GET /admin/devices` is one inventory rather than two.
- **DM session state in the shared store.** A DM session spans several HTTP
  requests, so it cannot live in process memory: the second request may land on a
  different ECS task. `DmSession` is a store item with a TTL.
- **Independently disableable.** `ACS_DM_ENABLED=false` removes the router and the
  `w7` characteristic, for an operator who wants configuration only.
- **Extension by data.** The MO tree is declarative (ADR 0003), so FUMO, a vendor
  MO or more VoLTE parameters are a YAML file, not a code change. A test asserts
  that contract.

## Consequences

- One deployment, one image, one alarm set.
- The service now owns two protocol implementations, so the protocol code is kept
  in separate packages (`protocol/omacp`, `protocol/omadm`) with no imports between
  them; they meet only in the domain layer and in the `w7` builder.
- WBXML is not implemented, and is refused with `415` rather than answered with XML
  a client cannot decode. Some production DM clients are WBXML-only, so this is the
  first thing to add for real-device work.
