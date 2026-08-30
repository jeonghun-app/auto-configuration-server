"""AWS-native SMS providers.

* :class:`EndUserMessagingSender` uses AWS End User Messaging SMS
  (``pinpoint-sms-voice-v2`` API, ``SendTextMessage``). This is the current AWS
  service for transactional SMS and the recommended choice.
* :class:`SnsSmsSender` uses ``sns:Publish`` with a ``PhoneNumber``. Simpler to
  set up, fewer delivery controls.

Neither can send a port-addressed binary SMS, so both refuse when ``SMS_port`` is
requested instead of sending a message the RCS client cannot consume.
"""

from __future__ import annotations

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from acs.domain.models import SmsMessage
from acs.observability import get_logger
from acs.sms.base import SmsRequest, SmsResult, UnsupportedDelivery
from acs.store.base import Store

log = get_logger(__name__)

_BOTO = BotoConfig(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=2)

_BINARY_UNSUPPORTED = (
    "SMS_port={port} requires a port-addressed binary SMS (UDH). AWS SMS "
    "services send text only; use an operator SMSC over SMPP for silent OTP "
    "delivery. See docs/limitations.md."
)


class EndUserMessagingSender:
    """AWS End User Messaging SMS sender."""

    name = "eum"

    def __init__(
        self,
        region_name: str,
        origination_identity: str,
        store: Store | None = None,
    ) -> None:
        self._client = boto3.client("pinpoint-sms-voice-v2", region_name=region_name, config=_BOTO)
        self._origination_identity = origination_identity
        self._store = store

    def send(self, request: SmsRequest) -> SmsResult:
        if request.requires_binary:
            raise UnsupportedDelivery(_BINARY_UNSUPPORTED.format(port=request.sms_port))
        params = {
            "DestinationPhoneNumber": request.msisdn,
            "MessageBody": request.body,
            "MessageType": "TRANSACTIONAL",
        }
        if self._origination_identity:
            params["OriginationIdentity"] = self._origination_identity
        try:
            response = self._client.send_text_message(**params)
        except ClientError as exc:
            log.error("end user messaging send failed", extra={"error": str(exc)})
            raise
        message_id = str(response.get("MessageId", ""))
        self._audit(request, message_id)
        return SmsResult(self.name, message_id)

    def _audit(self, request: SmsRequest, message_id: str) -> None:
        if self._store is None:
            return
        # Audit the fact of the send, never the OTP body.
        self._store.record_sms(
            SmsMessage(
                msisdn=request.msisdn,
                body=f"<redacted:{message_id}>",
                sms_port=request.sms_port,
                provider=self.name,
            )
        )


class SnsSmsSender:
    """Amazon SNS SMS sender."""

    name = "sns"

    def __init__(self, region_name: str, sender_id: str = "", store: Store | None = None) -> None:
        self._client = boto3.client("sns", region_name=region_name, config=_BOTO)
        self._sender_id = sender_id
        self._store = store

    def send(self, request: SmsRequest) -> SmsResult:
        if request.requires_binary:
            raise UnsupportedDelivery(_BINARY_UNSUPPORTED.format(port=request.sms_port))
        attributes: dict[str, dict[str, str]] = {
            "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}
        }
        if self._sender_id:
            attributes["AWS.SNS.SMS.SenderID"] = {
                "DataType": "String",
                "StringValue": self._sender_id,
            }
        try:
            response = self._client.publish(
                PhoneNumber=request.msisdn,
                Message=request.body,
                MessageAttributes=attributes,
            )
        except ClientError as exc:
            log.error("sns sms send failed", extra={"error": str(exc)})
            raise
        message_id = str(response.get("MessageId", ""))
        if self._store is not None:
            self._store.record_sms(
                SmsMessage(
                    msisdn=request.msisdn,
                    body=f"<redacted:{message_id}>",
                    sms_port=request.sms_port,
                    provider=self.name,
                )
            )
        return SmsResult(self.name, message_id)
