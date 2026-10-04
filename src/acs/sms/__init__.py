"""SMS delivery."""

from __future__ import annotations

from acs.config import Settings
from acs.sms.base import (
    MockSmsSender,
    SmsDeliveryFailed,
    SmsRequest,
    SmsResult,
    SmsSender,
    UnsupportedDelivery,
)
from acs.store.base import Store

__all__ = [
    "MockSmsSender",
    "SmsDeliveryFailed",
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
    if settings.sms_provider == "smpp":
        from acs.sms.smpp import SmppSmsSender

        return SmppSmsSender(
            host=settings.smpp_host,
            port=settings.smpp_port,
            system_id=settings.smpp_system_id,
            password=settings.smpp_password,
            system_type=settings.smpp_system_type,
            source_addr=settings.smpp_source_addr,
            source_addr_ton=settings.smpp_source_addr_ton,
            source_addr_npi=settings.smpp_source_addr_npi,
            dest_addr_ton=settings.smpp_dest_addr_ton,
            dest_addr_npi=settings.smpp_dest_addr_npi,
            use_tls=settings.smpp_tls,
            tls_ca_file=settings.smpp_tls_ca_file,
            timeout=settings.smpp_timeout_seconds,
            store=store,
        )
    return MockSmsSender(store)
