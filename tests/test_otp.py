"""SMS OTP challenge behaviour."""

from __future__ import annotations

import pytest
from tests.conftest import TEST_IMSI, TEST_MSISDN

from acs.auth import otp as otp_mod
from acs.store.memory import MemoryStore

POLICY = otp_mod.OtpPolicy(
    length=6, ttl_seconds=300, max_attempts=3, resend_cooldown_seconds=60, max_sends_per_day=5
)


def test_generated_otp_has_the_configured_length() -> None:
    for length in (4, 6, 8):
        assert len(otp_mod.generate_otp(length)) == length
        assert otp_mod.generate_otp(length).isdigit()


def test_otp_hash_is_bound_to_the_msisdn() -> None:
    # Binding the hash prevents a digest captured for one subscriber from being
    # replayed against another.
    assert otp_mod.hash_otp("+8210", "123456") != otp_mod.hash_otp("+8211", "123456")


def test_challenge_is_stored_hashed(store: MemoryStore) -> None:
    challenge, clear = otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, POLICY)
    assert clear not in challenge.otp_hash
    assert challenge.otp_hash == otp_mod.hash_otp(TEST_MSISDN, clear)


def test_valid_otp_verifies_once(store: MemoryStore) -> None:
    _, clear = otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, POLICY)
    assert otp_mod.verify_challenge(store, TEST_MSISDN, clear, POLICY) == otp_mod.VERIFIED
    # Single use: the second attempt finds nothing.
    assert otp_mod.verify_challenge(store, TEST_MSISDN, clear, POLICY) == otp_mod.NO_CHALLENGE


def test_wrong_otp_reports_mismatch(store: MemoryStore) -> None:
    otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, POLICY)
    assert otp_mod.verify_challenge(store, TEST_MSISDN, "000000", POLICY) == otp_mod.MISMATCH


def test_attempts_are_bounded(store: MemoryStore) -> None:
    otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, POLICY)
    outcomes = [
        otp_mod.verify_challenge(store, TEST_MSISDN, "000000", POLICY)
        for _ in range(POLICY.max_attempts)
    ]
    assert outcomes[-1] == otp_mod.EXHAUSTED
    assert store.get_otp(TEST_MSISDN) is None


def test_expired_challenge_is_rejected_and_removed(store: MemoryStore) -> None:
    _, clear = otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, POLICY, now=1000)
    outcome = otp_mod.verify_challenge(store, TEST_MSISDN, clear, POLICY, now=1000 + 301)
    assert outcome == otp_mod.EXPIRED
    assert store.get_otp(TEST_MSISDN) is None


def test_verification_without_a_challenge_is_reported(store: MemoryStore) -> None:
    assert otp_mod.verify_challenge(store, TEST_MSISDN, "123456", POLICY) == otp_mod.NO_CHALLENGE


def test_resend_within_the_cooldown_is_blocked(store: MemoryStore) -> None:
    otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, POLICY, now=1000)
    with pytest.raises(otp_mod.SendBlocked) as excinfo:
        otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, POLICY, now=1010)
    assert excinfo.value.reason == "cooldown"
    assert excinfo.value.retry_after == 50


def test_resend_after_the_cooldown_is_allowed(store: MemoryStore) -> None:
    otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, POLICY, now=1000)
    challenge, _ = otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, POLICY, now=1100)
    assert challenge.created_at == 1100


def test_daily_quota_stops_sms_pumping(store: MemoryStore) -> None:
    # An unbounded OTP endpoint is a direct financial attack on the operator.
    policy = otp_mod.OtpPolicy(resend_cooldown_seconds=0, max_sends_per_day=3)
    for index in range(3):
        otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, policy, now=1000 + index)
    with pytest.raises(otp_mod.SendBlocked) as excinfo:
        otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, policy, now=2000)
    assert excinfo.value.reason == "daily_quota"


def test_sms_port_is_carried_into_the_challenge(store: MemoryStore) -> None:
    challenge, _ = otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, POLICY, sms_port=37273)
    assert challenge.sms_port == 37273


def test_policy_is_derived_from_settings(settings: object) -> None:
    policy = otp_mod.policy_from_settings(settings)
    assert policy.length == 6
    assert policy.ttl_seconds == 300
    assert policy.max_attempts == 3
