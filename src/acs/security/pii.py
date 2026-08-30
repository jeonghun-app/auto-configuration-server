"""PII handling for subscriber identifiers.

IMSI / IMEI / MSISDN are subscriber-identifying data and must never reach logs,
metric dimensions or traces in the clear. Everything that is logged goes through
:func:`redact`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Final

_SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "imsi",
        "imei",
        "imeisv",
        "msisdn",
        # An OMA-DM DevId is IMEI-derived, so it is a device identifier too and
        # must be redacted under whatever name it travels.
        "device",
        "device_id",
        "devid",
        "otp",
        "token",
        "password",
        "userpwd",
        "authorization",
        "cred",
        "secret",
    }
)

_MSISDN_RE: Final = re.compile(r"^\+?\d{6,15}$")


def mask_tail(value: str | None, keep: int = 4) -> str:
    """Mask everything but the last ``keep`` characters."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def hash_id(value: str | None, secret: str) -> str:
    """Return a stable keyed pseudonym (HMAC-SHA256, truncated)."""
    if not value:
        return ""
    if not secret:
        # Unkeyed hashing of a low-entropy identifier is reversible by brute
        # force, so refuse rather than provide false assurance.
        raise ValueError("pii_hash_secret is required for hash mode")
    digest = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    return digest[:16]


def redact(value: str | None, mode: str = "mask", secret: str = "") -> str:
    """Redact a single sensitive value according to ``mode``."""
    if value is None:
        return ""
    if mode == "none":
        return value
    if mode == "hash":
        return hash_id(value, secret)
    return mask_tail(value)


def redact_mapping(
    data: dict[str, object], mode: str = "mask", secret: str = ""
) -> dict[str, object]:
    """Return a copy of ``data`` with sensitive keys redacted."""
    out: dict[str, object] = {}
    for key, value in data.items():
        if key.lower() in _SENSITIVE_KEYS and isinstance(value, str):
            out[key] = redact(value, mode, secret)
        else:
            out[key] = value
    return out


def normalise_msisdn(raw: str | None) -> str | None:
    """Normalise an MSISDN towards E.164.

    Clients are inconsistent: some send ``+821012345678``, some send
    ``821012345678``, and a badly encoded ``+`` arrives as a space. Only digits
    are kept and a leading ``+`` is always added.
    """
    if raw is None:
        return None
    cleaned = raw.strip().replace(" ", "+").replace("-", "")
    cleaned = "+" + cleaned.lstrip("+")
    digits = "+" + "".join(ch for ch in cleaned if ch.isdigit())
    if not _MSISDN_RE.match(digits):
        return None
    return digits
