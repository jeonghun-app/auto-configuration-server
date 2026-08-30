"""IMSI to IMS identity derivation."""

from __future__ import annotations

import pytest

from acs.protocol.identity import derive_identity, split_imsi


def test_two_digit_mnc_is_the_default() -> None:
    assert split_imsi("450050000000001") == ("450", "005")


def test_north_american_mccs_use_three_digit_mncs() -> None:
    assert split_imsi("310260000000001") == ("310", "260")
    assert split_imsi("302720000000001") == ("302", "720")


def test_mnc_length_can_be_overridden() -> None:
    assert split_imsi("450050000000001", mnc_length=3) == ("450", "050")


def test_short_or_non_numeric_imsi_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 5 digits"):
        split_imsi("123")
    with pytest.raises(ValueError, match="at least 5 digits"):
        split_imsi("abcdefgh")


def test_derived_identity_matches_the_3gpp_naming_convention() -> None:
    identity = derive_identity("450050000000001", "+821012345678")
    assert identity.ims_domain == "ims.mnc005.mcc450.3gppnetwork.org"
    assert identity.impi == "450050000000001@ims.mnc005.mcc450.3gppnetwork.org"
    assert identity.impu == "sip:+821012345678@ims.mnc005.mcc450.3gppnetwork.org"
    assert identity.acs_fqdn == "config.rcs.mnc005.mcc450.pub.3gppnetwork.org"


def test_impu_falls_back_to_the_impi_without_an_msisdn() -> None:
    identity = derive_identity("450050000000001")
    assert identity.impu == f"sip:{identity.impi}"


def test_context_exposes_every_placeholder_the_catalogue_uses() -> None:
    context = derive_identity("450050000000001", "+821012345678").as_context()
    for key in ("imsi", "mcc", "mnc", "ims_domain", "impi", "impu", "acs_fqdn"):
        assert key in context
