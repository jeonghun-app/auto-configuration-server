"""SMS delivery port.

Two delivery modes matter for RCC.14:

**Text OTP** — the OTP arrives in the user's inbox and is typed in. Deliverable
with AWS End User Messaging SMS (formerly Amazon Pinpoint SMS) or Amazon SNS.

**Port-addressed OTP** — when the client supplies ``SMS_port``, the OTP must
arrive as a binary SMS carrying a User Data Header with that destination port so
the client reads it silently. *No AWS SMS service can send that.* It requires an
operator SMSC over SMPP. The interface below carries ``sms_port`` end to end and
the AWS providers raise :class:`UnsupportedDelivery` rather than silently
downgrading to a text message that the client would never see.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

from acs.domain.models import SmsMessage
from acs.store.base import Store


class UnsupportedDelivery(RuntimeError):
    """The provider cannot satisfy the requested delivery mode."""


@dataclasses.dataclass(frozen=True, slots=True)
class SmsRequest:
    msisdn: str
    body: str
    sms_port: int | None = None
    sender_id: str = ""

    @property
    def requires_binary(self) -> bool:
        return bool(self.sms_port)


@dataclasses.dataclass(frozen=True, slots=True)
class SmsResult:
    provider: str
    message_id: str
    binary: bool = False


class SmsSender(Protocol):
    name: str

    def send(self, request: SmsRequest) -> SmsResult: ...


class MockSmsSender:
    """Records messages in the store instead of sending them.

    Used by tests and local development, and readable through the ``/dev/sms``
    endpoint, which is refused outside a development environment.
    """

    name = "mock"

    def __init__(self, store: Store) -> None:
        self._store = store
        self._counter = 0

    def send(self, request: SmsRequest) -> SmsResult:
        self._counter += 1
        self._store.record_sms(
            SmsMessage(
                msisdn=request.msisdn,
                body=request.body,
                sms_port=request.sms_port,
                provider=self.name,
                binary=request.requires_binary,
            )
        )
        return SmsResult(self.name, f"mock-{self._counter}", request.requires_binary)
