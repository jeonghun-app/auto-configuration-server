"""Persistence port.

Two implementations exist: :class:`acs.store.memory.MemoryStore` (dev and unit
tests) and :class:`acs.store.dynamodb.DynamoDbStore` (the AWS-native backend,
also used locally against the ``amazon/dynamodb-local`` container).
"""

from __future__ import annotations

import dataclasses
from typing import Literal, Protocol, runtime_checkable

from acs.domain.models import Device, DmSession, OtpChallenge, SmsMessage, Subscriber, TokenRecord


@dataclasses.dataclass(frozen=True, slots=True)
class OtpIssueRefused:
    """Why :meth:`Store.issue_otp` did not store a challenge."""

    reason: Literal["cooldown", "daily_quota"]
    retry_after: int


@runtime_checkable
class Store(Protocol):
    """Everything the ACS needs to persist."""

    # ---- subscribers ------------------------------------------------------
    def get_subscriber(self, imsi: str) -> Subscriber | None: ...
    def get_subscriber_by_msisdn(self, msisdn: str) -> Subscriber | None: ...
    def put_subscriber(self, subscriber: Subscriber) -> None: ...
    def delete_subscriber(self, imsi: str) -> None: ...
    def list_subscribers(self, limit: int = 100) -> list[Subscriber]: ...

    # ---- OTP challenges ---------------------------------------------------
    def put_otp(self, challenge: OtpChallenge) -> None: ...
    def get_otp(self, msisdn: str) -> OtpChallenge | None: ...
    def delete_otp(self, msisdn: str) -> None: ...

    # The two operations below are the only ones the OTP flow may use to change
    # a challenge. Each is atomic per MSISDN, also across ECS tasks: a separate
    # read and write would let concurrent requests exceed the daily cap or
    # verify one code twice.
    def issue_otp(
        self,
        challenge: OtpChallenge,
        cooldown_seconds: int,
        max_sends_per_day: int,
        now: int,
    ) -> OtpIssueRefused | None:
        """Store ``challenge`` and count the send, unless the cooldown or cap applies.

        The cooldown applies while an unconsumed, unexpired challenge younger than
        ``cooldown_seconds`` exists. The cap counts sends in the 24 hours before
        ``now``.
        """
        ...

    def replace_otp(self, expected: OtpChallenge, replacement: OtpChallenge | None) -> bool:
        """Swap the stored challenge for ``replacement`` (``None`` deletes it).

        Succeeds only if the stored challenge is still ``expected`` — same code,
        creation time and attempt count — and returns whether it did.
        """
        ...

    # ---- tokens -----------------------------------------------------------
    def put_token(self, record: TokenRecord) -> None: ...
    def get_token(self, token_hash: str) -> TokenRecord | None: ...
    def revoke_token(self, token_hash: str) -> None: ...
    def revoke_tokens_for_imsi(self, imsi: str) -> int: ...

    # ---- devices (OMA-DM) -------------------------------------------------
    def put_device(self, device: Device) -> None: ...
    def get_device(self, device_id: str) -> Device | None: ...
    def list_devices(self, limit: int = 100) -> list[Device]: ...

    # ---- DM sessions ------------------------------------------------------
    def put_dm_session(self, session: DmSession) -> None: ...
    def get_dm_session(self, session_id: str) -> DmSession | None: ...
    def delete_dm_session(self, session_id: str) -> None: ...

    # ---- mock SMS outbox (dev only) --------------------------------------
    def record_sms(self, message: SmsMessage) -> None: ...
    def list_sms(self, msisdn: str | None = None, limit: int = 50) -> list[SmsMessage]: ...

    # ---- health -----------------------------------------------------------
    def health(self) -> bool: ...
