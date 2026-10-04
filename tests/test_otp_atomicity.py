"""OTP issue and verification stay atomic under concurrent requests.

The configuration flow runs in a worker thread, and in production several ECS
tasks share one table, so two requests for the same MSISDN really do overlap.
Each test lines requests up on a barrier and asserts the store contract: one
send under a cap of one, one verification per code, one attempt per guess. The
same contract is asserted on the in-memory store and on DynamoDB (moto).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

import pytest
from botocore.exceptions import ClientError
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN
from tests.test_store_dynamodb import REGION, TABLE, create_table

from acs.auth import otp as otp_mod
from acs.config import Settings
from acs.domain.models import OtpChallenge, Subscriber
from acs.domain.service import ProvisioningService
from acs.protocol.request import ConfigQuery
from acs.sms.base import MockSmsSender, SmsDeliveryFailed, SmsRequest, SmsResult
from acs.store.base import OtpIssueRefused, OtpStoreContention, Store
from acs.store.dynamodb import DynamoDbStore
from acs.store.memory import MemoryStore

T = TypeVar("T")
THREADS = 8
NOW = 1_000_000


def challenge(created_at: int = NOW, attempts: int = 0, otp_hash: str = "h1") -> OtpChallenge:
    return OtpChallenge(
        msisdn=TEST_MSISDN,
        otp_hash=otp_hash,
        imsi=TEST_IMSI,
        created_at=created_at,
        expires_at=created_at + 300,
        attempts=attempts,
    )


@pytest.fixture(params=["memory", pytest.param("dynamodb", marks=pytest.mark.aws)])
def otp_store(request: pytest.FixtureRequest) -> Iterator[Store]:
    if request.param == "memory":
        yield MemoryStore()
        return
    monkeypatch = request.getfixturevalue("monkeypatch")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    from moto import mock_aws
    from moto.core.botocore_stubber import BotocoreStubber

    # DynamoDB applies each request, a transaction included, atomically. moto's
    # backend has no locking, so without this its own check-then-write would race
    # and the test would measure moto rather than the store.
    serial = threading.Lock()
    process = BotocoreStubber.process_request

    def atomic_request(self: BotocoreStubber, req: object) -> object:
        with serial:
            return process(self, req)

    monkeypatch.setattr(BotocoreStubber, "process_request", atomic_request)

    with mock_aws():
        create_table()
        yield DynamoDbStore(TABLE, REGION)


def race(count: int, action: Callable[[], T]) -> list[T]:
    """Run ``action`` in ``count`` threads released together by a barrier."""
    barrier = threading.Barrier(count)
    results: list[T] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run() -> None:
        try:
            barrier.wait(timeout=10)
            result = action()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(result)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    if errors:
        raise errors[0]
    return results


# ------------------------------------------------------------ store contract
def test_issue_stores_the_challenge_and_counts_the_send(otp_store: Store) -> None:
    assert otp_store.issue_otp(challenge(), 60, 2, NOW) is None
    stored = otp_store.get_otp(TEST_MSISDN)
    assert stored is not None
    assert stored.otp_hash == "h1"


def test_issue_inside_the_cooldown_is_refused_with_the_remaining_wait(otp_store: Store) -> None:
    otp_store.issue_otp(challenge(), 60, 5, NOW)
    refused = otp_store.issue_otp(challenge(NOW + 10, otp_hash="h2"), 60, 5, NOW + 10)
    assert refused == OtpIssueRefused("cooldown", 50)
    stored = otp_store.get_otp(TEST_MSISDN)
    assert stored is not None
    assert stored.otp_hash == "h1"


def test_issue_after_the_cooldown_replaces_the_challenge(otp_store: Store) -> None:
    otp_store.issue_otp(challenge(), 60, 5, NOW)
    assert otp_store.issue_otp(challenge(NOW + 60, otp_hash="h2"), 60, 5, NOW + 60) is None
    stored = otp_store.get_otp(TEST_MSISDN)
    assert stored is not None
    assert stored.otp_hash == "h2"


def test_the_daily_cap_counts_sends_in_the_last_24_hours(otp_store: Store) -> None:
    assert otp_store.issue_otp(challenge(), 0, 2, NOW) is None
    assert otp_store.issue_otp(challenge(NOW + 1, otp_hash="h2"), 0, 2, NOW + 1) is None
    refused = otp_store.issue_otp(challenge(NOW + 2, otp_hash="h3"), 0, 2, NOW + 2)
    assert refused == OtpIssueRefused("daily_quota", 3600)
    # The first send ages out of the window a day later.
    later = NOW + 86401
    assert otp_store.issue_otp(challenge(later, otp_hash="h4"), 0, 2, later) is None


def test_replace_succeeds_only_against_the_challenge_that_was_read(otp_store: Store) -> None:
    otp_store.issue_otp(challenge(), 60, 5, NOW)
    read = otp_store.get_otp(TEST_MSISDN)
    assert read is not None
    assert otp_store.replace_otp(read, challenge(attempts=1)) is True
    # ``read`` is now stale: its attempt count no longer matches.
    assert otp_store.replace_otp(read, challenge(attempts=1)) is False
    assert otp_store.replace_otp(read, None) is False
    current = otp_store.get_otp(TEST_MSISDN)
    assert current is not None
    assert current.attempts == 1
    assert otp_store.replace_otp(current, None) is True
    assert otp_store.get_otp(TEST_MSISDN) is None
    assert otp_store.replace_otp(current, None) is False


# --------------------------------------------------------------- concurrency
@pytest.mark.parametrize("cap", [1, 3])
def test_concurrent_issues_never_exceed_the_daily_cap(otp_store: Store, cap: int) -> None:
    policy = otp_mod.OtpPolicy(resend_cooldown_seconds=0, max_sends_per_day=cap)

    def issue() -> str:
        try:
            otp_mod.create_challenge(otp_store, TEST_MSISDN, TEST_IMSI, policy, now=NOW)
        except otp_mod.SendBlocked as blocked:
            return blocked.reason
        return "sent"

    outcomes = race(THREADS, issue)
    assert outcomes.count("sent") <= cap
    assert outcomes.count("sent") >= 1


def test_concurrent_issues_inside_the_cooldown_send_once(otp_store: Store) -> None:
    policy = otp_mod.OtpPolicy(resend_cooldown_seconds=60, max_sends_per_day=10)

    def issue() -> str:
        try:
            otp_mod.create_challenge(otp_store, TEST_MSISDN, TEST_IMSI, policy, now=NOW)
        except otp_mod.SendBlocked as blocked:
            return blocked.reason
        return "sent"

    outcomes = race(THREADS, issue)
    assert outcomes.count("sent") == 1
    assert set(outcomes) <= {"sent", "cooldown"}


def test_one_code_verifies_once_under_concurrent_requests(otp_store: Store) -> None:
    policy = otp_mod.OtpPolicy()
    _, clear = otp_mod.create_challenge(otp_store, TEST_MSISDN, TEST_IMSI, policy, now=NOW)
    outcomes = race(
        THREADS, lambda: otp_mod.verify_challenge(otp_store, TEST_MSISDN, clear, policy, now=NOW)
    )
    assert outcomes.count(otp_mod.VERIFIED) == 1
    assert otp_store.get_otp(TEST_MSISDN) is None


def test_concurrent_wrong_guesses_each_spend_an_attempt(otp_store: Store) -> None:
    policy = otp_mod.OtpPolicy(max_attempts=3)
    otp_mod.create_challenge(otp_store, TEST_MSISDN, TEST_IMSI, policy, now=NOW)
    outcomes = race(
        THREADS,
        lambda: otp_mod.verify_challenge(otp_store, TEST_MSISDN, "wrong!", policy, now=NOW),
    )
    # Three attempts in total: two mismatches and the exhausting one. A request
    # that lost every round is refused as a mismatch without spending one.
    assert otp_mod.VERIFIED not in outcomes
    assert outcomes.count(otp_mod.EXHAUSTED) == 1
    assert otp_store.get_otp(TEST_MSISDN) is None


# ---------------------------------------------- interleavings, made explicit
# The memory store holds its lock across read and write, so these interleavings
# can only happen on DynamoDB.
@pytest.fixture
def dynamo_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[DynamoDbStore]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    from moto import mock_aws

    with mock_aws():
        create_table()
        yield DynamoDbStore(TABLE, REGION)


@pytest.mark.aws
def test_a_write_between_read_and_commit_makes_the_loser_meet_the_cooldown(
    dynamo_store: DynamoDbStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    otp_store = dynamo_store
    original = otp_store._get_consistent
    interfered = False

    def interleaved(key: dict[str, str]) -> dict[str, object] | None:
        nonlocal interfered
        result = original(key)
        if not interfered and key["sk"] == "CHAL":
            interfered = True
            assert otp_store.issue_otp(challenge(otp_hash="rival"), 60, 5, NOW) is None
        return result

    monkeypatch.setattr(otp_store, "_get_consistent", interleaved)
    assert otp_store.issue_otp(challenge(otp_hash="mine"), 60, 5, NOW) == OtpIssueRefused(
        "cooldown", 60
    )
    stored = otp_store.get_otp(TEST_MSISDN)
    assert stored is not None
    assert stored.otp_hash == "rival"


@pytest.mark.aws
def test_a_store_that_always_conflicts_refuses_the_send(
    dynamo_store: DynamoDbStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    otp_store = dynamo_store
    otp_store.issue_otp(challenge(NOW - 3600), 60, 5, NOW - 3600)
    stale = otp_store._get_consistent({"pk": f"OTP#{TEST_MSISDN}", "sk": "CHAL"})
    assert stale is not None
    # Every round reads a challenge that no longer matches the stored one.
    monkeypatch.setattr(
        otp_store,
        "_get_consistent",
        lambda key: {**stale, "attempts": 7} if key["sk"] == "CHAL" else None,
    )
    # Losing every round is contention, not a cooldown: "pending" would leave the
    # client waiting for an SMS that was never sent.
    with pytest.raises(OtpStoreContention):
        otp_store.issue_otp(challenge(otp_hash="new"), 60, 5, NOW)


def test_verification_that_loses_every_round_is_not_verified(store: MemoryStore) -> None:
    policy = otp_mod.OtpPolicy()
    _, clear = otp_mod.create_challenge(store, TEST_MSISDN, TEST_IMSI, policy, now=NOW)

    class AlwaysStale(MemoryStore):
        def __init__(self, inner: MemoryStore) -> None:
            self._inner = inner

        def get_otp(self, msisdn: str) -> OtpChallenge | None:
            return self._inner.get_otp(msisdn)

        def replace_otp(self, expected: OtpChallenge, replacement: OtpChallenge | None) -> bool:
            return False

    outcome = otp_mod.verify_challenge(AlwaysStale(store), TEST_MSISDN, clear, policy, now=NOW)
    assert outcome == otp_mod.MISMATCH
    assert store.get_otp(TEST_MSISDN) is not None


# ------------------------------------------------------------- service level
def test_concurrent_bootstrap_requests_send_one_otp_under_a_cap_of_one(
    settings: Settings,
) -> None:
    """The reviewer's reproduction: two requests with a cap of one both sent."""
    store = MemoryStore()
    store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, entitled=True))
    tight = settings.model_copy(
        update={"otp_resend_cooldown_seconds": 0, "otp_max_sends_per_msisdn_per_day": 1}
    )
    service = ProvisioningService(tight, store, MockSmsSender(store))
    query = ConfigQuery(imsi=TEST_IMSI, imei=TEST_IMEI, vers=0)
    metrics = race(THREADS, lambda: service.handle(query).metric)
    assert metrics.count("OtpSent") == 1
    assert len(store.list_sms(TEST_MSISDN)) == 1


# ------------------------------------------------- transaction cancellations
def cancelled(*codes: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
            "CancellationReasons": [{"Code": code} for code in codes],
        },
        "TransactWriteItems",
    )


@pytest.mark.aws
@pytest.mark.parametrize(
    "codes",
    [
        ("ProvisionedThroughputExceeded", "None"),
        ("ThrottlingError", "None"),
        ("ValidationError", "None"),
        ("ConditionalCheckFailed", "ThrottlingError"),
    ],
    ids=["throughput", "throttling", "validation", "condition-and-throttling"],
)
def test_a_cancellation_for_any_other_reason_is_raised_not_retried(
    dynamo_store: DynamoDbStore, monkeypatch: pytest.MonkeyPatch, codes: tuple[str, ...]
) -> None:
    calls: list[int] = []

    def fail(**_kwargs: object) -> None:
        calls.append(1)
        raise cancelled(*codes)

    monkeypatch.setattr(dynamo_store._table.meta.client, "transact_write_items", fail)
    with pytest.raises(ClientError):
        dynamo_store.issue_otp(challenge(), 60, 5, NOW)
    assert len(calls) == 1


@pytest.mark.aws
def test_a_cancellation_on_conditions_alone_is_retried_as_contention(
    dynamo_store: DynamoDbStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def fail(**_kwargs: object) -> None:
        calls.append(1)
        raise cancelled("ConditionalCheckFailed", "None")

    monkeypatch.setattr(dynamo_store._table.meta.client, "transact_write_items", fail)
    with pytest.raises(OtpStoreContention):
        dynamo_store.issue_otp(challenge(), 60, 5, NOW)
    assert len(calls) == 5


@pytest.mark.aws
def test_a_real_condition_failure_reports_conditional_check_failed(
    dynamo_store: DynamoDbStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guards the parsing above against the shape moto/botocore actually produce.
    seen: list[ClientError] = []
    original = dynamo_store._table.meta.client.transact_write_items

    def spy(**kwargs: Any) -> Any:
        try:
            return original(**kwargs)
        except ClientError as exc:
            seen.append(exc)
            raise

    dynamo_store.issue_otp(challenge(NOW - 3600), 60, 5, NOW - 3600)
    stale = dynamo_store._get_consistent({"pk": f"OTP#{TEST_MSISDN}", "sk": "CHAL"})
    assert stale is not None
    monkeypatch.setattr(dynamo_store._table.meta.client, "transact_write_items", spy)
    monkeypatch.setattr(
        dynamo_store,
        "_get_consistent",
        lambda key: {**stale, "attempts": 7} if key["sk"] == "CHAL" else None,
    )
    with pytest.raises(OtpStoreContention):
        dynamo_store.issue_otp(challenge(otp_hash="new"), 60, 5, NOW)
    reasons = [r["Code"] for r in seen[0].response["CancellationReasons"]]
    assert "ConditionalCheckFailed" in reasons


# --------------------------------------------------- legacy quota migration
@pytest.mark.aws
def test_the_first_quota_item_is_seeded_from_legacy_send_rows(
    dynamo_store: DynamoDbStore,
) -> None:
    # Rows in the format written before OTPQUOTA# existed: two within the last
    # day, one older. A cap of three leaves room for exactly one more send.
    for stamp in (NOW - 90000, NOW - 3600, NOW - 60):
        dynamo_store._table.put_item(
            Item={
                "pk": f"OTPSEND#{TEST_MSISDN}",
                "sk": str(stamp).zfill(12),
                "entity": "otp_send",
                "expires_at": stamp + 86400,
            }
        )
    assert dynamo_store.issue_otp(challenge(), 0, 3, NOW) is None
    refused = dynamo_store.issue_otp(challenge(NOW + 1, otp_hash="h2"), 0, 3, NOW + 1)
    assert refused == OtpIssueRefused("daily_quota", 3600)


# ------------------------------------------------------- failure cleanup
class LateFailingSender:
    """Fails only after the challenge it was sent for was exhausted and replaced."""

    name = "late"

    def __init__(self, store: Store, policy: otp_mod.OtpPolicy) -> None:
        self._store = store
        self._policy = policy
        self.replacement: OtpChallenge | None = None

    def send(self, request: SmsRequest) -> SmsResult:
        for _ in range(self._policy.max_attempts):
            otp_mod.verify_challenge(self._store, TEST_MSISDN, "wrong!", self._policy)
        self.replacement, _ = otp_mod.create_challenge(
            self._store, TEST_MSISDN, TEST_IMSI, self._policy
        )
        raise SmsDeliveryFailed("SMSC did not answer")


def test_a_late_send_failure_does_not_delete_a_newer_challenge(
    otp_store: Store, settings: Settings
) -> None:
    otp_store.put_subscriber(Subscriber(imsi=TEST_IMSI, msisdn=TEST_MSISDN, entitled=True))
    open_policy = settings.model_copy(update={"otp_resend_cooldown_seconds": 0})
    sender = LateFailingSender(otp_store, otp_mod.policy_from_settings(open_policy))
    service = ProvisioningService(open_policy, otp_store, sender)
    outcome = service.handle(ConfigQuery(imsi=TEST_IMSI, imei=TEST_IMEI, vers=0))
    assert outcome.metric == "OtpDeliveryFailed"
    survivor = otp_store.get_otp(TEST_MSISDN)
    assert sender.replacement is not None
    assert survivor is not None
    assert survivor.otp_hash == sender.replacement.otp_hash


def test_discarding_removes_the_failed_challenge_even_after_a_wrong_guess(
    otp_store: Store,
) -> None:
    policy = otp_mod.OtpPolicy()
    issued, _ = otp_mod.create_challenge(otp_store, TEST_MSISDN, TEST_IMSI, policy, now=NOW)
    otp_mod.verify_challenge(otp_store, TEST_MSISDN, "wrong!", policy, now=NOW)
    assert otp_mod.discard_challenge(otp_store, issued) is True
    assert otp_store.get_otp(TEST_MSISDN) is None
    assert otp_mod.discard_challenge(otp_store, issued) is False


def test_contention_answers_503_without_a_pending_signal(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    class Contended(MemoryStore):
        def issue_otp(self, *args: Any, **kwargs: Any) -> OtpIssueRefused | None:
            raise OtpStoreContention("busy")

    store = Contended()
    for item in seeded_store.list_subscribers():
        store.put_subscriber(item)
    service = ProvisioningService(settings, store, MockSmsSender(store))
    outcome = service.handle(ConfigQuery(imsi=TEST_IMSI, imei=TEST_IMEI, vers=0))
    assert outcome.status_code == 503
    assert outcome.headers["Retry-After"] == "5"
    assert (outcome.metric, outcome.detail) == ("OtpStoreContention", "otp_store_contention")
    assert store.list_sms(TEST_MSISDN) == []


@pytest.mark.aws
def test_a_send_by_this_version_is_counted_by_the_legacy_query(
    dynamo_store: DynamoDbStore,
) -> None:
    """During a rolling deployment 1.3 tasks count the cap from OTPSEND# rows."""
    from boto3.dynamodb.conditions import Key

    now = int(time.time())
    assert dynamo_store.issue_otp(challenge(now), 0, 5, now) is None
    # 1.3's count_otp_sends_today, verbatim in effect.
    cutoff = int(time.time()) - 86400
    response = dynamo_store._table.query(
        KeyConditionExpression=Key("pk").eq(f"OTPSEND#{TEST_MSISDN}")
        & Key("sk").gte(str(cutoff).zfill(12)),
        Select="COUNT",
    )
    assert response["Count"] == 1
    row = dynamo_store._table.get_item(
        Key={"pk": f"OTPSEND#{TEST_MSISDN}", "sk": str(now).zfill(12)}
    )["Item"]
    assert row["entity"] == "otp_send"
    assert int(row["expires_at"]) == now + 86400
