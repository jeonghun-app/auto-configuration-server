"""SMS one-time-password challenge.

RCC.14 flow:

1. the client sends a configuration request it cannot authenticate;
2. the ACS generates an OTP, sends it by SMS and answers **200 with an empty
   body** — that empty 200 is the "challenge pending" signal;
3. the client repeats the *identical* request with ``OTP=`` appended;
4. the ACS verifies and returns the configuration.

When ``SMS_port`` is non-zero the OTP is expected as a port-addressed binary SMS
so the client can read it without user interaction. See
:mod:`acs.sms` for why that delivery mode needs an operator SMSC.

An open OTP endpoint is a direct financial attack (an attacker makes the operator
pay for SMS), so sends are rate limited per MSISDN with a cooldown and a daily
cap, and verification attempts are bounded.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import secrets
import time

from acs.domain.models import OtpChallenge
from acs.store.base import Store


@dataclasses.dataclass(frozen=True, slots=True)
class OtpPolicy:
    length: int = 6
    ttl_seconds: int = 300
    max_attempts: int = 3
    resend_cooldown_seconds: int = 60
    max_sends_per_day: int = 10


class OtpOutcome(str):
    """Marker string type for verification outcomes."""


VERIFIED = OtpOutcome("verified")
NO_CHALLENGE = OtpOutcome("no_challenge")
EXPIRED = OtpOutcome("expired")
MISMATCH = OtpOutcome("mismatch")
EXHAUSTED = OtpOutcome("exhausted")
CONSUMED = OtpOutcome("consumed")


def generate_otp(length: int = 6) -> str:
    """Generate a numeric OTP using a cryptographically secure source."""
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def hash_otp(msisdn: str, otp: str) -> str:
    """Hash the OTP together with the MSISDN so a digest cannot be replayed."""
    return hashlib.sha256(f"{msisdn}:{otp}".encode()).hexdigest()


class SendBlocked(Exception):
    """The OTP send was refused by a rate limit."""

    def __init__(self, reason: str, retry_after: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


def create_challenge(
    store: Store,
    msisdn: str,
    imsi: str,
    policy: OtpPolicy,
    sms_port: int | None = None,
    now: int | None = None,
) -> tuple[OtpChallenge, str]:
    """Create and persist a challenge, returning it with the clear-text OTP.

    Raises :class:`SendBlocked` when a cooldown or the daily cap applies, and
    :class:`~acs.store.base.OtpStoreContention` when concurrent requests for the
    same MSISDN kept conflicting. An
    existing, still valid challenge is replaced only after the cooldown, so
    repeated identical bootstrap requests do not each cost an SMS.
    """
    current = now or int(time.time())
    otp = generate_otp(policy.length)
    challenge = OtpChallenge(
        msisdn=msisdn,
        otp_hash=hash_otp(msisdn, otp),
        imsi=imsi,
        created_at=current,
        expires_at=current + policy.ttl_seconds,
        sms_port=sms_port,
        challenge_id=secrets.token_hex(8),
    )
    refused = store.issue_otp(
        challenge,
        cooldown_seconds=policy.resend_cooldown_seconds,
        max_sends_per_day=policy.max_sends_per_day,
        now=current,
    )
    if refused is not None:
        raise SendBlocked(refused.reason, retry_after=refused.retry_after)
    return challenge, otp


# Rounds of compare-and-swap before giving up. Each lost round means another
# request for the same MSISDN changed the challenge first, so contention this
# high is a guessing attack and the answer is "not verified".
_VERIFY_ROUNDS = 5


def verify_challenge(
    store: Store,
    msisdn: str,
    otp: str,
    policy: OtpPolicy,
    now: int | None = None,
) -> OtpOutcome:
    """Verify and atomically consume an OTP.

    Every change goes through :meth:`Store.replace_otp`, conditioned on the
    challenge being unchanged since it was read, so concurrent requests can
    neither verify one code twice nor spend the same attempt twice.

    Every failure path is indistinguishable to the caller in terms of HTTP
    response, so this function's detailed outcome is for metrics and logs only —
    it must not leak whether the MSISDN is known.
    """
    current = now or int(time.time())
    for _ in range(_VERIFY_ROUNDS):
        challenge = store.get_otp(msisdn)
        if challenge is None:
            return NO_CHALLENGE
        if challenge.consumed:
            return CONSUMED
        if challenge.expired(current):
            store.replace_otp(challenge, None)
            return EXPIRED
        if challenge.attempts >= policy.max_attempts:
            store.replace_otp(challenge, None)
            return EXHAUSTED

        attempts = challenge.attempts + 1
        replacement: OtpChallenge | None
        if hmac.compare_digest(challenge.otp_hash, hash_otp(msisdn, otp)):
            outcome, replacement = VERIFIED, None
        elif attempts >= policy.max_attempts:
            outcome, replacement = EXHAUSTED, None
        else:
            outcome = MISMATCH
            replacement = dataclasses.replace(challenge, attempts=attempts)
        if store.replace_otp(challenge, replacement):
            return outcome
    return MISMATCH


def discard_challenge(store: Store, challenge: OtpChallenge) -> bool:
    """Delete ``challenge`` if it is still the stored one, whatever its attempts.

    Identity is the challenge id, so a newer challenge that happens to carry the
    same code from the same second is not mistaken for it.

    For a challenge whose SMS could not be sent. The send can take seconds, and
    meanwhile the challenge may have been exhausted and a new one issued; deleting
    by MSISDN alone would destroy the new challenge, whose code is on its way.
    """
    for _ in range(_VERIFY_ROUNDS):
        current = store.get_otp(challenge.msisdn)
        if current is None or not current.same_issue(challenge):
            return False
        # Attempts may still move under us; the next round re-reads them.
        if store.replace_otp(current, None):
            return True
    return False


def policy_from_settings(settings: object) -> OtpPolicy:
    """Build a policy from :class:`acs.config.Settings` without importing it."""
    return OtpPolicy(
        length=int(getattr(settings, "otp_length", 6)),
        ttl_seconds=int(getattr(settings, "otp_ttl_seconds", 300)),
        max_attempts=int(getattr(settings, "otp_max_attempts", 3)),
        resend_cooldown_seconds=int(getattr(settings, "otp_resend_cooldown_seconds", 60)),
        max_sends_per_day=int(getattr(settings, "otp_max_sends_per_msisdn_per_day", 10)),
    )
