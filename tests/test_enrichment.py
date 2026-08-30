"""Header enrichment trust boundary."""

from __future__ import annotations

from acs.auth.enrichment import (
    client_address,
    is_trusted_peer,
    resolve_enriched_identity,
)

HEADER = "X-3GPP-Intended-Identity"
TRUSTED = ["10.0.0.0/8"]


def resolve(headers: dict[str, str], peer: str, cidrs: list[str] | None = None):
    return resolve_enriched_identity(
        headers=headers,
        peer=peer,
        forwarded_for=headers.get("x-forwarded-for"),
        header_name=HEADER,
        trusted_cidrs=TRUSTED if cidrs is None else cidrs,
    )


def test_mechanism_is_disabled_by_default() -> None:
    # Empty trusted CIDR list must switch the whole mechanism off.
    result = resolve({HEADER: "+821012345678"}, "10.1.2.3", cidrs=[])
    assert result.msisdn is None
    assert result.reason == "disabled"


def test_trusted_peer_identity_is_accepted() -> None:
    result = resolve({HEADER: "+821012345678"}, "10.1.2.3")
    assert result.msisdn == "+821012345678"
    assert result.trusted is True


def test_untrusted_peer_identity_is_ignored() -> None:
    # An identity header is trivially forgeable; from an untrusted peer it must
    # never be honoured.
    result = resolve({HEADER: "+821099999999"}, "203.0.113.5")
    assert result.msisdn is None
    assert result.reason == "untrusted_peer"


def test_absent_header_is_reported() -> None:
    assert resolve({}, "10.1.2.3").reason == "absent"


def test_header_lookup_is_case_insensitive() -> None:
    result = resolve({"x-3gpp-intended-identity": "+821012345678"}, "10.1.2.3")
    assert result.msisdn == "+821012345678"


def test_tel_and_sip_uri_forms_are_understood() -> None:
    assert resolve({HEADER: "tel:+821012345678"}, "10.1.2.3").msisdn == "+821012345678"
    assert (
        resolve({HEADER: "sip:+821012345678@ims.example.org"}, "10.1.2.3").msisdn == "+821012345678"
    )
    assert resolve({HEADER: '"+821012345678"'}, "10.1.2.3").msisdn == "+821012345678"


def test_malformed_value_from_a_trusted_peer_is_reported() -> None:
    result = resolve({HEADER: "not-a-number"}, "10.1.2.3")
    assert result.msisdn is None
    assert result.trusted is True
    assert result.reason == "malformed_value"


def test_rightmost_forwarded_for_entry_is_used() -> None:
    # Behind an ALB the peer is the load balancer, and the ALB appends, so only
    # the right-most entry is not caller-controlled.
    headers = {HEADER: "+821012345678", "x-forwarded-for": "203.0.113.9, 10.0.0.7"}
    assert resolve(headers, "172.31.0.1").msisdn == "+821012345678"


def test_forged_leading_forwarded_for_entry_does_not_grant_trust() -> None:
    headers = {HEADER: "+821099999999", "x-forwarded-for": "10.0.0.7, 203.0.113.9"}
    assert resolve(headers, "203.0.113.9").msisdn is None


def test_client_address_prefers_forwarded_for() -> None:
    assert client_address("1.2.3.4", "10.0.0.1, 10.0.0.2") == "10.0.0.2"
    assert client_address("1.2.3.4", None) == "1.2.3.4"
    assert client_address(None, "") is None


def test_trust_evaluation_handles_bad_input() -> None:
    assert is_trusted_peer("not-an-ip", TRUSTED) is False
    assert is_trusted_peer(None, TRUSTED) is False
    assert is_trusted_peer("10.0.0.1", []) is False
    assert is_trusted_peer("10.0.0.1", ["nonsense"]) is False
    assert is_trusted_peer("10.0.0.1", ["10.0.0.0/8"]) is True
