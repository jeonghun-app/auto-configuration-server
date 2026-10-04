"""Deterministic overlap at challenge replacement and retry boundaries."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.conftest import TEST_IMSI, TEST_MSISDN, base_query
from tests.test_otp_atomicity import NOW, cancelled, dynamo_store, otp_store

from acs.app import create_app
from acs.auth import otp
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.store.base import Store
from acs.store.dynamodb import DynamoDbStore

__all__ = ["dynamo_store", "otp_store"]


@pytest.mark.xfail(
    strict=True,
    reason="QA defect: repeated code within one second aliases a new challenge during discard",
)
def test_a_late_failed_send_preserves_a_new_issue_even_if_the_random_code_repeats(
    otp_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = otp.OtpPolicy(length=4)
    monkeypatch.setattr(otp, "generate_otp", lambda _length: "1234")
    old, _ = otp.create_challenge(otp_store, TEST_MSISDN, TEST_IMSI, policy, now=NOW)
    released = threading.Event()

    def delayed_cleanup() -> bool:
        assert released.wait(timeout=5)
        return otp.discard_challenge(otp_store, old)

    with ThreadPoolExecutor(max_workers=1) as pool:
        cleanup = pool.submit(delayed_cleanup)
        try:
            for _ in range(policy.max_attempts):
                otp.verify_challenge(otp_store, TEST_MSISDN, "wrong", policy, now=NOW)
            newer, clear = otp.create_challenge(
                otp_store, TEST_MSISDN, TEST_IMSI, policy, sms_port=37273, now=NOW
            )
            assert newer.sms_port != old.sms_port
        finally:
            released.set()
        deleted = cleanup.result(timeout=5)
    result = otp.verify_challenge(otp_store, TEST_MSISDN, clear, policy, now=NOW)
    assert (deleted, result) == (False, otp.VERIFIED)


@pytest.mark.parametrize("clear", ["한글", "🔒", "9" * 10000])
def test_unusual_wrong_guesses_spend_exactly_one_attempt(otp_store: Store, clear: str) -> None:
    policy = otp.OtpPolicy()
    otp.create_challenge(otp_store, TEST_MSISDN, TEST_IMSI, policy, now=NOW)
    assert otp.verify_challenge(otp_store, TEST_MSISDN, clear, policy, now=NOW) == otp.MISMATCH
    current = otp_store.get_otp(TEST_MSISDN)
    assert current is not None
    assert current.attempts == 1


@pytest.mark.aws
def test_real_transaction_retry_exhaustion_becomes_http_503(
    dynamo_store: DynamoDbStore, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    dynamo_store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, entitled=True))
    attempts = 0

    def fail(**kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise cancelled("ConditionalCheckFailed", "None", "None")

    monkeypatch.setattr(dynamo_store._table.meta.client, "transact_write_items", fail)
    with TestClient(create_app(settings, dynamo_store)) as client:
        response = client.post("/config", data=base_query(SMS_port=37273))
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert attempts == 5
    assert dynamo_store.get_otp(TEST_MSISDN) is None
    assert dynamo_store.list_sms(TEST_MSISDN) == []
