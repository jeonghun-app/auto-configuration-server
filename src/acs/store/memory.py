"""In-memory store for development and unit tests.

Refused in staging/prod by :meth:`acs.config.Settings.validate_startup` because
OTP challenges and DM sessions would not be visible to sibling ECS tasks.
"""

from __future__ import annotations

import threading
import time

from acs.domain.models import Device, DmSession, OtpChallenge, SmsMessage, Subscriber, TokenRecord


class MemoryStore:
    """Thread-safe dictionary-backed :class:`acs.store.base.Store`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[str, Subscriber] = {}
        self._msisdn_index: dict[str, str] = {}
        self._otp: dict[str, OtpChallenge] = {}
        self._otp_sends: dict[str, list[int]] = {}
        self._tokens: dict[str, TokenRecord] = {}
        self._devices: dict[str, Device] = {}
        self._dm_sessions: dict[str, DmSession] = {}
        self._sms: list[SmsMessage] = []

    # ---- subscribers ------------------------------------------------------
    def get_subscriber(self, imsi: str) -> Subscriber | None:
        with self._lock:
            return self._subscribers.get(imsi)

    def get_subscriber_by_msisdn(self, msisdn: str) -> Subscriber | None:
        with self._lock:
            imsi = self._msisdn_index.get(msisdn)
            return self._subscribers.get(imsi) if imsi else None

    def put_subscriber(self, subscriber: Subscriber) -> None:
        with self._lock:
            previous = self._subscribers.get(subscriber.imsi)
            if previous and previous.msisdn != subscriber.msisdn:
                self._msisdn_index.pop(previous.msisdn, None)
            self._subscribers[subscriber.imsi] = subscriber
            self._msisdn_index[subscriber.msisdn] = subscriber.imsi

    def delete_subscriber(self, imsi: str) -> None:
        with self._lock:
            subscriber = self._subscribers.pop(imsi, None)
            if subscriber:
                self._msisdn_index.pop(subscriber.msisdn, None)

    def list_subscribers(self, limit: int = 100) -> list[Subscriber]:
        with self._lock:
            return list(self._subscribers.values())[:limit]

    # ---- OTP --------------------------------------------------------------
    def put_otp(self, challenge: OtpChallenge) -> None:
        with self._lock:
            self._otp[challenge.msisdn] = challenge

    def get_otp(self, msisdn: str) -> OtpChallenge | None:
        with self._lock:
            return self._otp.get(msisdn)

    def delete_otp(self, msisdn: str) -> None:
        with self._lock:
            self._otp.pop(msisdn, None)

    def count_otp_sends_today(self, msisdn: str) -> int:
        cutoff = int(time.time()) - 86400
        with self._lock:
            sends = [t for t in self._otp_sends.get(msisdn, []) if t >= cutoff]
            self._otp_sends[msisdn] = sends
            return len(sends)

    def record_otp_send(self, msisdn: str) -> None:
        with self._lock:
            self._otp_sends.setdefault(msisdn, []).append(int(time.time()))

    # ---- tokens -----------------------------------------------------------
    def put_token(self, record: TokenRecord) -> None:
        with self._lock:
            self._tokens[record.token_hash] = record

    def get_token(self, token_hash: str) -> TokenRecord | None:
        with self._lock:
            return self._tokens.get(token_hash)

    def revoke_token(self, token_hash: str) -> None:
        with self._lock:
            record = self._tokens.get(token_hash)
            if record:
                record.revoked = True

    def revoke_tokens_for_imsi(self, imsi: str) -> int:
        count = 0
        with self._lock:
            for record in self._tokens.values():
                if record.imsi == imsi and not record.revoked:
                    record.revoked = True
                    count += 1
        return count

    # ---- devices ----------------------------------------------------------
    def put_device(self, device: Device) -> None:
        with self._lock:
            self._devices[device.device_id] = device

    def get_device(self, device_id: str) -> Device | None:
        with self._lock:
            return self._devices.get(device_id)

    def list_devices(self, limit: int = 100) -> list[Device]:
        with self._lock:
            return list(self._devices.values())[:limit]

    # ---- DM sessions ------------------------------------------------------
    def put_dm_session(self, session: DmSession) -> None:
        with self._lock:
            self._dm_sessions[session.session_id] = session

    def get_dm_session(self, session_id: str) -> DmSession | None:
        with self._lock:
            session = self._dm_sessions.get(session_id)
            if session and session.expires_at and session.expires_at < int(time.time()):
                self._dm_sessions.pop(session_id, None)
                return None
            return session

    def delete_dm_session(self, session_id: str) -> None:
        with self._lock:
            self._dm_sessions.pop(session_id, None)

    # ---- SMS outbox -------------------------------------------------------
    def record_sms(self, message: SmsMessage) -> None:
        with self._lock:
            self._sms.append(message)
            del self._sms[:-500]

    def list_sms(self, msisdn: str | None = None, limit: int = 50) -> list[SmsMessage]:
        with self._lock:
            items = [m for m in self._sms if msisdn is None or m.msisdn == msisdn]
            return list(reversed(items))[:limit]

    # ---- health -----------------------------------------------------------
    def health(self) -> bool:
        return True
