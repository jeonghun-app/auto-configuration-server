"""RCC.14 HTTP configuration request parsing.

The wire contract is a set of case-sensitive query parameters. Spelling matters:
``IMSI``, ``IMEI``, ``SMS_port`` and ``OTP`` are upper-case in the specification
while ``msisdn``, ``token`` and the ``terminal_*``/``client_*`` family are
lower-case. Some deployed clients disagree, so lookup is done through an
explicit alias table rather than by lower-casing everything.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from typing import Any, Final

from acs.errors import MalformedRequest
from acs.security.pii import normalise_msisdn

_DIGITS: Final = re.compile(r"^\d+$")

#: Wire name -> internal field name. The first entry is the normative spelling.
PARAMETER_ALIASES: Final[dict[str, str]] = {
    "vers": "vers",
    "IMSI": "imsi",
    "imsi": "imsi",
    "IMEI": "imei",
    "imei": "imei",
    "IMEISV": "imeisv",
    "msisdn": "msisdn",
    "MSISDN": "msisdn",
    "token": "token",
    "OTP": "otp",
    "otp": "otp",
    "SMS_port": "sms_port",
    "sms_port": "sms_port",
    "SMS_format": "sms_format",
    "default_sms_app": "default_sms_app",
    "default_vvm_app": "default_vvm_app",
    "terminal_vendor": "terminal_vendor",
    "terminal_model": "terminal_model",
    "terminal_sw_version": "terminal_sw_version",
    "client_vendor": "client_vendor",
    "client_version": "client_version",
    "rcs_version": "rcs_version",
    "rcs_profile": "rcs_profile",
    "rcs_state": "rcs_state",
    "provisioning_version": "provisioning_version",
    "device_type": "device_type",
    "device_id": "device_id",
    "friendly_device_name": "friendly_device_name",
    "alias": "alias",
}

#: Maximum accepted length per field (RCC.14 constrains several of these; the
#: values here are the documented spec limits where known, and defensive caps
#: elsewhere). Over-long values are rejected rather than silently truncated.
FIELD_MAX_LENGTH: Final[dict[str, int]] = {
    "imsi": 15,
    "imei": 16,
    "imeisv": 16,
    "msisdn": 20,
    "token": 256,
    "otp": 16,
    "terminal_vendor": 8,
    "terminal_model": 32,
    "terminal_sw_version": 32,
    "client_vendor": 8,
    "client_version": 32,
    "rcs_version": 16,
    "rcs_profile": 32,
    "device_type": 16,
    "device_id": 64,
    "friendly_device_name": 64,
    "alias": 64,
}

#: Parameters that must never be repeated: a duplicate would let an attacker
#: smuggle a second identity or OTP past a proxy that only inspects the first.
SINGLE_VALUE_ONLY: Final[frozenset[str]] = frozenset(
    {"vers", "IMSI", "imsi", "IMEI", "imei", "msisdn", "token", "OTP", "otp"}
)


@dataclasses.dataclass(slots=True, frozen=True)
class ConfigQuery:
    """A parsed, validated RCC.14 configuration request."""

    vers: int = 0
    """Configuration version currently held by the client. 0 = unprovisioned."""
    imsi: str | None = None
    imei: str | None = None
    imeisv: str | None = None
    msisdn: str | None = None
    token: str | None = None
    otp: str | None = None
    sms_port: int | None = None
    sms_format: str | None = None
    default_sms_app: int | None = None
    default_vvm_app: int | None = None
    terminal_vendor: str | None = None
    terminal_model: str | None = None
    terminal_sw_version: str | None = None
    client_vendor: str | None = None
    client_version: str | None = None
    rcs_version: str | None = None
    rcs_profile: str | None = None
    rcs_state: int | None = None
    provisioning_version: str | None = None
    device_type: str | None = None
    device_id: str | None = None
    friendly_device_name: str | None = None
    alias: str | None = None
    apps: tuple[str, ...] = ()
    """Repeatable ``app=`` parameter: the AppIDs the client is asking for."""
    user_agent: str = ""
    unknown: tuple[str, ...] = ()
    """Names of parameters the ACS does not recognise (logged, never echoed)."""

    @property
    def device_key(self) -> str:
        """Stable identifier for the device across CP and DM planes."""
        return self.imei or self.device_id or (self.imsi or "unknown")

    def redactable(self) -> dict[str, Any]:
        """Fields safe to hand to the logger (it redacts the sensitive ones)."""
        return {
            "vers": self.vers,
            "imsi": self.imsi,
            "imei": self.imei,
            "msisdn": self.msisdn,
            "token": self.token,
            "terminal_vendor": self.terminal_vendor,
            "terminal_model": self.terminal_model,
            "client_vendor": self.client_vendor,
            "client_version": self.client_version,
            "rcs_profile": self.rcs_profile,
            "rcs_state": self.rcs_state,
            "apps": list(self.apps),
        }


def _one(values: list[str], wire_name: str) -> str:
    if len(values) > 1 and wire_name in SINGLE_VALUE_ONLY:
        raise MalformedRequest(f"parameter {wire_name} must not be repeated")
    return values[0]


def _as_int(raw: str, field: str, low: int | None = None, high: int | None = None) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise MalformedRequest(f"{field} must be an integer") from exc
    if low is not None and value < low:
        raise MalformedRequest(f"{field} must be >= {low}")
    if high is not None and value > high:
        raise MalformedRequest(f"{field} must be <= {high}")
    return value


def parse_config_query(
    params: Mapping[str, list[str]],
    user_agent: str = "",
    max_value_length: int = 256,
    default_country_code: str = "",
    national_trunk_prefix: str = "0",
) -> ConfigQuery:
    """Parse and validate the RCC.14 query string.

    ``params`` maps wire parameter name to the list of supplied values, which is
    what ``starlette.datastructures.QueryParams.multi_items`` provides. Repeated
    ``app`` values are preserved; repeats of identity parameters are rejected.
    """
    raw: dict[str, str] = {}
    apps: list[str] = []
    unknown: list[str] = []

    for wire_name, values in params.items():
        if not values:
            continue
        if wire_name == "app":
            apps.extend(v for v in values if v)
            continue
        field = PARAMETER_ALIASES.get(wire_name)
        if field is None:
            unknown.append(wire_name)
            continue
        value = _one(values, wire_name)
        if len(value) > max_value_length:
            raise MalformedRequest(f"parameter {wire_name} exceeds {max_value_length} characters")
        limit = FIELD_MAX_LENGTH.get(field)
        if limit is not None and len(value) > limit:
            raise MalformedRequest(f"parameter {wire_name} exceeds {limit} characters")
        raw.setdefault(field, value)

    kwargs: dict[str, Any] = {}

    # vers may legitimately be negative: clients echo back the disable value
    # they were previously given, so parsing must accept it.
    if "vers" in raw:
        kwargs["vers"] = _as_int(raw["vers"], "vers", low=-99, high=10_000_000)

    imsi = raw.get("imsi")
    if imsi is not None:
        # Keep IMSI as a string. Converting to int drops the leading zeros that
        # distinguish one MCC from another.
        if not _DIGITS.match(imsi) or not 5 <= len(imsi) <= 15:
            raise MalformedRequest("IMSI must be 5-15 digits")
        kwargs["imsi"] = imsi

    for field in ("imei", "imeisv"):
        device_value = raw.get(field)
        if device_value is not None:
            # IMEISV is 16 digits and field-test devices carry non-Luhn IMEIs,
            # so validate the shape only.
            if not _DIGITS.match(device_value) or not 14 <= len(device_value) <= 16:
                raise MalformedRequest(f"{field.upper()} must be 14-16 digits")
            kwargs[field] = device_value

    if "msisdn" in raw:
        normalised_msisdn = normalise_msisdn(
            raw["msisdn"], default_country_code, national_trunk_prefix
        )
        if normalised_msisdn is None:
            raise MalformedRequest("msisdn is not a valid E.164 number")
        kwargs["msisdn"] = normalised_msisdn

    if "otp" in raw:
        otp = raw["otp"].strip()
        if not otp.isalnum():
            raise MalformedRequest("OTP must be alphanumeric")
        kwargs["otp"] = otp

    if "sms_port" in raw:
        kwargs["sms_port"] = _as_int(raw["sms_port"], "SMS_port", low=0, high=65535)

    for field in ("default_sms_app", "default_vvm_app", "rcs_state"):
        if field in raw:
            kwargs[field] = _as_int(raw[field], field, low=-4, high=99)

    for field in (
        "token",
        "sms_format",
        "terminal_vendor",
        "terminal_model",
        "terminal_sw_version",
        "client_vendor",
        "client_version",
        "rcs_version",
        "rcs_profile",
        "provisioning_version",
        "device_type",
        "device_id",
        "friendly_device_name",
        "alias",
    ):
        if field in raw:
            kwargs[field] = raw[field]

    return ConfigQuery(
        **kwargs,
        apps=tuple(dict.fromkeys(apps)),
        user_agent=user_agent[:256],
        unknown=tuple(sorted(set(unknown))),
    )
