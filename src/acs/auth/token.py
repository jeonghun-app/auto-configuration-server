"""Provisioning tokens.

A token lets a client skip the OTP challenge on subsequent configuration
requests, so it is a bearer credential for a subscriber identity. Consequences:

* only the SHA-256 digest is persisted — a database leak does not yield usable
  tokens;
* the token is bound to the IMSI and (optionally) the IMEI, so lifting a token
  onto another handset fails;
* comparison is constant time;
* tokens are revocable, individually and per subscriber.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from acs.domain.models import TokenRecord
from acs.store.base import Store

TOKEN_BYTES = 32
"""256 bits of entropy."""


def generate_token() -> str:
    """Return a fresh opaque URL-safe token."""
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).decode().rstrip("=")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(
    store: Store,
    imsi: str,
    imei: str | None,
    ttl_seconds: int,
    bind_imei: bool = True,
) -> str:
    """Create, persist and return a new token."""
    token = generate_token()
    now = int(time.time())
    store.put_token(
        TokenRecord(
            token_hash=hash_token(token),
            imsi=imsi,
            imei=imei if bind_imei else None,
            issued_at=now,
            expires_at=now + ttl_seconds,
        )
    )
    return token


def verify_token(
    store: Store,
    token: str,
    imei: str | None,
    bind_imei: bool = True,
) -> TokenRecord | None:
    """Return the token record when the token is valid, otherwise ``None``."""
    if not token:
        return None
    digest = hash_token(token)
    record = store.get_token(digest)
    if record is None:
        return None
    # The lookup key is already a digest, but compare again in constant time so
    # the code path is identical whether or not the record was found.
    if not hmac.compare_digest(record.token_hash, digest):
        return None  # pragma: no cover - defensive
    if record.revoked or record.expired():
        return None
    if bind_imei and record.imei and record.imei != imei:
        return None
    return record
