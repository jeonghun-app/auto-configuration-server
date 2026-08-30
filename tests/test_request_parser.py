"""RCC.14 query parameter parsing."""

from __future__ import annotations

import pytest

from acs.errors import MalformedRequest
from acs.protocol.request import ConfigQuery, parse_config_query


def parse(**params: str | list[str]) -> ConfigQuery:
    grouped = {k: (v if isinstance(v, list) else [v]) for k, v in params.items()}
    return parse_config_query(grouped)


@pytest.mark.spec
def test_parses_the_full_documented_parameter_set() -> None:
    query = parse(
        vers="3",
        IMSI="001010000000001",
        IMEI="356938035643809",
        msisdn="+821012345678",
        token="abc",
        OTP="123456",
        SMS_port="37273",
        default_sms_app="1",
        terminal_vendor="Sim",
        terminal_model="SimPhone",
        terminal_sw_version="1.0",
        client_vendor="Sim",
        client_version="RCSAndrd-1.0",
        rcs_version="9.0",
        rcs_profile="UP_2.4",
        rcs_state="0",
        provisioning_version="1",
    )
    assert query.vers == 3
    assert query.imsi == "001010000000001"
    assert query.imei == "356938035643809"
    assert query.msisdn == "+821012345678"
    assert query.otp == "123456"
    assert query.sms_port == 37273
    assert query.default_sms_app == 1
    assert query.rcs_profile == "UP_2.4"


def test_missing_vers_defaults_to_zero() -> None:
    assert parse(IMSI="001010000000001").vers == 0


@pytest.mark.spec
def test_negative_vers_is_accepted() -> None:
    # Clients echo back the disable value they were previously given.
    assert parse(vers="-2").vers == -2


def test_imsi_leading_zeros_are_preserved() -> None:
    # Parsing an IMSI as an integer would silently change the MCC.
    assert parse(IMSI="001010000000001").imsi == "001010000000001"


@pytest.mark.parametrize("bad", ["12ab567890", "123", "0123456789012345678"])
def test_invalid_imsi_is_rejected(bad: str) -> None:
    with pytest.raises(MalformedRequest):
        parse(IMSI=bad)


@pytest.mark.parametrize("imei", ["356938035643809", "3569380356438091", "35693803564380"])
def test_imei_and_imeisv_lengths_are_accepted(imei: str) -> None:
    assert parse(IMEI=imei).imei == imei


def test_non_luhn_test_imei_is_accepted() -> None:
    # Field-test devices carry IMEIs that fail the Luhn check; rejecting them
    # would lock real handsets out of provisioning.
    assert parse(IMEI="000000000000000").imei == "000000000000000"


def test_repeated_app_parameter_is_collected() -> None:
    query = parse(app=["ap2001", "ap2002", "ap2001"])
    assert query.apps == ("ap2001", "ap2002")


def test_repeated_identity_parameter_is_rejected() -> None:
    with pytest.raises(MalformedRequest):
        parse(IMSI=["001010000000001", "001010000000002"])


def test_repeated_otp_is_rejected() -> None:
    with pytest.raises(MalformedRequest):
        parse(OTP=["111111", "222222"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+821012345678", "+821012345678"),
        ("821012345678", "+821012345678"),
        ("821012345678", "+821012345678"),
        ("+82-10-1234-5678", "+821012345678"),
    ],
)
def test_msisdn_is_normalised_to_e164(raw: str, expected: str) -> None:
    assert parse(msisdn=raw).msisdn == expected


def test_badly_encoded_plus_arriving_as_space_is_recovered() -> None:
    assert parse(msisdn=" 821012345678").msisdn == "+821012345678"


def test_invalid_msisdn_is_rejected() -> None:
    with pytest.raises(MalformedRequest):
        parse(msisdn="not-a-number")


def test_oversized_terminal_model_is_rejected() -> None:
    with pytest.raises(MalformedRequest):
        parse(terminal_model="x" * 64)


def test_non_alphanumeric_otp_is_rejected() -> None:
    with pytest.raises(MalformedRequest):
        parse(OTP="12'34")


def test_sms_port_range_is_enforced() -> None:
    with pytest.raises(MalformedRequest):
        parse(SMS_port="70000")


def test_unknown_parameters_are_recorded_not_rejected() -> None:
    query = parse(IMSI="001010000000001", vendor_extension="x")
    assert query.unknown == ("vendor_extension",)


def test_lowercase_aliases_are_accepted() -> None:
    # Some deployed clients disagree with the specification's casing.
    query = parse(imsi="001010000000001", imei="356938035643809", otp="123456")
    assert query.imsi == "001010000000001"
    assert query.imei == "356938035643809"
    assert query.otp == "123456"


def test_device_key_prefers_imei() -> None:
    assert parse(IMEI="356938035643809", IMSI="001010000000001").device_key == "356938035643809"
    assert parse(IMSI="001010000000001").device_key == "001010000000001"


def test_user_agent_is_truncated() -> None:
    query = parse_config_query({}, user_agent="x" * 400)
    assert len(query.user_agent) == 256
