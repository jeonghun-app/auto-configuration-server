"""The OTP flows that predate #11 behave as they did, on both stores.

#11 replaced the read-check-write OTP code with the atomic issue_otp/replace_otp
store operations and added new failure answers. These tests pin the behaviour a
client already relies on — text OTP, the resend cooldown, the daily cap, three
wrong guesses, the 511 recovery and the MSISDN web flow's refusal to reveal which
numbers exist — and run every one against the in-memory store and DynamoDB (moto).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN
from tests.test_otp_atomicity import otp_store  # noqa: F401 - shared fixture

from acs.app import create_app
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.domain.service import ProvisioningService
from acs.protocol.request import ConfigQuery
from acs.sms.base import SmsDeliveryFailed, SmsRequest, SmsResult
from acs.store.base import OtpStoreContention, Store


class RecordingSender:
    """Records every request; optionally fails like an SMSC outage."""

    name = "recording"

    def __init__(self, fail: bool = False) -> None:
        self.requests: list[SmsRequest] = []
        self.fail = fail

    def send(self, request: SmsRequest) -> SmsResult:
        self.requests.append(request)
        if self.fail:
            raise SmsDeliveryFailed("SMSC did not answer")
        return SmsResult(self.name, f"ref-{len(self.requests)}", request.requires_binary)

    def last_otp(self) -> str:
        return "".join(ch for ch in self.requests[-1].body if ch.isdigit())


def query(**extra: object) -> ConfigQuery:
    params: dict[str, object] = {"imsi": TEST_IMSI, "imei": TEST_IMEI, "vers": 0}
    params.update(extra)
    return ConfigQuery(**params)  # type: ignore[arg-type]


@pytest.fixture
def seeded(otp_store: Store) -> Store:  # noqa: F811 - the imported fixture
    otp_store.put_subscriber(
        Subscriber(
            imsi=TEST_IMSI,
            msisdn=TEST_MSISDN,
            entitled=True,
            provisioning_version=1,
            rcs_profile="UP_2.4",
        )
    )
    return otp_store


def make_service(
    settings: Settings, store: Store, sender: RecordingSender, **update: object
) -> ProvisioningService:
    return ProvisioningService(settings.model_copy(update=update), store, sender)


# ------------------------------------------------------------------ text OTP
def test_a_text_otp_is_sent_once_and_completes_provisioning(
    settings: Settings, seeded: Store
) -> None:
    sender = RecordingSender()
    service = make_service(settings, seeded, sender)

    first = service.handle(query())
    assert (first.status_code, first.body, first.detail) == (200, b"", "otp_sent")
    assert len(sender.requests) == 1
    sent = sender.requests[0]
    assert sent.msisdn == TEST_MSISDN
    assert sent.sms_port is None
    assert not sent.requires_binary
    assert re.fullmatch(r"RCS activation code: \d{6}", sent.body)

    done = service.handle(query(otp=sender.last_otp()))
    assert done.status_code == 200
    assert done.version == 1
    assert seeded.get_otp(TEST_MSISDN) is None


def test_a_verified_code_cannot_be_replayed(settings: Settings, seeded: Store) -> None:
    sender = RecordingSender()
    service = make_service(settings, seeded, sender)
    service.handle(query())
    otp = sender.last_otp()
    assert service.handle(query(otp=otp)).status_code == 200
    replay = service.handle(query(otp=otp))
    assert (replay.status_code, replay.detail) == (511, "otp_no_challenge")


# ------------------------------------------------------------------ cooldown
def test_a_repeated_bootstrap_inside_the_cooldown_sends_nothing_new(
    settings: Settings, seeded: Store
) -> None:
    sender = RecordingSender()
    service = make_service(settings, seeded, sender)
    service.handle(query())
    otp = sender.last_otp()

    again = service.handle(query())
    assert (again.status_code, again.body, again.metric) == (200, b"", "OtpPendingReuse")
    assert again.headers["Content-Length"] == "0"
    assert len(sender.requests) == 1
    # The outstanding code still works.
    assert service.handle(query(otp=otp)).status_code == 200


def test_a_bootstrap_after_the_cooldown_replaces_the_code(
    settings: Settings, seeded: Store
) -> None:
    sender = RecordingSender()
    service = make_service(settings, seeded, sender, otp_resend_cooldown_seconds=0)
    service.handle(query())
    old = seeded.get_otp(TEST_MSISDN)
    service.handle(query())
    new = seeded.get_otp(TEST_MSISDN)

    assert len(sender.requests) == 2
    assert old is not None and new is not None
    assert old.challenge_id != new.challenge_id
    assert service.handle(query(otp=sender.last_otp())).status_code == 200


# ----------------------------------------------------------------- daily cap
def test_the_daily_cap_answers_429_and_sends_nothing(settings: Settings, seeded: Store) -> None:
    sender = RecordingSender()
    service = make_service(
        settings,
        seeded,
        sender,
        otp_resend_cooldown_seconds=0,
        otp_max_sends_per_msisdn_per_day=2,
    )
    assert service.handle(query()).detail == "otp_sent"
    assert service.handle(query()).detail == "otp_sent"
    capped = service.handle(query())
    assert capped.status_code == 429
    assert capped.headers["Retry-After"] == "3600"
    assert capped.detail == "daily_quota"
    assert len(sender.requests) == 2
    # The cap does not touch the code already on its way.
    assert service.handle(query(otp=sender.last_otp())).status_code == 200


def test_a_failed_delivery_still_counts_towards_the_daily_cap(
    settings: Settings, seeded: Store
) -> None:
    # As before #11: the send is counted when the challenge is issued, so an SMSC
    # that keeps failing cannot be used to retry without bound.
    sender = RecordingSender(fail=True)
    service = make_service(
        settings,
        seeded,
        sender,
        otp_resend_cooldown_seconds=0,
        otp_max_sends_per_msisdn_per_day=1,
    )
    assert service.handle(query()).detail == "otp_delivery_failed"
    assert service.handle(query()).status_code == 429
    assert len(sender.requests) == 1


# -------------------------------------------------------- three wrong guesses
def test_three_wrong_guesses_exhaust_the_code_and_a_fresh_one_is_issued(
    settings: Settings, seeded: Store
) -> None:
    sender = RecordingSender()
    service = make_service(settings, seeded, sender)
    service.handle(query())
    good = sender.last_otp()
    wrong = "000000" if good != "000000" else "111111"

    details = [service.handle(query(otp=wrong)).detail for _ in range(3)]
    assert details == ["otp_mismatch", "otp_mismatch", "otp_exhausted"]
    assert seeded.get_otp(TEST_MSISDN) is None

    # The right code no longer works once the attempts are spent.
    late = service.handle(query(otp=good))
    assert (late.status_code, late.detail) == (511, "otp_no_challenge")

    # An exhausted challenge is gone, so it holds no cooldown: a new code is sent.
    fresh = service.handle(query())
    assert fresh.detail == "otp_sent"
    assert len(sender.requests) == 2
    assert service.handle(query(otp=sender.last_otp())).status_code == 200


def test_each_wrong_guess_answers_511_with_a_challenge(settings: Settings, seeded: Store) -> None:
    sender = RecordingSender()
    service = make_service(settings, seeded, sender)
    service.handle(query())
    outcome = service.handle(query(otp="999999" if sender.last_otp() != "999999" else "888888"))
    assert outcome.status_code == 511
    assert outcome.metric == "Challenge511"
    stored = seeded.get_otp(TEST_MSISDN)
    assert stored is not None and stored.attempts == 1


# -------------------------------------------------------------- 511 recovery
def test_after_a_511_the_client_rebootstraps_and_the_pending_code_still_works(
    settings: Settings, seeded: Store
) -> None:
    sender = RecordingSender()
    service = make_service(settings, seeded, sender)
    service.handle(query())
    good = sender.last_otp()

    rejected = service.handle(query(otp="000000" if good != "000000" else "111111"))
    assert rejected.status_code == 511
    # RCC.14: on 511 the client starts over without credentials.
    restart = service.handle(query())
    assert (restart.status_code, restart.body, restart.metric) == (200, b"", "OtpPendingReuse")
    assert len(sender.requests) == 1
    assert service.handle(query(otp=good)).status_code == 200


def test_a_delivery_failure_lets_the_client_retry_at_once(
    settings: Settings, seeded: Store
) -> None:
    sender = RecordingSender(fail=True)
    service = make_service(settings, seeded, sender)
    failed = service.handle(query())
    assert (failed.status_code, failed.headers["Retry-After"]) == (503, "60")
    assert seeded.get_otp(TEST_MSISDN) is None

    # The discarded challenge holds no cooldown, so the retry sends a new code.
    sender.fail = False
    retried = service.handle(query())
    assert retried.detail == "otp_sent"
    assert service.handle(query(otp=sender.last_otp())).status_code == 200


# --------------------------------------------- MSISDN web flow, no enumeration
UNKNOWN_MSISDN = "+821099999999"


@pytest.fixture
def web(settings: Settings, seeded: Store) -> Iterator[TestClient]:
    with TestClient(create_app(settings, seeded)) as client:
        yield client


def submit(client: TestClient, msisdn: str) -> tuple[int, str]:
    page = client.get("/msisdn")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    response = client.post("/msisdn", data={"msisdn": msisdn, "csrf": csrf})
    # Only the fresh CSRF token and the echoed number may differ.
    text = re.sub(r'name="csrf" value="[^"]+"', 'name="csrf" value=""', response.text)
    return response.status_code, text.replace(msisdn, "<msisdn>")


def test_the_web_flow_answers_alike_for_every_reason_a_code_is_or_is_not_sent(
    settings: Settings, seeded: Store, web: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = web.app.state.acs  # type: ignore[attr-defined]
    sender = RecordingSender()
    state.sms = sender
    tight = settings.model_copy(update={"otp_max_sends_per_msisdn_per_day": 1})

    reference = submit(web, UNKNOWN_MSISDN)
    answers = {"unknown": reference}
    answers["sent"] = submit(web, TEST_MSISDN)
    answers["cooldown"] = submit(web, TEST_MSISDN)
    assert len(sender.requests) == 1

    state.settings = tight
    current = seeded.get_otp(TEST_MSISDN)
    assert current is not None
    seeded.replace_otp(current, None)
    answers["daily_cap"] = submit(web, TEST_MSISDN)
    assert len(sender.requests) == 1

    state.settings = settings
    sender.fail = True
    other = "+821011112222"
    seeded.put_subscriber(
        Subscriber(imsi="001010000000002", msisdn=other, entitled=True, rcs_profile="UP_2.4")
    )
    status, text = submit(web, other)
    answers["delivery_failed"] = (status, text)
    assert seeded.get_otp(other) is None

    def contended(*_args: object, **_kwargs: object) -> None:
        raise OtpStoreContention("busy")

    monkeypatch.setattr(seeded, "issue_otp", contended)
    answers["contention"] = submit(web, other)

    for reason, answer in answers.items():
        assert answer == reference, reason


def test_the_web_flow_verifies_a_code_and_refuses_an_unknown_number_alike(
    web: TestClient,
) -> None:
    state = web.app.state.acs  # type: ignore[attr-defined]
    sender = RecordingSender()
    state.sms = sender
    page = web.get("/msisdn")
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]
    submitted = web.post("/msisdn", data={"msisdn": TEST_MSISDN, "csrf": csrf})
    csrf = submitted.text.split('name="csrf" value="')[1].split('"')[0]
    good = sender.last_otp()

    unknown = web.post("/msisdn/verify", data={"msisdn": UNKNOWN_MSISDN, "otp": good, "csrf": csrf})
    wrong = web.post(
        "/msisdn/verify",
        data={"msisdn": TEST_MSISDN, "otp": "000000" if good != "000000" else "1", "csrf": csrf},
    )
    assert (unknown.status_code, unknown.text) == (wrong.status_code, wrong.text)
    verified = web.post("/msisdn/verify", data={"msisdn": TEST_MSISDN, "otp": good, "csrf": csrf})
    assert verified.status_code == 200
    assert "verified" in verified.text.lower()
