"""SMS delivery."""

from __future__ import annotations

from acs.config import Settings
from acs.sms.base import (
    MockSmsSender,
    SmsRequest,
    SmsResult,
    SmsSender,
    UnsupportedDelivery,
)
from acs.store.base import Store

__all__ = [
    "MockSmsSender",
    "SmsRequest",
    "SmsResult",
    "SmsSender",
    "UnsupportedDelivery",
    "build_sms_sender",
]


def build_sms_sender(settings: Settings, store: Store) -> SmsSender:
    """Return the configured SMS sender."""
    if settings.sms_provider == "eum":
        from acs.sms.aws import EndUserMessagingSender

        return EndUserMessagingSender(
            region_name=settings.aws_region,
            origination_identity=settings.sms_origination_identity,
            store=store,
        )
    if settings.sms_provider == "sns":
        from acs.sms.aws import SnsSmsSender

        return SnsSmsSender(
            region_name=settings.aws_region,
            sender_id=settings.sms_sender_id,
            store=store,
        )
    return MockSmsSender(store)
