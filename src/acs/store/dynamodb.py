"""Amazon DynamoDB single-table store.

Table design (partition key ``pk``, sort key ``sk``)::

    SUB#<imsi>       META          subscriber record
    MSISDN#<msisdn>  SUB           reverse index -> imsi
    OTP#<msisdn>     CHAL          pending OTP challenge          (TTL)
    OTPSEND#<msisdn> <epoch>       OTP send audit for quotas      (TTL)
    TOKEN#<sha256>   META          provisioning token            (TTL)
    DEV#<device_id>  META          managed device
    DMSESS#<sid>     META          OMA-DM session state           (TTL)
    SMS#<msisdn>     <epoch>       mock SMS outbox (dev only)     (TTL)

One global secondary index, ``gsi1`` (``gsi1pk``/``gsi1sk``), supports
"all tokens for an IMSI" and the bounded admin listings. All expiring items
carry the ``expires_at`` attribute, which is configured as the table's TTL
attribute, so DynamoDB reclaims OTP challenges and DM sessions for free.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from acs.domain.models import Device, DmSession, OtpChallenge, SmsMessage, Subscriber, TokenRecord
from acs.observability import get_logger

log = get_logger(__name__)

_ENTITY_SUBSCRIBER = "subscriber"
_ENTITY_DEVICE = "device"


def _clean(value: Any) -> Any:
    """Convert DynamoDB numbers back to plain ints and drop empty strings."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _encode(item: dict[str, Any]) -> dict[str, Any]:
    """DynamoDB rejects float; keep ints and strings, drop ``None``."""
    out: dict[str, Any] = {}
    for key, value in item.items():
        if value is None:
            continue
        if isinstance(value, float):
            out[key] = Decimal(str(value))
        else:
            out[key] = value
    return out


class DynamoDbStore:
    """AWS-native :class:`acs.store.base.Store` implementation."""

    def __init__(
        self,
        table_name: str,
        region_name: str,
        endpoint_url: str = "",
        sms_retention_seconds: int = 3600,
    ) -> None:
        self._table_name = table_name
        self._sms_retention = sms_retention_seconds
        resource = boto3.resource(
            "dynamodb",
            region_name=region_name,
            endpoint_url=endpoint_url or None,
            config=BotoConfig(
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=2,
                read_timeout=3,
            ),
        )
        self._table = resource.Table(table_name)

    # ---- helpers ----------------------------------------------------------
    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"pk": pk, "sk": sk})
        item = response.get("Item")
        return _clean(item) if item else None

    def _put(self, item: dict[str, Any]) -> None:
        self._table.put_item(Item=_encode(item))

    def _delete(self, pk: str, sk: str) -> None:
        self._table.delete_item(Key={"pk": pk, "sk": sk})

    def _query_gsi1(self, gsi1pk: str, limit: int = 100) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key  # local import: optional dep path

        response = self._table.query(
            IndexName="gsi1",
            KeyConditionExpression=Key("gsi1pk").eq(gsi1pk),
            Limit=limit,
        )
        return [_clean(i) for i in response.get("Items", [])]

    # ---- subscribers ------------------------------------------------------
    def get_subscriber(self, imsi: str) -> Subscriber | None:
        item = self._get(f"SUB#{imsi}", "META")
        return Subscriber.from_item(item) if item else None

    def get_subscriber_by_msisdn(self, msisdn: str) -> Subscriber | None:
        item = self._get(f"MSISDN#{msisdn}", "SUB")
        if not item:
            return None
        return self.get_subscriber(str(item["imsi"]))

    def put_subscriber(self, subscriber: Subscriber) -> None:
        previous = self.get_subscriber(subscriber.imsi)
        subscriber.updated_at = int(time.time())
        item = subscriber.to_item()
        item.update(
            {
                "pk": f"SUB#{subscriber.imsi}",
                "sk": "META",
                "entity": _ENTITY_SUBSCRIBER,
                "gsi1pk": f"ENTITY#{_ENTITY_SUBSCRIBER}",
                "gsi1sk": subscriber.imsi,
            }
        )
        self._put(item)
        self._put(
            {
                "pk": f"MSISDN#{subscriber.msisdn}",
                "sk": "SUB",
                "imsi": subscriber.imsi,
                "entity": "msisdn_index",
            }
        )
        if previous and previous.msisdn != subscriber.msisdn:
            self._delete(f"MSISDN#{previous.msisdn}", "SUB")

    def delete_subscriber(self, imsi: str) -> None:
        subscriber = self.get_subscriber(imsi)
        self._delete(f"SUB#{imsi}", "META")
        if subscriber:
            self._delete(f"MSISDN#{subscriber.msisdn}", "SUB")

    def list_subscribers(self, limit: int = 100) -> list[Subscriber]:
        items = self._query_gsi1(f"ENTITY#{_ENTITY_SUBSCRIBER}", limit)
        return [Subscriber.from_item(i) for i in items]

    # ---- OTP --------------------------------------------------------------
    def put_otp(self, challenge: OtpChallenge) -> None:
        item = challenge.to_item()
        item.update({"pk": f"OTP#{challenge.msisdn}", "sk": "CHAL", "entity": "otp"})
        self._put(item)

    def get_otp(self, msisdn: str) -> OtpChallenge | None:
        item = self._get(f"OTP#{msisdn}", "CHAL")
        return OtpChallenge.from_item(item) if item else None

    def delete_otp(self, msisdn: str) -> None:
        self._delete(f"OTP#{msisdn}", "CHAL")

    def count_otp_sends_today(self, msisdn: str) -> int:
        from boto3.dynamodb.conditions import Key

        cutoff = int(time.time()) - 86400
        response = self._table.query(
            KeyConditionExpression=Key("pk").eq(f"OTPSEND#{msisdn}")
            & Key("sk").gte(str(cutoff).zfill(12)),
            Select="COUNT",
        )
        return int(response.get("Count", 0))

    def record_otp_send(self, msisdn: str) -> None:
        stamp = int(time.time())
        self._put(
            {
                "pk": f"OTPSEND#{msisdn}",
                "sk": str(stamp).zfill(12),
                "entity": "otp_send",
                "expires_at": stamp + 86400,
            }
        )

    # ---- tokens -----------------------------------------------------------
    def put_token(self, record: TokenRecord) -> None:
        item = record.to_item()
        item.update(
            {
                "pk": f"TOKEN#{record.token_hash}",
                "sk": "META",
                "entity": "token",
                "gsi1pk": f"TOKENIMSI#{record.imsi}",
                "gsi1sk": str(record.issued_at),
                "expires_at": record.expires_at,
            }
        )
        self._put(item)

    def get_token(self, token_hash: str) -> TokenRecord | None:
        item = self._get(f"TOKEN#{token_hash}", "META")
        return TokenRecord.from_item(item) if item else None

    def revoke_token(self, token_hash: str) -> None:
        try:
            self._table.update_item(
                Key={"pk": f"TOKEN#{token_hash}", "sk": "META"},
                UpdateExpression="SET revoked = :t",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":t": True},
            )
        except ClientError as exc:  # pragma: no cover - defensive
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def revoke_tokens_for_imsi(self, imsi: str) -> int:
        count = 0
        for item in self._query_gsi1(f"TOKENIMSI#{imsi}", limit=100):
            if not item.get("revoked"):
                self.revoke_token(str(item["token_hash"]))
                count += 1
        return count

    # ---- devices ----------------------------------------------------------
    def put_device(self, device: Device) -> None:
        item = device.to_item()
        item.update(
            {
                "pk": f"DEV#{device.device_id}",
                "sk": "META",
                "entity": _ENTITY_DEVICE,
                "gsi1pk": f"ENTITY#{_ENTITY_DEVICE}",
                "gsi1sk": device.device_id,
            }
        )
        self._put(item)

    def get_device(self, device_id: str) -> Device | None:
        item = self._get(f"DEV#{device_id}", "META")
        return Device.from_item(item) if item else None

    def list_devices(self, limit: int = 100) -> list[Device]:
        items = self._query_gsi1(f"ENTITY#{_ENTITY_DEVICE}", limit)
        return [Device.from_item(i) for i in items]

    # ---- DM sessions ------------------------------------------------------
    def put_dm_session(self, session: DmSession) -> None:
        item = session.to_item()
        item.update({"pk": f"DMSESS#{session.session_id}", "sk": "META", "entity": "dm_session"})
        self._put(item)

    def get_dm_session(self, session_id: str) -> DmSession | None:
        item = self._get(f"DMSESS#{session_id}", "META")
        if not item:
            return None
        session = DmSession.from_item(item)
        if session.expires_at and session.expires_at < int(time.time()):
            self.delete_dm_session(session_id)
            return None
        return session

    def delete_dm_session(self, session_id: str) -> None:
        self._delete(f"DMSESS#{session_id}", "META")

    # ---- SMS outbox -------------------------------------------------------
    def record_sms(self, message: SmsMessage) -> None:
        self._put(
            {
                "pk": f"SMS#{message.msisdn}",
                "sk": str(message.sent_at).zfill(12),
                "entity": "sms",
                "body": message.body,
                "sms_port": message.sms_port,
                "provider": message.provider,
                "binary": message.binary,
                "msisdn": message.msisdn,
                "sent_at": message.sent_at,
                "expires_at": message.sent_at + self._sms_retention,
            }
        )

    def list_sms(self, msisdn: str | None = None, limit: int = 50) -> list[SmsMessage]:
        from boto3.dynamodb.conditions import Key

        if msisdn is None:
            # Deliberately not a full table Scan: the outbox is a dev aid and is
            # only readable per MSISDN.
            return []
        response = self._table.query(
            KeyConditionExpression=Key("pk").eq(f"SMS#{msisdn}"),
            ScanIndexForward=False,
            Limit=limit,
        )
        out: list[SmsMessage] = []
        for raw in response.get("Items", []):
            item = _clean(raw)
            out.append(
                SmsMessage(
                    msisdn=str(item["msisdn"]),
                    body=str(item["body"]),
                    sms_port=item.get("sms_port"),
                    sent_at=int(item["sent_at"]),
                    provider=str(item.get("provider", "mock")),
                    binary=bool(item.get("binary", False)),
                )
            )
        return out

    # ---- health -----------------------------------------------------------
    def health(self) -> bool:
        try:
            self._table.table_status  # noqa: B018 - triggers DescribeTable
        except ClientError as exc:  # pragma: no cover - requires AWS failure
            log.warning("dynamodb health check failed", extra={"error": str(exc)})
            return False
        return True
