# ADR 0002 — DynamoDB as the only production store

**Status:** accepted

## Context

The ACS persists subscribers, short-lived OTP challenges, provisioning tokens, a
device inventory and OMA-DM session state. The requirement was that every backing
service be an AWS managed service.

Access patterns are almost entirely key lookups:

- subscriber by IMSI, and by MSISDN;
- OTP challenge by MSISDN;
- token by digest;
- device by device id;
- DM session by session id;
- all tokens for an IMSI (revocation);
- bounded listings for the admin API.

## Decision

A single DynamoDB table with one global secondary index. `MemoryStore` remains for
development and unit tests, and is refused in staging and production at startup.

```
SUB#<imsi>       META      subscriber record          gsi1pk=ENTITY#subscriber
MSISDN#<msisdn>  SUB       reverse index -> imsi
OTP#<msisdn>     CHAL      pending challenge          TTL
OTPSEND#<msisdn> <epoch>   send audit for quotas      TTL
TOKEN#<sha256>   META      token                      gsi1pk=TOKENIMSI#<imsi>, TTL
DEV#<device_id>  META      managed device             gsi1pk=ENTITY#device
DMSESS#<sid>     META      DM session state           TTL
SMS#<msisdn>     <epoch>   mock outbox (dev only)     TTL
```

## Rationale

- Every access pattern is a `GetItem` or a bounded `Query`. No joins, no scans.
- TTL on `expires_at` expires OTP challenges and DM sessions for free. With a
  relational store this would be a cleanup job that can fail silently and leave
  challenges valid forever.
- No VPC database means no subnet group, no NAT gateway, no idle instance cost, and
  no migration tooling.
- On-demand billing matches the traffic shape, which is bursty: fleet reboots and
  validity-expiry storms, then near silence.
- `moto` makes the real code path testable in CI, and `amazon/dynamodb-local`
  makes it testable in `docker compose`. The containerised end-to-end run uses the
  DynamoDB backend, not the in-memory one.

## Alternatives

**RDS PostgreSQL** — better for ad-hoc queries and multi-item transactions.
Rejected: it forces private subnets plus a NAT gateway or RDS Proxy, adds Alembic
migrations, and costs money while idle. None of the access patterns need SQL.

**SQLite** — fine for one process. Rejected outright: with `desiredCount >= 2` an
OTP issued by one task is invisible to the next, which is a latent bug that only
appears under load. The in-memory store has the same flaw, which is why startup
validation refuses it outside development.

## Consequences

- `list_subscribers` and `list_devices` use the GSI with a bounded `Limit` rather
  than a `Scan`. They are administrative conveniences, not query APIs.
- OTP quota counting is a `Query … Select=COUNT` over a per-MSISDN partition.
- The mock SMS outbox is readable per MSISDN only; no cross-table listing.
- The table carries `DeletionPolicy: Retain`. Deleting the application stack must
  not delete subscriber data.
- There is no cross-item transaction. Nothing in the current flows needs one; OTP
  consumption is a single-item update.
