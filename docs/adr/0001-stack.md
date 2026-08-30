# ADR 0001 — Python 3.11 and FastAPI

**Status:** accepted

## Context

The ACS is an HTTP service whose real work is parsing a query string, making a
decision, and emitting a deeply nested XML document. It also needs a second
protocol (SyncML DM) with its own XML parsing, a large test matrix, and a
container image that deploys to AWS.

## Decision

Python 3.11 with FastAPI, `pydantic-settings` for configuration and `lxml` for XML.

## Rationale

- `lxml` gives correct attribute escaping, safe parsing with entity resolution and
  network access disabled, and namespace-tolerant traversal. The OMA-CP and SyncML
  documents are attribute-heavy and deeply nested, which is awkward in Go's
  `encoding/xml` and verbose in Java.
- The test matrix is large — 337 tests across two protocols — and pytest with
  parametrisation and fixtures keeps it readable.
- `moto` lets the DynamoDB and SMS code paths be tested without an AWS account.
- `mypy --strict` over the whole source tree gives most of the safety a static
  language would, and it passes clean.
- The image is 207 MB, which is larger than a Go binary and much smaller than a
  JVM image. For a service whose latency is dominated by a DynamoDB round trip,
  that is the right trade.

## Alternatives

**Go** — smallest image and fastest start. Rejected because `encoding/xml` makes
the characteristic/parm model painful, and there is no comparable local-mocking
story for AWS.

**Java / Spring Boot** — the most "telecom-native" ecosystem. Rejected for image
size, slower test iteration, and far more ceremony for a service this size.

**Node / TypeScript** — viable. Rejected because it offers no advantage for
XML-heavy protocol work and needs more care around runtime validation.

## Consequences

- Async is available but the code is deliberately synchronous: `boto3` is
  synchronous and FastAPI runs sync endpoints in a threadpool, which is simpler to
  reason about than mixing async with blocking SDK calls.
- Type checking must stay strict, or Python's flexibility becomes a liability in a
  service where a misspelled parameter name silently disables a handset feature.
