# ADR 0002 — DynamoDB as the only production store

**Status:** accepted; amended by issue #11 (see [Amendment](#amendment-atomic-otp-issue-and-consumption))

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
OTPSEND#<msisdn> <epoch>   send audit for quotas      TTL   (superseded, read once to seed OTPQUOTA#)
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
  *(Superseded by the amendment below.)*
- The mock SMS outbox is readable per MSISDN only; no cross-table listing.
- The table carries `DeletionPolicy: Retain`. Deleting the application stack must
  not delete subscriber data.
- There is no cross-item transaction. Nothing in the current flows needs one; OTP
  consumption is a single-item update.
  *(Superseded by the amendment below: OTP issue now uses one two-item
  transaction.)*

## Amendment: atomic OTP issue and consumption

**Status:** accepted (issue #11)

### Context

The OTP flow read the challenge and the send count, decided, then wrote, in
separate calls. With more than one ECS task sharing the table, and later with the
configuration flow running in a worker thread, concurrent requests for one MSISDN
could all pass the checks: eight concurrent requests under a daily cap of one
sent five to eight SMS, and one code could verify twice. That is the
SMS-pumping and replay exposure the limits exist to prevent.

### Decision

The store offers two atomic operations and no other way to change a challenge:
the unconditional `put_otp` and `delete_otp` are removed.

- `issue_otp` stores the challenge and counts the send unless the cooldown or the
  daily cap applies.
- `replace_otp` is a compare-and-swap on code hash, creation time and attempt
  count. Replacing with nothing deletes, and that conditional delete is how a code
  is consumed. A wrong guess is a conditional put with the attempt count
  incremented, so each concurrent guess spends exactly one attempt. When an SMS
  cannot be sent, the cleanup deletes only the challenge that was issued for it
  (same code hash and creation time), never whatever the MSISDN holds by then:
  the send can take seconds, and a newer challenge may have been issued
  meanwhile.

The send history moves from one row per send to one versioned item:

```
OTPQUOTA#<msisdn> SENDS    send times (24 h) + version  TTL
```

`issue_otp` reads the challenge and the quota item with `ConsistentRead`, decides,
then writes both in one `TransactWriteItems`. Each write is conditioned on its
item being unchanged since the read: `version = :v` on the quota item, the same
code hash, creation time and attempt count on the challenge, or
`attribute_not_exists(pk)` for an item that did not exist. A cancelled
transaction is treated as contention only when every cancellation reason is
`ConditionalCheckFailed`; a request that loses re-reads, and normally then meets
the cooldown. Any other reason (throttling, capacity, validation) is raised as a
fault rather than retried. After five lost rounds issue raises
`OtpStoreContention`, which the service answers with `503` and a short
`Retry-After`, never with the "pending" signal for an SMS that was not sent. A
verification that loses five rounds is not verified.

`MemoryStore` performs each operation under its lock.

### Consequences

- The "no cross-item transaction" consequence above no longer holds for OTP issue.
  `TransactWriteItems` needs no IAM action of its own; it is authorised by the
  per-item `PutItem` already granted on the table.
- Every cap, cooldown and attempt limit holds across tasks, not just within one.
- **Daily cap across the deployment.** When an MSISDN's `OTPQUOTA#` item does
  not exist yet, `issue_otp` seeds it from that MSISDN's `OTPSEND#` rows of the
  last 24 hours, read with the old `Query` on the row timestamps, so the daily
  count carries over the deployment instead of restarting at zero. The old rows
  are never written again and expire by TTL within a day, after which the seed
  finds nothing. The resend cooldown is unaffected because it is read from the
  challenge.
- The quota item keeps a list of send times, not a counter, so the 24-hour window
  stays rolling. The list is bounded by the cap.
- moto applies requests without locking, so the concurrency tests serialise
  moto's request handling to model DynamoDB's per-request atomicity.
