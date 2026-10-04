"""SMS providers."""

from __future__ import annotations

import io
import json
import logging
import traceback
from collections.abc import Iterator

import pytest
from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from moto import mock_aws
from tests.conftest import TEST_IMEI, TEST_IMSI, base_query

from acs.config import Settings
from acs.domain.service import ProvisioningService
from acs.observability import JsonFormatter
from acs.sms import build_sms_sender
from acs.sms.aws import EndUserMessagingSender, SnsSmsSender
from acs.sms.base import MockSmsSender, SmsDeliveryFailed, SmsRequest, UnsupportedDelivery
from acs.sms.smpp import SmppSmsSender
from acs.store.memory import MemoryStore

MSISDN = "+821012345678"
REGION = "ap-northeast-2"
OTP = "424242"
BODY = f"RCS activation code: {OTP}"
AWS_ERROR_MESSAGE = f"Delivery refused for {MSISDN}: {BODY}"


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


@pytest.fixture(params=["sns", "eum"])
def aws_sender(
    request: pytest.FixtureRequest, aws_env: None, seeded_store: MemoryStore
) -> SnsSmsSender | EndUserMessagingSender:
    if request.param == "sns":
        return SnsSmsSender(region_name=REGION, store=seeded_store)
    return EndUserMessagingSender(region_name=REGION, origination_identity="", store=seeded_store)


@pytest.fixture(
    params=[
        "ThrottlingException",
        "OptedOutException",
        EndpointConnectionError,
        ConnectTimeoutError,
        ReadTimeoutError,
    ]
)
def aws_failure(
    request: pytest.FixtureRequest,
    aws_sender: SnsSmsSender | EndUserMessagingSender,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    failure = request.param
    if isinstance(failure, str):
        method = "publish" if aws_sender.name == "sns" else "send_text_message"
        with Stubber(aws_sender._client) as stubber:
            stubber.add_client_error(
                method, service_error_code=failure, service_message=AWS_ERROR_MESSAGE
            )
            yield failure
            stubber.assert_no_pending_responses()
    else:

        def fail_request(*args: object, **kwargs: object) -> None:
            raise failure(endpoint_url=AWS_ERROR_MESSAGE)

        monkeypatch.setattr(aws_sender._client._endpoint, "make_request", fail_request)
        yield failure.__name__


@pytest.fixture
def aws_log() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test", "mask", ""))
    logger = logging.getLogger("acs")
    saved = (logger.handlers, logger.propagate, logger.level)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    yield stream
    logger.handlers, logger.propagate, logger.level = saved


def assert_aws_failure_is_private(output: str, error: str) -> None:
    assert error in output
    for secret in (MSISDN, MSISDN.lstrip("+"), TEST_IMSI, TEST_IMEI, OTP, BODY, AWS_ERROR_MESSAGE):
        assert secret not in output


@pytest.mark.aws
def test_aws_failures_raise_a_delivery_failure_without_echoing_provider_details(
    aws_sender: SnsSmsSender | EndUserMessagingSender,
    aws_failure: str,
    aws_log: io.StringIO,
    seeded_store: MemoryStore,
) -> None:
    with pytest.raises(SmsDeliveryFailed) as caught:
        aws_sender.send(SmsRequest(msisdn=MSISDN, body=BODY))
    assert str(caught.value) == aws_failure
    output = aws_log.getvalue() + "".join(traceback.format_exception(caught.value))
    assert_aws_failure_is_private(output, aws_failure)
    assert seeded_store.list_sms(MSISDN) == []


@pytest.mark.aws
@pytest.mark.spec
def test_aws_delivery_failures_answer_503_and_discard_the_configuration_challenge(
    client: TestClient,
    settings: Settings,
    seeded_store: MemoryStore,
    aws_sender: SnsSmsSender | EndUserMessagingSender,
    aws_failure: str,
    aws_log: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("acs.auth.otp.generate_otp", lambda _length: OTP)
    state = client.app.state.acs  # type: ignore[attr-defined]
    state.provisioning = ProvisioningService(settings, seeded_store, aws_sender)
    response = client.get("/config", params=base_query())
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert response.content == b""
    assert seeded_store.get_otp(MSISDN) is None
    assert seeded_store.list_sms(MSISDN) == []
    records = [json.loads(line) for line in aws_log.getvalue().splitlines()]
    handled = next(
        record for record in records if record["message"] == "configuration request handled"
    )
    assert (handled["outcome"], handled["detail"]) == ("OtpDeliveryFailed", "otp_delivery_failed")
    metrics = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    assert any(metric.get("OtpDeliveryFailed") == 1 for metric in metrics)
    assert_aws_failure_is_private(aws_log.getvalue(), aws_failure)


@pytest.mark.aws
def test_aws_delivery_failures_discard_the_web_challenge_and_keep_the_success_page(
    client: TestClient,
    seeded_store: MemoryStore,
    aws_sender: SnsSmsSender | EndUserMessagingSender,
    aws_failure: str,
    aws_log: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("acs.auth.otp.generate_otp", lambda _length: OTP)
    monkeypatch.setattr("acs.api.msisdn_ui.secrets.token_urlsafe", lambda _nbytes: "test-csrf")
    state = client.app.state.acs  # type: ignore[attr-defined]
    state.sms = aws_sender
    client.get("/msisdn")
    data = {"msisdn": MSISDN, "csrf": "test-csrf"}
    failed = client.post("/msisdn", data=data)
    assert failed.status_code == 200
    assert "If that number is eligible" in failed.text
    assert seeded_store.get_otp(MSISDN) is None
    assert seeded_store.list_sms(MSISDN) == []
    assert_aws_failure_is_private(aws_log.getvalue(), aws_failure)
    state.sms = MockSmsSender(seeded_store)
    succeeded = client.post("/msisdn", data=data)
    assert succeeded.status_code == 200
    assert seeded_store.get_otp(MSISDN) is not None
    assert failed.content == succeeded.content


# ------------------------------------------------------------------- SMPP
# The session itself is tested against a fake SMSC in tests/test_smpp.py.
def test_port_addressing_udh_is_built_correctly() -> None:
    # 06 05 04 <dest hi> <dest lo> <src hi> <src lo>
    assert SmppSmsSender.build_udh(37273) == bytes([0x06, 0x05, 0x04, 0x91, 0x99, 0x00, 0x00])
    assert SmppSmsSender.build_udh(0x1234, 0x5678) == bytes(
        [0x06, 0x05, 0x04, 0x12, 0x34, 0x56, 0x78]
    )


def test_udh_rejects_out_of_range_ports() -> None:
    with pytest.raises(ValueError, match="16 bits"):
        SmppSmsSender.build_udh(70000)


@pytest.mark.aws
def test_an_aws_failure_logs_the_request_id_for_support(
    aws_sender: SnsSmsSender | EndUserMessagingSender, aws_log: io.StringIO
) -> None:
    method = "publish" if aws_sender.name == "sns" else "send_text_message"
    with Stubber(aws_sender._client) as stubber:
        stubber.add_client_error(
            method,
            service_error_code="ThrottlingException",
            service_message=AWS_ERROR_MESSAGE,
            response_meta={"RequestId": "req-0123456789abcdef"},
        )
        with pytest.raises(SmsDeliveryFailed):
            aws_sender.send(SmsRequest(msisdn=MSISDN, body=BODY))
    output = aws_log.getvalue()
    assert '"aws_request_id": "req-0123456789abcdef"' in output
    assert_aws_failure_is_private(output, "ThrottlingException")
