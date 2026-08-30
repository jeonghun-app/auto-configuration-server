"""Domain entities shared by the OMA-CP (RCS ACS) and OMA-DM planes."""

from __future__ import annotations

import dataclasses
import time
from typing import Any


def now() -> int:
    return int(time.time())


@dataclasses.dataclass(slots=True)
class Subscriber:
    """A provisionable subscriber record.

    ``provisioning_version`` is the OMA-CP ``VERS/version`` the ACS will hand
    out. ``forced_vers`` overrides it with one of the disable/dormant values
    defined in :mod:`acs.protocol.vers`.
    """

    imsi: str
    msisdn: str
    entitled: bool = True
    provisioning_version: int = 1
    forced_vers: int | None = None
    rcs_profile: str = ""
    imei_allowlist: list[str] = dataclasses.field(default_factory=list)
    overrides: dict[str, str] = dataclasses.field(default_factory=dict)
    """Per-subscriber OMA-CP parameter overrides keyed by dotted catalogue path."""
    dm_password: str = ""
    volte_enabled: bool = True
    created_at: int = dataclasses.field(default_factory=now)
    updated_at: int = dataclasses.field(default_factory=now)

    def imei_allowed(self, imei: str | None) -> bool:
        if not self.imei_allowlist:
            return True
        return imei is not None and imei in self.imei_allowlist

    def to_item(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Subscriber:
        fields = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in item.items() if k in fields}
        for key in ("provisioning_version", "created_at", "updated_at"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = int(kwargs[key])
        if kwargs.get("forced_vers") is not None:
            kwargs["forced_vers"] = int(kwargs["forced_vers"])
        return cls(**kwargs)


@dataclasses.dataclass(slots=True)
class OtpChallenge:
    """A pending SMS OTP challenge. Stored hashed, single use, TTL bounded."""

    msisdn: str
    otp_hash: str
    imsi: str
    created_at: int
    expires_at: int
    attempts: int = 0
    sms_port: int | None = None
    consumed: bool = False

    def expired(self, at: int | None = None) -> bool:
        return (at or now()) >= self.expires_at

    def to_item(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> OtpChallenge:
        fields = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in item.items() if k in fields}
        for key in ("created_at", "expires_at", "attempts"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = int(kwargs[key])
        if kwargs.get("sms_port") is not None:
            kwargs["sms_port"] = int(kwargs["sms_port"])
        return cls(**kwargs)


@dataclasses.dataclass(slots=True)
class TokenRecord:
    """A provisioning token. Only the SHA-256 digest is persisted."""

    token_hash: str
    imsi: str
    imei: str | None
    issued_at: int
    expires_at: int
    revoked: bool = False

    def expired(self, at: int | None = None) -> bool:
        return (at or now()) >= self.expires_at

    def to_item(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> TokenRecord:
        fields = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in item.items() if k in fields}
        for key in ("issued_at", "expires_at"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = int(kwargs[key])
        return cls(**kwargs)


@dataclasses.dataclass(slots=True)
class Device:
    """A managed device, populated from RCC.14 parameters and OMA-DM DevInfo."""

    device_id: str
    """OMA-DM ``./DevInfo/DevId`` (IMEI-derived) or the RCC.14 IMEI."""
    imsi: str = ""
    manufacturer: str = ""
    model: str = ""
    sw_version: str = ""
    client_vendor: str = ""
    client_version: str = ""
    dm_client_version: str = ""
    last_seen_at: int = dataclasses.field(default_factory=now)
    mo_values: dict[str, str] = dataclasses.field(default_factory=dict)
    """Flat map of OMA-DM node URI -> value for this device."""

    def to_item(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> Device:
        fields = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in item.items() if k in fields}
        if "last_seen_at" in kwargs and kwargs["last_seen_at"] is not None:
            kwargs["last_seen_at"] = int(kwargs["last_seen_at"])
        return cls(**kwargs)


@dataclasses.dataclass(slots=True)
class DmSession:
    """Server-side OMA-DM session state.

    A DM session spans several HTTP round trips, so the state must live in the
    shared store — otherwise it breaks the moment the service runs more than one
    ECS task.
    """

    session_id: str
    """Server-side key: the device id namespaced with the wire SessionID."""
    device_id: str
    wire_session_id: str = ""
    """The SessionID the device chose, echoed back in every response."""
    imsi: str = ""
    phase: str = "init"
    """init -> devinfo -> configure -> done"""
    last_msg_id: int = 0
    server_cmd_id: int = 0
    authenticated: bool = False
    nonce: str = ""
    pending: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    expires_at: int = 0

    def to_item(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> DmSession:
        fields = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in item.items() if k in fields}
        for key in ("last_msg_id", "server_cmd_id", "expires_at"):
            if key in kwargs and kwargs[key] is not None:
                kwargs[key] = int(kwargs[key])
        return cls(**kwargs)


@dataclasses.dataclass(slots=True)
class SmsMessage:
    """A recorded outbound SMS (mock provider / audit)."""

    msisdn: str
    body: str
    sms_port: int | None = None
    sent_at: int = dataclasses.field(default_factory=now)
    provider: str = "mock"
    binary: bool = False
