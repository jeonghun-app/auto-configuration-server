"""Operator header enrichment.

On the cellular data path an operator gateway can insert a trusted subscriber
identity header (commonly ``X-3GPP-Intended-Identity``). That is the smoothest
provisioning experience — no OTP, no user interaction.

It is also trivially spoofable: an identity header is just a header. Anyone can
send one. So it is only honoured when **both** hold:

* header enrichment is explicitly enabled (``ACS_TRUSTED_PROXY_CIDRS`` is set —
  the default of empty disables the mechanism entirely); and
* the immediate peer address falls inside one of those CIDRs.

Note that behind an ALB the peer address is the load balancer, so the client
address must be taken from ``X-Forwarded-For``. The ALB *appends* to
``X-Forwarded-For``, so the right-most entry is the one the ALB observed and the
only one that cannot be forged by the caller.
"""

from __future__ import annotations

import dataclasses
import ipaddress
from collections.abc import Mapping, Sequence

from acs.security.pii import normalise_msisdn


@dataclasses.dataclass(frozen=True, slots=True)
class EnrichmentResult:
    msisdn: str | None
    trusted: bool
    reason: str


def _networks(cidrs: Sequence[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    out: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        try:
            out.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return out


def client_address(peer: str | None, forwarded_for: str | None) -> str | None:
    """Return the address to evaluate for trust.

    The right-most ``X-Forwarded-For`` entry is used because that is the value
    written by the closest trusted hop; earlier entries are caller-controlled.
    """
    if forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return peer


def is_trusted_peer(address: str | None, cidrs: Sequence[str]) -> bool:
    if not address or not cidrs:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in _networks(cidrs))


def resolve_enriched_identity(
    headers: Mapping[str, str],
    peer: str | None,
    forwarded_for: str | None,
    header_name: str,
    trusted_cidrs: Sequence[str],
) -> EnrichmentResult:
    """Extract an enriched MSISDN, but only from a trusted peer."""
    if not trusted_cidrs:
        return EnrichmentResult(None, False, "disabled")

    # Header lookup is case-insensitive: Starlette lower-cases header names.
    lowered = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get(header_name.lower())
    if not raw:
        return EnrichmentResult(None, False, "absent")

    address = client_address(peer, forwarded_for)
    if not is_trusted_peer(address, trusted_cidrs):
        # Present but untrusted: this is either a misconfiguration or a spoofing
        # attempt. Ignore the value; never fall back to trusting it.
        return EnrichmentResult(None, False, "untrusted_peer")

    # X-3GPP-Intended-Identity values are often quoted, and may be a tel: or
    # sip: URI rather than a bare number.
    value = raw.strip().strip('"')
    for prefix in ("tel:", "sip:"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    value = value.split("@", 1)[0].split(";", 1)[0]

    msisdn = normalise_msisdn(value)
    if msisdn is None:
        return EnrichmentResult(None, True, "malformed_value")
    return EnrichmentResult(msisdn, True, "trusted")
