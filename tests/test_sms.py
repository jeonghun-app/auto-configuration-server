"""SMS providers."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from moto import mock_aws

from acs.config import Settings
from acs.sms import build_sms_sender
from acs.sms.base import MockSmsSender, SmsRequest, UnsupportedDelivery
from acs.sms.smpp import SmppSmsSender
from acs.store.memory import MemoryStore

MSISDN = "+821012345678"
REGION = "ap-northeast-2"


def test_mock_sender_records_to_the_store(store: MemoryStore) -> None:
    sender = MockSmsSender(store)
    result = sender.send(SmsRequest(msisdn=MSISDN, body="code 123456"))
    assert result.provider == "mock"
    messages = store.list_sms(MSISDN)
    assert messages[0].body == "code 123456"


def test_mock_sender_records_the_binary_flag(store: MemoryStore) -> None:
    MockSmsSender(store).send(SmsRequest(msisdn=MSISDN, body="x", sms_port=37273))
    message = store.list_sms(MSISDN)[0]
    assert message.binary is True
    assert message.sms_port == 37273


def test_request_detects_the_binary_requirement() -> None:
    assert SmsRequest(msisdn=MSISDN, body="x", sms_port=37273).requires_binary is True
    assert SmsRequest(msisdn=MSISDN, body="x").requires_binary is False
    assert SmsRequest(msisdn=MSISDN, body="x", sms_port=0).requires_binary is False


def test_factory_selects_the_mock_provider(store: MemoryStore) -> None:
    settings = Settings(env="test", sms_provider="mock")
    assert build_sms_sender(settings, store).name == "mock"


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        yield


@pytest.mark.aws
def test_sns_sender_publishes_a_transactional_message(aws_env: None, store: MemoryStore) -> None:
    from acs.sms.aws import SnsSmsSender

    sender = SnsSmsSender(region_name=REGION, sender_id="RCS", store=store)
    result = sender.send(SmsRequest(msisdn=MSISDN, body="code 123456"))
    assert result.provider == "sns"
    # The audit trail must never contain the OTP itself.
    assert "123456" not in store.list_sms(MSISDN)[0].body


@pytest.mark.aws
def test_sns_sender_refuses_port_addressed_delivery(aws_env: None, store: MemoryStore) -> None:
    from acs.sms.aws import SnsSmsSender

    sender = SnsSmsSender(region_name=REGION, store=store)
    with pytest.raises(UnsupportedDelivery, match="port-addressed"):
        sender.send(SmsRequest(msisdn=MSISDN, body="x", sms_port=37273))


@pytest.mark.aws
def test_end_user_messaging_sender_refuses_port_addressed_delivery(aws_env: None) -> None:
    from acs.sms.aws import EndUserMessagingSender

    sender = EndUserMessagingSender(region_name=REGION, origination_identity="")
    with pytest.raises(UnsupportedDelivery, match="UDH"):
        sender.send(SmsRequest(msisdn=MSISDN, body="x", sms_port=37273))


@pytest.mark.aws
def test_factory_builds_the_aws_providers(aws_env: None, store: MemoryStore) -> None:
    assert build_sms_sender(Settings(env="test", sms_provider="sns"), store).name == "sns"
    assert build_sms_sender(Settings(env="test", sms_provider="eum"), store).name == "eum"


# ------------------------------------------------------------------- SMPP
def test_smpp_sender_is_explicitly_unimplemented() -> None:
    # Silently downgrading to text SMS would make the ACS look healthy while the
    # RCS client waits forever for a message it can read.
    with pytest.raises(NotImplementedError, match="operator SMSC"):
        SmppSmsSender().send(SmsRequest(msisdn=MSISDN, body="x", sms_port=37273))


def test_port_addressing_udh_is_built_correctly() -> None:
    # 06 05 04 <dest hi> <dest lo> <src hi> <src lo>
    assert SmppSmsSender.build_udh(37273) == bytes([0x06, 0x05, 0x04, 0x91, 0x99, 0x00, 0x00])
    assert SmppSmsSender.build_udh(0x1234, 0x5678) == bytes(
        [0x06, 0x05, 0x04, 0x12, 0x34, 0x56, 0x78]
    )


def test_udh_rejects_out_of_range_ports() -> None:
    with pytest.raises(ValueError, match="16 bits"):
        SmppSmsSender.build_udh(70000)
