"""Amazon DynamoDB single-table store.

Table design (partition key ``pk``, sort key ``sk``)::

    SUB#<imsi>       META          subscriber record
    MSISDN#<msisdn>  SUB           reverse index -> imsi
    OTP#<msisdn>     CHAL          pending OTP challenge          (TTL)
    OTPQUOTA#<msisdn> SENDS        send times for the daily cap   (TTL)
    OTPSEND#<msisdn> <epoch>       legacy send rows: merged with OTPQUOTA#, dual-written for 1.3
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
from acs.store.base import OtpIssueRefused, OtpStoreContention

log = get_logger(__name__)

_ENTITY_SUBSCRIBER = "subscriber"
_ENTITY_DEVICE = "device"

# Optimistic writes retry only when another request changed the same MSISDN's
# challenge in between. A loser re-reads and then normally meets the cooldown, so
# a handful of rounds is plenty; past that the send is refused, never let through.
_OTP_WRITE_ROUNDS = 5


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


def _same_challenge(challenge: OtpChallenge) -> dict[str, Any]:
    """Condition that the stored challenge is ``challenge``, unchanged since read.

    By id when it has one. A challenge stored by 1.3 has none, so it falls back to
    code, creation time and attempts, the condition 1.4.0 used.
    """
    if challenge.challenge_id:
        return {
            "ConditionExpression": "challenge_id = :id AND attempts = :a",
            "ExpressionAttributeValues": {
                ":id": challenge.challenge_id,
                ":a": challenge.attempts,
            },
        }
    return {
        "ConditionExpression": "otp_hash = :h AND created_at = :c AND attempts = :a",
        "ExpressionAttributeValues": {
            ":h": challenge.otp_hash,
            ":c": challenge.created_at,
            ":a": challenge.attempts,
        },
    }


def _lost_a_condition(exc: ClientError) -> bool:
    """Whether a cancelled transaction failed only on its conditions."""
    if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
        return False
    codes = {
        str(reason.get("Code", "None")) for reason in exc.response.get("CancellationReasons", [])
    }
    codes.discard("None")
    return codes == {"ConditionalCheckFailed"}


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
    def get_otp(self, msisdn: str) -> OtpChallenge | None:
        item = self._get(f"OTP#{msisdn}", "CHAL")
        return OtpChallenge.from_item(item) if item else None

    def issue_otp(
        self,
        challenge: OtpChallenge,
        cooldown_seconds: int,
        max_sends_per_day: int,
        now: int,
    ) -> OtpIssueRefused | None:
        # Read both items, decide, then write both in one transaction conditioned
        # on neither having changed since the read. The quota item carries a
        # version because a list of send times cannot be compared in a condition.
        msisdn = challenge.msisdn
        quota_key = {"pk": f"OTPQUOTA#{msisdn}", "sk": "SENDS"}
        challenge_key = {"pk": f"OTP#{msisdn}", "sk": "CHAL"}
        client = self._table.meta.client
        for _ in range(_OTP_WRITE_ROUNDS):
            quota = self._get_consistent(quota_key)
            raw_existing = self._get_consistent(challenge_key)
            existing = OtpChallenge.from_item(raw_existing) if raw_existing else None
            if existing and not existing.consumed and not existing.expired(now):
                age = now - existing.created_at
                if age < cooldown_seconds:
                    return OtpIssueRefused("cooldown", cooldown_seconds - age)
            history = quota["sends"] if quota else []
            sends = [int(t) for t in history if int(t) >= now - 86400]
            # Until #28, 1.3 can write more legacy rows after the quota was seeded.
            # Exclude dual-written seconds, but preserve distinct sends already
            # counted in the quota within one second when cooldown is disabled.
            sends.extend(sorted(set(self._legacy_sends(msisdn, now)) - set(sends)))
            if len(sends) >= max_sends_per_day:
                return OtpIssueRefused("daily_quota", 3600)

            version = int((quota or {}).get("version", 0))
            quota_put: dict[str, Any] = {
                "TableName": self._table_name,
                "Item": {
                    **quota_key,
                    "entity": "otp_quota",
                    "sends": [*sends, now],
                    "version": version + 1,
                    "expires_at": now + 86400,
                },
            }
            if quota is None:
                quota_put["ConditionExpression"] = "attribute_not_exists(pk)"
            else:
                quota_put["ConditionExpression"] = "version = :v"
                quota_put["ExpressionAttributeValues"] = {":v": version}

            item = challenge.to_item()
            item.update({**challenge_key, "entity": "otp"})
            challenge_put: dict[str, Any] = {"TableName": self._table_name, "Item": _encode(item)}
            if existing is None:
                challenge_put["ConditionExpression"] = "attribute_not_exists(pk)"
            else:
                challenge_put.update(_same_challenge(existing))
            # Mixed-version compatibility, to be removed by #28 ("Stop dual-writing
            # legacy OTPSEND rows") once no 1.3 task can be running: 1.3 counts
            # the daily cap by querying these rows, so during a rolling deployment
            # it must see the sends made here. Same key, attributes and TTL as
            # 1.3's record_otp_send. Unconditional, as 1.3 wrote it.
            legacy_put: dict[str, Any] = {
                "TableName": self._table_name,
                "Item": {
                    "pk": f"OTPSEND#{msisdn}",
                    "sk": str(now).zfill(12),
                    "entity": "otp_send",
                    "expires_at": now + 86400,
                },
            }
            try:
                client.transact_write_items(
                    TransactItems=[{"Put": quota_put}, {"Put": challenge_put}, {"Put": legacy_put}]
                )
            except ClientError as exc:
                if not _lost_a_condition(exc):
                    # Throttling, validation or capacity: a fault, not contention,
                    # and retrying it here would only hide it.
                    raise
                continue
            return None
        log.warning("otp issue lost every optimistic write round")
        raise OtpStoreContention("concurrent OTP issue for one MSISDN kept conflicting")

    def _legacy_sends(self, msisdn: str, now: int) -> list[int]:
        """Send times from the legacy OTPSEND# rows in the last 24 hours.

        Read on every quota check until #28 removes mixed-version support: 1.3
        tasks can still send after the quota item exists.
        """
        from boto3.dynamodb.conditions import Key

        response = self._table.query(
            KeyConditionExpression=Key("pk").eq(f"OTPSEND#{msisdn}")
            & Key("sk").gte(str(now - 86400).zfill(12)),
            ConsistentRead=True,
        )
        return [int(item["sk"]) for item in response.get("Items", [])]

    def replace_otp(self, expected: OtpChallenge, replacement: OtpChallenge | None) -> bool:
        key = {"pk": f"OTP#{expected.msisdn}", "sk": "CHAL"}
        condition = _same_challenge(expected)
        try:
            if replacement is None:
                self._table.delete_item(Key=key, **condition)
            else:
                item = replacement.to_item()
                item.update({**key, "entity": "otp"})
                self._table.put_item(Item=_encode(item), **condition)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            return False
        return True

    def _get_consistent(self, key: dict[str, str]) -> dict[str, Any] | None:
        item = self._table.get_item(Key=key, ConsistentRead=True).get("Item")
        return _clean(item) if item else None

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
