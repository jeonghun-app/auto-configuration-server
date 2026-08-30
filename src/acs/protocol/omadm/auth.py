"""OMA-DM session authentication.

Two schemes are supported at the SyncML layer:

``syncml:auth-basic``
    ``Data`` is ``base64(username:password)``.

``syncml:auth-md5``
    ``Data`` is ``base64(MD5(base64(MD5(username:password)) + ":" + nonce))``.
    MD5 is mandated by the OMA-DM specification; it is used here only for
    protocol compatibility, which is why the password itself is never stored in
    the clear and the transport is required to be TLS.

Credentials are provisioned to the device by the OMA-CP ``w7`` characteristic
(see :func:`acs.protocol.omacp.builder.dm_account_characteristic`), so the DM
username is the subscriber's IMSI and the password is the per-subscriber secret
generated at RCS provisioning time.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import secrets

from acs.protocol.omadm.syncml import AUTH_BASIC, AUTH_MD5, Credentials


@dataclasses.dataclass(frozen=True, slots=True)
class DmAuthResult:
    authenticated: bool
    username: str = ""
    scheme: str = ""
    reason: str = ""
    challenge_nonce: str = ""


def make_nonce() -> str:
    return base64.b64encode(secrets.token_bytes(16)).decode()


def md5_credential(username: str, password: str, nonce: str) -> str:
    """Compute the ``syncml:auth-md5`` credential value."""
    inner = hashlib.md5(f"{username}:{password}".encode()).digest()  # noqa: S324
    inner_b64 = base64.b64encode(inner)
    digest = hashlib.md5(inner_b64 + b":" + nonce.encode()).digest()  # noqa: S324
    return base64.b64encode(digest).decode()


def basic_credential(username: str, password: str) -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


def authenticate(
    credentials: Credentials | None,
    scheme: str,
    lookup_password: object,
    nonce: str = "",
) -> DmAuthResult:
    """Validate device credentials.

    ``lookup_password`` is a callable taking the username and returning the
    expected password, or ``None`` when the username is unknown. An unknown user
    and a wrong password are reported identically so the DM endpoint cannot be
    used to enumerate subscribers.
    """
    if scheme == "none":
        username = ""
        if credentials is not None:
            decoded = credentials.decode_basic()
            if decoded:
                username = decoded[0]
        return DmAuthResult(True, username=username, scheme="none")

    if credentials is None or not credentials.data:
        return DmAuthResult(
            False, reason="missing_credentials", challenge_nonce=nonce or make_nonce()
        )

    if credentials.type == AUTH_BASIC:
        decoded = credentials.decode_basic()
        if decoded is None:
            return DmAuthResult(False, reason="malformed", challenge_nonce=nonce or make_nonce())
        username, password = decoded
        expected = lookup_password(username)  # type: ignore[operator]
        if not expected or not hmac.compare_digest(str(expected), password):
            return DmAuthResult(
                False, username=username, reason="invalid", challenge_nonce=nonce or make_nonce()
            )
        return DmAuthResult(True, username=username, scheme=AUTH_BASIC)

    if credentials.type == AUTH_MD5:
        # The username is not carried inside an auth-md5 credential, so it comes
        # from the SyncHdr LocName, which the caller passes via the nonce-bound
        # session. Handled by :func:`authenticate_md5`.
        return DmAuthResult(
            False, reason="md5_requires_username", challenge_nonce=nonce or make_nonce()
        )

    return DmAuthResult(False, reason="unsupported_scheme", challenge_nonce=nonce or make_nonce())


def authenticate_md5(
    credentials: Credentials | None,
    username: str,
    expected_password: str | None,
    nonce: str,
) -> DmAuthResult:
    """Validate a ``syncml:auth-md5`` credential for a known username."""
    if credentials is None or not credentials.data or credentials.type != AUTH_MD5:
        return DmAuthResult(
            False,
            username=username,
            reason="missing_credentials",
            challenge_nonce=nonce or make_nonce(),
        )
    if not expected_password or not nonce:
        return DmAuthResult(
            False, username=username, reason="invalid", challenge_nonce=nonce or make_nonce()
        )
    expected = md5_credential(username, expected_password, nonce)
    if not hmac.compare_digest(expected, credentials.data.strip()):
        return DmAuthResult(
            False, username=username, reason="invalid", challenge_nonce=make_nonce()
        )
    return DmAuthResult(True, username=username, scheme=AUTH_MD5)
