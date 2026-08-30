"""GBA scaffolding."""

from __future__ import annotations

import pytest

from acs.auth import gba


def test_mock_bsf_returns_deterministic_keys() -> None:
    client = gba.MockBsfClient({"btid-1": "001010000000001@ims.example.org"})
    first = client.fetch_keys("btid-1", "naf")
    second = client.fetch_keys("btid-1", "naf")
    assert first is not None and second is not None
    assert first.ks_naf == second.ks_naf
    assert first.impi.startswith("001010000000001")


def test_mock_bsf_rejects_an_unknown_btid() -> None:
    assert gba.MockBsfClient().fetch_keys("unknown", "naf") is None


def test_keys_differ_per_naf() -> None:
    client = gba.MockBsfClient({"btid-1": "impi@example.org"})
    assert client.fetch_keys("btid-1", "naf-a") != client.fetch_keys("btid-1", "naf-b")


def test_unconfigured_bsf_fails_closed() -> None:
    # In production, GBA enabled without a real Zn client must not pretend to
    # succeed.
    with pytest.raises(NotImplementedError, match="refusing to fake"):
        gba.UnconfiguredBsfClient().fetch_keys("btid", "naf")


def test_factory_returns_none_when_disabled() -> None:
    assert gba.build_bsf_client(enabled=False, is_prod=False) is None


def test_factory_returns_mock_in_dev_and_failclosed_in_prod() -> None:
    assert isinstance(gba.build_bsf_client(True, False), gba.MockBsfClient)
    assert isinstance(gba.build_bsf_client(True, True), gba.UnconfiguredBsfClient)


def test_challenge_header_declares_akav1_md5() -> None:
    header = gba.challenge_header("realm", "nonce123")
    assert "algorithm=AKAv1-MD5" in header
    assert 'nonce="nonce123"' in header
    assert 'realm="realm"' in header


def test_nonce_is_random() -> None:
    assert gba.make_nonce() != gba.make_nonce()


def test_authorization_header_is_parsed() -> None:
    parsed = gba.parse_authorization(
        'Digest username="btid-1", realm="r", nonce="n", uri="/config", response="abc"'
    )
    assert parsed["username"] == "btid-1"
    assert parsed["response"] == "abc"


def test_non_digest_authorization_is_ignored() -> None:
    assert gba.parse_authorization("Bearer abc") == {}
    assert gba.parse_authorization("") == {}


def test_digest_response_is_reproducible() -> None:
    args = ("btid", "realm", b"key", "GET", "/config", "nonce", "cnonce", "00000001")
    assert gba.digest_response(*args) == gba.digest_response(*args)
    assert len(gba.digest_response(*args)) == 32
