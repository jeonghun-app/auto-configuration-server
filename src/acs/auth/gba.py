"""GBA (Generic Bootstrapping Architecture) scaffolding — 3GPP TS 33.220.

What GBA needs to work for real: a USIM performing AKA, a Bootstrapping Server
Function reachable over Ub, an HSS holding the subscriber key, and a Zn interface
from this server (the NAF) to the BSF to fetch ``Ks_NAF``. None of that exists
outside an operator network, so **GBA cannot be genuinely completed here**.

What *is* implemented, and is genuinely useful:

* the HTTP-level challenge/response shape (``401`` with
  ``WWW-Authenticate: Digest ... algorithm=AKAv1-MD5``, then an ``Authorization``
  header carrying the B-TID as username);
* a :class:`BsfClient` port so an operator can drop in a real Zn client;
* a deterministic :class:`MockBsfClient` for tests.

GBA is disabled by default and, when enabled in staging/prod without a real BSF
adapter, the service fails closed rather than pretending a bootstrap succeeded.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import secrets
from typing import Protocol


@dataclasses.dataclass(frozen=True, slots=True)
class NafKeys:
    """The material a NAF receives from the BSF over Zn."""

    btid: str
    ks_naf: bytes
    impi: str
    lifetime_seconds: int


class BsfClient(Protocol):
    """Zn interface towards the Bootstrapping Server Function."""

    def fetch_keys(self, btid: str, naf_id: str) -> NafKeys | None: ...


class MockBsfClient:
    """Deterministic test double.

    ``Ks_NAF`` is derived from a fixed test vector, so tests are reproducible.
    Only usable when ``ACS_ENV`` is a non-production environment.
    """

    TEST_SECRET = b"33.220-test-vector"

    def __init__(self, known_btids: dict[str, str] | None = None) -> None:
        self._known = known_btids or {}

    def register(self, btid: str, impi: str) -> None:
        self._known[btid] = impi

    def fetch_keys(self, btid: str, naf_id: str) -> NafKeys | None:
        impi = self._known.get(btid)
        if impi is None:
            return None
        ks_naf = hashlib.sha256(self.TEST_SECRET + btid.encode() + naf_id.encode()).digest()
        return NafKeys(btid=btid, ks_naf=ks_naf, impi=impi, lifetime_seconds=3600)


class UnconfiguredBsfClient:
    """Fail-closed placeholder used when GBA is enabled without a real adapter."""

    def fetch_keys(self, btid: str, naf_id: str) -> NafKeys | None:
        raise NotImplementedError(
            "GBA is enabled but no BSF (Zn) client is configured. Implement "
            "BsfClient against the operator BSF; refusing to fake a bootstrap."
        )


def make_nonce() -> str:
    return base64.b64encode(secrets.token_bytes(16)).decode()


def challenge_header(realm: str, nonce: str, qop: str = "auth") -> str:
    """Build the ``WWW-Authenticate`` value for a GBA bootstrap challenge."""
    return (
        f'Digest realm="{realm}", nonce="{nonce}", qop="{qop}", '
        'algorithm=AKAv1-MD5, opaque="gba"'
    )


def parse_authorization(header: str) -> dict[str, str]:
    """Parse a Digest ``Authorization`` header into its directives."""
    if not header.lower().startswith("digest "):
        return {}
    out: dict[str, str] = {}
    for part in header[7:].split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip().lower()] = value.strip().strip('"')
    return out


def digest_response(
    username: str,
    realm: str,
    password: bytes,
    method: str,
    uri: str,
    nonce: str,
    cnonce: str,
    nc: str,
    qop: str = "auth",
) -> str:
    """RFC 2617 Digest response using ``Ks_NAF`` as the password."""
    ha1 = hashlib.md5(  # noqa: S324 - algorithm mandated by AKAv1-MD5
        username.encode() + b":" + realm.encode() + b":" + password
    ).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()  # noqa: S324
    return hashlib.md5(  # noqa: S324
        f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
    ).hexdigest()


def build_bsf_client(enabled: bool, is_prod: bool) -> BsfClient | None:
    """Return the BSF client for the current configuration."""
    if not enabled:
        return None
    if is_prod:
        return UnconfiguredBsfClient()
    return MockBsfClient()
