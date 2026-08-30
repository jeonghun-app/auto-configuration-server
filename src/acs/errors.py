"""Domain errors, each mapped to a specific RCC.14 HTTP outcome."""

from __future__ import annotations


class AcsError(Exception):
    """Base class for ACS domain errors."""

    status_code = 500
    reason = "internal_error"


class MalformedRequest(AcsError):
    """Structurally invalid RCC.14 query parameters."""

    status_code = 400
    reason = "malformed_request"


class SubscriberNotEntitled(AcsError):
    """Subscriber is known but not entitled to RCS -> HTTP 403."""

    status_code = 403
    reason = "not_entitled"


class IdentityUnresolved(AcsError):
    """The ACS cannot determine which subscriber is calling -> HTTP 511.

    RFC 6585 s6 defines 511; RCC.14 uses it to tell the client to retry over
    the mobile network (so the operator gateway can enrich the request) or to
    start the MSISDN entry flow.
    """

    status_code = 511
    reason = "network_authentication_required"


class OtpChallengeIssued(AcsError):
    """An OTP was sent. RCC.14 signals this with 200 and an empty body."""

    status_code = 200
    reason = "otp_pending"


class OtpRejected(AcsError):
    """Supplied OTP was wrong, expired or already consumed."""

    status_code = 511
    reason = "otp_rejected"


class RateLimited(AcsError):
    status_code = 429
    reason = "rate_limited"


class ServiceBusy(AcsError):
    status_code = 503
    reason = "service_busy"


class GbaChallengeRequired(AcsError):
    """GBA bootstrapping required -> HTTP 401 with a Digest AKAv1-MD5 challenge."""

    status_code = 401
    reason = "gba_challenge"


class AdminUnavailable(AcsError):
    status_code = 503
    reason = "admin_disabled"


class DmProtocolError(AcsError):
    """Malformed or unsupported SyncML DM package."""

    status_code = 400
    reason = "dm_protocol_error"


class DmAuthRequired(AcsError):
    status_code = 401
    reason = "dm_auth_required"


class CatalogError(RuntimeError):
    """The provisioning catalogue failed to load or validate."""
