"""Identity resolution: deciding *who* is asking for configuration.

The chain is ordered strongest-evidence-first. Each step either resolves the
subscriber or falls through:

1. **token** — a previously issued provisioning token, bound to IMSI/IMEI;
2. **header enrichment** — a trusted operator gateway asserted the MSISDN;
3. **GBA** — a completed AKA bootstrap (interface only, see :mod:`acs.auth.gba`);
4. **OTP** — the client presented a valid ``OTP=`` for a pending challenge;
5. **claimed identity** — IMSI or MSISDN present and known: not proof of
   ownership, so this yields a *challenge*, never an authenticated identity;
6. **nothing usable** — HTTP 511, telling the client to retry over the mobile
   network or start the MSISDN entry flow.

Step 5 is the one that is easy to get wrong. A bare ``msisdn=`` query parameter
is a claim, not a credential; treating it as authentication would let anyone pull
another subscriber's IMS credentials.
"""

from __future__ import annotations

import dataclasses
import enum

from acs.domain.models import Subscriber


class IdentityMethod(str, enum.Enum):
    TOKEN = "token"
    ENRICHMENT = "enrichment"
    GBA = "gba"
    OTP = "otp"
    NONE = "none"


class IdentityDecision(str, enum.Enum):
    AUTHENTICATED = "authenticated"
    """The subscriber is proven; serve the configuration."""

    CHALLENGE_OTP = "challenge_otp"
    """A candidate MSISDN is known; send an OTP and answer 200 with no body."""

    UNRESOLVED = "unresolved"
    """No usable identity; answer 511."""

    NOT_ENTITLED = "not_entitled"
    """Known subscriber, RCS not permitted; answer 403."""


@dataclasses.dataclass(frozen=True, slots=True)
class Identity:
    decision: IdentityDecision
    method: IdentityMethod = IdentityMethod.NONE
    subscriber: Subscriber | None = None
    candidate_msisdn: str | None = None
    detail: str = ""

    @property
    def authenticated(self) -> bool:
        return self.decision is IdentityDecision.AUTHENTICATED


def authenticated(subscriber: Subscriber, method: IdentityMethod) -> Identity:
    return Identity(IdentityDecision.AUTHENTICATED, method, subscriber)


def challenge(subscriber: Subscriber, msisdn: str, detail: str = "") -> Identity:
    return Identity(
        IdentityDecision.CHALLENGE_OTP,
        IdentityMethod.OTP,
        subscriber,
        candidate_msisdn=msisdn,
        detail=detail,
    )


def unresolved(detail: str = "") -> Identity:
    return Identity(IdentityDecision.UNRESOLVED, detail=detail)


def not_entitled(subscriber: Subscriber, detail: str = "") -> Identity:
    return Identity(IdentityDecision.NOT_ENTITLED, subscriber=subscriber, detail=detail)
