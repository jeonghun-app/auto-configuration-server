# ADR 0002 — DynamoDB as the only production store

**Status:** accepted; amended by issue #11 (see
[Amendment](#amendment-atomic-otp-issue-and-consumption)) and issue #28 (see
[Amendment](#amendment-legacy-send-rows-retired))

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
OTPSEND#<msisdn> <epoch>   send audit for quotas      TTL   (replaced by OTPQUOTA#; not written since #28)
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
  *(Superseded by the amendment below: OTP issue now uses one transaction over
  the challenge and the quota item.)*

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
- `replace_otp` is a compare-and-swap on the challenge's identity and attempt
  count. Identity is a random `challenge_id` assigned at issue; a code and a
  creation second are not enough, because the same code can be drawn twice
  within one second. A challenge stored by 1.3 has no id, so for it identity
  falls back to code hash and creation time. 1.3 ignores the new attribute on
  read (its `from_item` keeps only declared fields), and drops it when it
  rewrites a challenge, which the fallback also covers. Replacing with nothing deletes, and that conditional delete is how a code
  is consumed. A wrong guess is a conditional put with the attempt count
  incremented, so each concurrent guess spends exactly one attempt. When an SMS
  cannot be sent, the cleanup deletes only the challenge that was issued for it
  (same identity, whatever its attempt count), never whatever the MSISDN holds by then:
  the send can take seconds, and a newer challenge may have been issued
  meanwhile.

The send history moves from one row per send to one versioned item:

```
OTPQUOTA#<msisdn> SENDS    send times (24 h) + version  TTL
```

`issue_otp` reads the challenge and the quota item with `ConsistentRead`, decides,
then writes both in one `TransactWriteItems`. Each write is conditioned on its
item being unchanged since the read: `version = :v` on the quota item, the same
identity and attempt count on the challenge, or
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
- **Daily cap across the deployment.** *(Superseded by the amendment for #28
  below: the quota item is the only source.)* `issue_otp` merges the quota's
  send history with that MSISDN's `OTPSEND#` rows of the last 24 hours, read with a
  consistent `Query` on the row timestamps. This seeds a missing quota item and
  includes legacy sends made after it was created, so the daily count carries
  over the deployment instead of restarting at zero. The resend cooldown is
  unaffected because it is read from the challenge.
- **Mixed versions.** *(Superseded by the amendment for #28 below: no legacy
  row is written or read.)* The atomic limit holds once every task runs 1.4.0;
  during a rolling deployment the 1.3 tasks' non-atomic check still applies, and
  dual-writing lets them see the new tasks' sends. While 1.4.x is current,
  `issue_otp` also writes the legacy `OTPSEND#<msisdn>` row (1.3's key,
  attributes and TTL) as a third `Put` in the same transaction, so 1.3's `Query`
  counts sends made by 1.4 tasks. Conversely, every 1.4 quota check queries the
  legacy rows even when the quota item exists. A legacy timestamp already in
  the quota is not counted again; distinct sends recorded in the quota within
  the same second still each count. A 1.3 write racing after the query is not
  protected by the quota version condition, so the mixed deployment still
  cannot guarantee the atomic cap. No new IAM action is needed. Removing the
  legacy reads and dual writes once no 1.3 task can run is tracked in #28.
- The quota item keeps a list of send times, not a counter, so the 24-hour window
  stays rolling. The list is bounded by the cap.
- moto applies requests without locking, so the concurrency tests serialise
  moto's request handling to model DynamoDB's per-request atomicity.

## Amendment: legacy send rows retired

**Status:** accepted (issue #28)

### Context

1.4.x kept the `OTPSEND#<msisdn>` rows alive for one purpose: a rolling
deployment from 1.3, whose tasks count the daily cap from those rows. Every 1.4
send wrote one as a third `Put` in the issue transaction, and every 1.4 quota
check queried them. 1.4.0 is released, so after it no deployment needs to run a
1.3 task next to a current one, and the rows only cost a `Query` per check, a
third transactional write per send, and a second source the cap could be
counted from.

### Decision

`issue_otp` reads and writes only the challenge and `OTPQUOTA#<msisdn>`. It
neither writes nor queries `OTPSEND#` rows, and `MemoryStore` keeps a single send
list as before. The supported upgrade path to the release after 1.4 (1.5.0, a
minor release under the rule in [releasing.md](../releasing.md#versioning)) is
through 1.4.x, run for at least 24 hours after the last 1.3 task stops: by then
1.4 has carried every 1.3 send that still counts into the quota item, or the
send has aged out of the window.

### Alternatives

**Keep the dual write.** Rejected: it serves only 1.3 tasks, costs on every send,
and leaves a second record whose rules, not the quota's, a reader has to reason
about.

**Stop writing but keep reading.** Rejected: a 1.3 task still would not see the
new tasks' sends, so a direct rolling upgrade from 1.3 would still not hold the
cap, while the `Query` stays on every check.

**Delete the existing rows.** Rejected: TTL already removes them, and finding
them needs a table `Scan`, which the task role does not grant.

### Consequences

- The quota item is the only source of the daily cap, and its version condition
  covers every send. A mixed 1.4/later deployment still holds the cap: 1.4 tasks
  also count and update the quota item in the same conditional transaction, and
  their extra legacy row is ignored by later tasks.
- Rolling from 1.3.x directly to a later release is unsupported: each version
  would ignore the other's sends. So is rolling back to 1.3.x from one.
- Rows left in a table are inert. Every release that wrote them (1.0.0 to 1.4.x)
  set `expires_at` to the send time plus 24 hours, and `expires_at` is the
  table's TTL attribute, so DynamoDB deletes them on its own.
- Revisit only if a release again has to share the daily cap with a version that
  stores it differently; then dual-write for one release, as 1.4 did.
