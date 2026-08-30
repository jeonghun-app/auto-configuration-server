"""DynamoDB store, exercised against moto."""

from __future__ import annotations

import time
from collections.abc import Iterator

import boto3
import pytest
from moto import mock_aws

from acs.domain.models import Device, DmSession, OtpChallenge, SmsMessage, Subscriber, TokenRecord
from acs.store.dynamodb import DynamoDbStore

pytestmark = [pytest.mark.aws]

REGION = "ap-northeast-2"
TABLE = "rcs-acs-test"
IMSI = "001010000000001"
MSISDN = "+821012345678"
IMEI = "356938035643809"


def create_table() -> None:
    """Create the table exactly as infra/app.yaml declares it."""
    client = boto3.client("dynamodb", region_name=REGION)
    client.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "gsi1pk", "AttributeType": "S"},
            {"AttributeName": "gsi1sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi1",
                "KeySchema": [
                    {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                    {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    client.update_time_to_live(
        TableName=TABLE,
        TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"},
    )


@pytest.fixture
def ddb_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[DynamoDbStore]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        create_table()
        yield DynamoDbStore(TABLE, REGION)


def test_health_check_describes_the_table(ddb_store: DynamoDbStore) -> None:
    assert ddb_store.health() is True


def test_subscriber_round_trip(ddb_store: DynamoDbStore) -> None:
    ddb_store.put_subscriber(
        Subscriber(
            imsi=IMSI,
            msisdn=MSISDN,
            provisioning_version=3,
            imei_allowlist=[IMEI],
            overrides={"a/b": "c"},
        )
    )
    loaded = ddb_store.get_subscriber(IMSI)
    assert loaded is not None
    assert loaded.msisdn == MSISDN
    # DynamoDB returns Decimal; the store must give back plain ints.
    assert isinstance(loaded.provisioning_version, int)
    assert loaded.provisioning_version == 3
    assert loaded.imei_allowlist == [IMEI]
    assert loaded.overrides == {"a/b": "c"}


def test_lookup_by_msisdn_uses_the_reverse_index(ddb_store: DynamoDbStore) -> None:
    ddb_store.put_subscriber(Subscriber(imsi=IMSI, msisdn=MSISDN))
    found = ddb_store.get_subscriber_by_msisdn(MSISDN)
    assert found is not None and found.imsi == IMSI
    assert ddb_store.get_subscriber_by_msisdn("+821099999999") is None


def test_changing_the_msisdn_moves_the_reverse_index(ddb_store: DynamoDbStore) -> None:
    ddb_store.put_subscriber(Subscriber(imsi=IMSI, msisdn=MSISDN))
    ddb_store.put_subscriber(Subscriber(imsi=IMSI, msisdn="+821087654321"))
    assert ddb_store.get_subscriber_by_msisdn(MSISDN) is None
    assert ddb_store.get_subscriber_by_msisdn("+821087654321") is not None


def test_delete_removes_both_items(ddb_store: DynamoDbStore) -> None:
    ddb_store.put_subscriber(Subscriber(imsi=IMSI, msisdn=MSISDN))
    ddb_store.delete_subscriber(IMSI)
    assert ddb_store.get_subscriber(IMSI) is None
    assert ddb_store.get_subscriber_by_msisdn(MSISDN) is None


def test_listing_uses_the_secondary_index(ddb_store: DynamoDbStore) -> None:
    for index in range(3):
        ddb_store.put_subscriber(
            Subscriber(imsi=f"00101000000000{index}", msisdn=f"+8210000000{index}")
        )
    assert len(ddb_store.list_subscribers()) == 3


def test_otp_challenge_round_trip_and_ttl(ddb_store: DynamoDbStore) -> None:
    now = int(time.time())
    ddb_store.put_otp(
        OtpChallenge(
            msisdn=MSISDN,
            otp_hash="abc",
            imsi=IMSI,
            created_at=now,
            expires_at=now + 300,
            sms_port=37273,
        )
    )
    loaded = ddb_store.get_otp(MSISDN)
    assert loaded is not None
    assert loaded.otp_hash == "abc"
    assert loaded.sms_port == 37273
    assert isinstance(loaded.expires_at, int)
    ddb_store.delete_otp(MSISDN)
    assert ddb_store.get_otp(MSISDN) is None


def test_otp_send_quota_counting(ddb_store: DynamoDbStore) -> None:
    assert ddb_store.count_otp_sends_today(MSISDN) == 0
    ddb_store.record_otp_send(MSISDN)
    assert ddb_store.count_otp_sends_today(MSISDN) == 1


def test_token_round_trip_and_revocation(ddb_store: DynamoDbStore) -> None:
    now = int(time.time())
    ddb_store.put_token(
        TokenRecord(token_hash="hash1", imsi=IMSI, imei=IMEI, issued_at=now, expires_at=now + 3600)
    )
    loaded = ddb_store.get_token("hash1")
    assert loaded is not None and loaded.imsi == IMSI
    ddb_store.revoke_token("hash1")
    revoked = ddb_store.get_token("hash1")
    assert revoked is not None and revoked.revoked is True


def test_revoking_a_missing_token_is_silent(ddb_store: DynamoDbStore) -> None:
    ddb_store.revoke_token("absent")


def test_revoke_all_tokens_for_an_imsi(ddb_store: DynamoDbStore) -> None:
    now = int(time.time())
    for index in range(3):
        ddb_store.put_token(
            TokenRecord(
                token_hash=f"hash{index}",
                imsi=IMSI,
                imei=IMEI,
                issued_at=now + index,
                expires_at=now + 3600,
            )
        )
    assert ddb_store.revoke_tokens_for_imsi(IMSI) == 3
    assert ddb_store.revoke_tokens_for_imsi(IMSI) == 0


def test_device_round_trip_and_listing(ddb_store: DynamoDbStore) -> None:
    ddb_store.put_device(
        Device(device_id=IMEI, imsi=IMSI, model="SimPhone", mo_values={"./DevInfo/Man": "Sim"})
    )
    loaded = ddb_store.get_device(IMEI)
    assert loaded is not None
    assert loaded.model == "SimPhone"
    assert loaded.mo_values["./DevInfo/Man"] == "Sim"
    assert len(ddb_store.list_devices()) == 1


def test_dm_session_round_trip(ddb_store: DynamoDbStore) -> None:
    ddb_store.put_dm_session(
        DmSession(
            session_id="42",
            device_id=IMEI,
            imsi=IMSI,
            phase="devinfo",
            expires_at=int(time.time()) + 600,
        )
    )
    loaded = ddb_store.get_dm_session("42")
    assert loaded is not None
    assert loaded.phase == "devinfo"
    ddb_store.delete_dm_session("42")
    assert ddb_store.get_dm_session("42") is None


def test_expired_dm_session_is_treated_as_absent(ddb_store: DynamoDbStore) -> None:
    # DynamoDB TTL deletion is asynchronous, so the store must also check.
    ddb_store.put_dm_session(
        DmSession(session_id="old", device_id=IMEI, expires_at=int(time.time()) - 10)
    )
    assert ddb_store.get_dm_session("old") is None


def test_sms_outbox_round_trip(ddb_store: DynamoDbStore) -> None:
    ddb_store.record_sms(SmsMessage(msisdn=MSISDN, body="code 123456"))
    messages = ddb_store.list_sms(MSISDN)
    assert len(messages) == 1
    assert messages[0].body == "code 123456"


def test_sms_outbox_is_not_scannable_without_an_msisdn(ddb_store: DynamoDbStore) -> None:
    ddb_store.record_sms(SmsMessage(msisdn=MSISDN, body="code"))
    assert ddb_store.list_sms(None) == []


def test_missing_subscriber_returns_none(ddb_store: DynamoDbStore) -> None:
    assert ddb_store.get_subscriber("001019999999999") is None
    assert ddb_store.get_device("absent") is None
    assert ddb_store.get_token("absent") is None
