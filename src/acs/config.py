"""Application configuration.

Every setting is supplied through an ``ACS_``-prefixed environment variable so
the container is configured entirely by the ECS task definition / Secrets
Manager, with no configuration files baked into the image.
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "staging", "prod"]
StoreBackend = Literal["memory", "dynamodb"]
SmsProvider = Literal["mock", "sns", "eum"]
PiiLogMode = Literal["mask", "hash", "none"]


class Settings(BaseSettings):
    """Runtime settings for the ACS.

    Defaults are chosen to *fail closed*: the admin API is unusable, header
    enrichment is off, GBA is off and developer endpoints are off unless
    explicitly enabled.
    """

    model_config = SettingsConfigDict(
        env_prefix="ACS_",
        env_file=".env",
        extra="ignore",
        frozen=True,
    )

    # ---- Service identity -------------------------------------------------
    env: Environment = "dev"
    service_name: str = "rcs-acs"
    log_level: str = "INFO"
    metrics_namespace: str = "RcsAcs"

    # ---- AWS backing services --------------------------------------------
    aws_region: str = "ap-northeast-2"
    store_backend: StoreBackend = "memory"
    table_name: str = "rcs-acs"
    dynamodb_endpoint_url: str = ""
    """Set to e.g. ``http://dynamodb-local:8000`` for local development."""

    # ---- HTTP surface -----------------------------------------------------
    config_paths: str = "/,/config,/rcs/config"
    """Comma-separated paths that serve the RCC.14 configuration request."""
    dm_path: str = "/dm"
    xml_content_type: str = "text/xml"
    max_query_value_length: int = 256

    # ---- Provisioning policy ---------------------------------------------
    default_rcs_profile: str = "UP_2.4"
    provisioning_validity_seconds: int = 86400
    """VALIDITY emitted in the OMA-CP ``VERS`` characteristic. 0 = no expiry."""
    malformed_request_status: int = 400
    """Some operator ACSs answer 403 instead. Configurable, see ADR-0006."""

    # ---- Authentication ---------------------------------------------------
    otp_length: int = 6
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 3
    otp_resend_cooldown_seconds: int = 60
    otp_max_sends_per_msisdn_per_day: int = 10
    token_ttl_seconds: int = 2592000  # 30 days
    token_bind_imei: bool = True

    trusted_proxy_cidrs: str = ""
    """Empty (default) disables header-enrichment identity entirely."""
    enrichment_header_name: str = "X-3GPP-Intended-Identity"
    gba_enabled: bool = False
    gba_realm: str = "3GPP-bootstrapping@acs.example.com"

    # ---- OMA-DM -----------------------------------------------------------
    dm_enabled: bool = True
    dm_server_id: str = "ACS-DM"
    dm_auth_scheme: Literal["none", "basic", "md5"] = "basic"
    dm_bootstrap_in_cp: bool = True
    """Emit the OMA-CP ``w7`` APPLICATION characteristic that bootstraps the
    DM account on the device (this is the CP -> DM bridge)."""
    dm_account_uri: str = "https://acs.example.com/dm"
    dm_max_msg_size: int = 16384
    dm_session_ttl_seconds: int = 600

    # ---- SMS --------------------------------------------------------------
    sms_provider: SmsProvider = "mock"
    sms_origination_identity: str = ""
    """AWS End User Messaging origination identity (phone number ARN / pool)."""
    sms_sender_id: str = "RCS"
    sms_otp_template: str = "RCS activation code: {otp}"

    # ---- Operational ------------------------------------------------------
    admin_token: str = ""
    """Empty (default) makes the admin API answer 503 — never a default token."""
    dev_endpoints_enabled: bool = False
    """Exposes /dev/sms (mock SMS outbox). Refused when env == prod."""
    pii_log_mode: PiiLogMode = "mask"
    pii_hash_secret: str = ""
    rate_limit_per_ip_per_minute: int = 60

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def config_path_list(self) -> list[str]:
        return [p.strip() for p in self.config_paths.split(",") if p.strip()]

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [c.strip() for c in self.trusted_proxy_cidrs.split(",") if c.strip()]

    @property
    def is_prod(self) -> bool:
        return self.env in ("staging", "prod")

    def validate_startup(self) -> list[str]:
        """Return a list of fatal misconfigurations for the current env."""
        problems: list[str] = []
        if self.is_prod:
            if self.store_backend == "memory":
                problems.append(
                    "store_backend=memory is unsafe outside dev: OTP challenges would "
                    "not be shared across tasks. Use store_backend=dynamodb."
                )
            if self.dev_endpoints_enabled:
                problems.append("dev_endpoints_enabled must be false in staging/prod.")
            if self.sms_provider == "mock":
                problems.append("sms_provider=mock cannot be used in staging/prod.")
            if self.pii_log_mode == "none":
                problems.append("pii_log_mode=none is not permitted in staging/prod.")
            if self.pii_log_mode == "hash" and not self.pii_hash_secret:
                problems.append("pii_log_mode=hash requires pii_hash_secret.")
        if self.gba_enabled and not self.gba_realm:
            problems.append("gba_enabled requires gba_realm.")
        if self.otp_length < 4 or self.otp_length > 10:
            problems.append("otp_length must be between 4 and 10.")
        return problems


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
