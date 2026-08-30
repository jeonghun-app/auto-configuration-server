"""The RCC.14 configuration decision flow.

This module contains the whole request lifecycle and returns a transport-neutral
:class:`ProvisioningOutcome`, so the flow can be tested without HTTP.
"""

from __future__ import annotations

import dataclasses
import secrets
import time
from collections.abc import Mapping

from acs.auth import gba as gba_mod
from acs.auth import otp as otp_mod
from acs.auth import token as token_mod
from acs.auth.enrichment import resolve_enriched_identity
from acs.auth.identity import (
    Identity,
    IdentityDecision,
    IdentityMethod,
    authenticated,
    challenge,
    not_entitled,
    unresolved,
)
from acs.config import Settings
from acs.domain.models import Device, Subscriber
from acs.observability import get_logger
from acs.protocol import vers as vers_mod
from acs.protocol.omacp import builder, writer
from acs.protocol.request import ConfigQuery
from acs.sms.base import SmsRequest, SmsSender, UnsupportedDelivery
from acs.store.base import Store

log = get_logger(__name__)


@dataclasses.dataclass(slots=True)
class ProvisioningOutcome:
    """A transport-neutral result of a configuration request."""

    status_code: int
    body: bytes = b""
    content_type: str = ""
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    metric: str = ""
    detail: str = ""
    version: int | None = None


def _no_store_headers() -> dict[str, str]:
    """Responses carry IMS credentials and tokens; never let them be cached."""
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }


class ProvisioningService:
    """Implements the RCC.14 HTTP configuration flow."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        sms_sender: SmsSender,
        bsf_client: gba_mod.BsfClient | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._sms = sms_sender
        self._bsf = bsf_client
        self._otp_policy = otp_mod.policy_from_settings(settings)

    # ------------------------------------------------------------------ API
    def handle(
        self,
        query: ConfigQuery,
        headers: Mapping[str, str] | None = None,
        peer: str | None = None,
        method: str = "GET",
    ) -> ProvisioningOutcome:
        """Process one configuration request."""
        headers = headers or {}
        identity = self.resolve_identity(query, headers, peer, method)

        if identity.decision is IdentityDecision.NOT_ENTITLED:
            return ProvisioningOutcome(
                status_code=403,
                headers=_no_store_headers(),
                metric="Rejected403",
                detail=identity.detail or "not_entitled",
            )

        if identity.decision is IdentityDecision.UNRESOLVED:
            return self._unresolved_outcome(identity)

        if identity.decision is IdentityDecision.CHALLENGE_OTP:
            return self._issue_otp(identity, query)

        assert identity.subscriber is not None  # noqa: S101 - narrowed by decision
        return self._serve_configuration(identity.subscriber, query, identity.method)

    # ------------------------------------------------------- identity chain
    def resolve_identity(
        self,
        query: ConfigQuery,
        headers: Mapping[str, str],
        peer: str | None,
        method: str = "GET",
    ) -> Identity:
        """Run the ordered identity resolution chain (see :mod:`acs.auth.identity`)."""
        settings = self._settings

        # 1. Provisioning token — strongest evidence, no user interaction.
        if query.token:
            record = token_mod.verify_token(
                self._store, query.token, query.imei, settings.token_bind_imei
            )
            if record is not None:
                subscriber = self._store.get_subscriber(record.imsi)
                if subscriber is None:
                    return unresolved("token_orphaned")
                if not subscriber.entitled:
                    return not_entitled(subscriber, "token_holder_not_entitled")
                return authenticated(subscriber, IdentityMethod.TOKEN)
            # An invalid or revoked token must send the client back to
            # bootstrapping rather than being silently ignored.
            return unresolved("token_invalid")

        # 2. Operator header enrichment from a trusted peer only.
        enriched = resolve_enriched_identity(
            headers=headers,
            peer=peer,
            forwarded_for=headers.get("x-forwarded-for"),
            header_name=settings.enrichment_header_name,
            trusted_cidrs=settings.trusted_proxy_list,
        )
        if enriched.msisdn:
            subscriber = self._store.get_subscriber_by_msisdn(enriched.msisdn)
            if subscriber is not None:
                if not subscriber.entitled:
                    return not_entitled(subscriber, "enriched_not_entitled")
                return authenticated(subscriber, IdentityMethod.ENRICHMENT)
            return unresolved("enriched_msisdn_unknown")

        # 3. GBA — interface present, disabled by default.
        if settings.gba_enabled and self._bsf is not None:
            gba_identity = self._resolve_gba(headers, method)
            if gba_identity is not None:
                return gba_identity

        # 4. Locate the candidate subscriber from the claimed identifiers. This
        #    is a claim, not a credential.
        subscriber = self._lookup_candidate(query)
        if subscriber is None:
            return unresolved("subscriber_unknown")
        if not subscriber.entitled:
            return not_entitled(subscriber, "not_entitled")
        if not subscriber.imei_allowed(query.imei):
            return not_entitled(subscriber, "imei_not_allowed")

        # 5. An OTP was supplied: verify it against the pending challenge.
        if query.otp:
            outcome = otp_mod.verify_challenge(
                self._store, subscriber.msisdn, query.otp, self._otp_policy
            )
            if outcome == otp_mod.VERIFIED:
                return authenticated(subscriber, IdentityMethod.OTP)
            return unresolved(f"otp_{outcome}")

        # 6. Known candidate, no proof yet: challenge by SMS.
        return challenge(subscriber, subscriber.msisdn, "bootstrap")

    def _lookup_candidate(self, query: ConfigQuery) -> Subscriber | None:
        if query.imsi:
            subscriber = self._store.get_subscriber(query.imsi)
            if subscriber is not None:
                return subscriber
        if query.msisdn:
            return self._store.get_subscriber_by_msisdn(query.msisdn)
        return None

    def _resolve_gba(self, headers: Mapping[str, str], method: str) -> Identity | None:
        """Resolve a subscriber from a *verified* GBA Digest Authorization header.

        A B-TID travels in the clear in the ``username`` directive, so possession
        of one proves nothing. The digest response is recomputed with ``Ks_NAF``
        and the nonce is checked to be one this server issued; only then is the
        subscriber considered authenticated.
        """
        authorization = headers.get("authorization", "")
        if not authorization or self._bsf is None:
            return None

        check = gba_mod.verify_authorization(
            header=authorization,
            realm=self._settings.gba_realm,
            nonce_secret=self._settings.gba_nonce_secret,
            method=method,
            fetch_keys=self._bsf.fetch_keys,
        )
        if not check.ok:
            log.info("gba authorization rejected", extra={"reason": check.reason})
            # Fall through to a fresh challenge rather than to a weaker method.
            return unresolved(f"gba_{check.reason}")

        keys = self._bsf.fetch_keys(check.btid, self._settings.gba_realm)
        if keys is None:  # pragma: no cover - verified above
            return unresolved("gba_btid_unknown")
        imsi = keys.impi.split("@", 1)[0]
        subscriber = self._store.get_subscriber(imsi)
        if subscriber is None:
            return unresolved("gba_subscriber_unknown")
        if not subscriber.entitled:
            return not_entitled(subscriber, "gba_not_entitled")
        return authenticated(subscriber, IdentityMethod.GBA)

    # ------------------------------------------------------------- outcomes
    def _unresolved_outcome(self, identity: Identity) -> ProvisioningOutcome:
        """HTTP 511, or a GBA challenge when GBA is enabled."""
        if self._settings.gba_enabled and self._bsf is not None:
            nonce = gba_mod.make_nonce(self._settings.gba_nonce_secret)
            return ProvisioningOutcome(
                status_code=401,
                headers={
                    **_no_store_headers(),
                    "WWW-Authenticate": gba_mod.challenge_header(self._settings.gba_realm, nonce),
                },
                metric="GbaChallenge",
                detail=identity.detail,
            )
        return ProvisioningOutcome(
            status_code=511,
            headers=_no_store_headers(),
            metric="Challenge511",
            detail=identity.detail or "unresolved",
        )

    def _issue_otp(self, identity: Identity, query: ConfigQuery) -> ProvisioningOutcome:
        """Send an OTP and answer 200 with an empty body (RCC.14 pending signal)."""
        assert identity.subscriber is not None  # noqa: S101
        msisdn = identity.candidate_msisdn or identity.subscriber.msisdn
        try:
            _, clear_otp = otp_mod.create_challenge(
                store=self._store,
                msisdn=msisdn,
                imsi=identity.subscriber.imsi,
                policy=self._otp_policy,
                sms_port=query.sms_port or None,
            )
        except otp_mod.SendBlocked as blocked:
            if blocked.reason == "cooldown":
                # A challenge is already outstanding. Repeating the identical
                # bootstrap request must not cost another SMS, and the client
                # should keep waiting: answer with the same pending signal.
                return ProvisioningOutcome(
                    status_code=200,
                    content_type=self._settings.xml_content_type,
                    headers={**_no_store_headers(), "Content-Length": "0"},
                    metric="OtpPendingReuse",
                    detail="cooldown",
                )
            return ProvisioningOutcome(
                status_code=429,
                headers={**_no_store_headers(), "Retry-After": str(blocked.retry_after)},
                metric="OtpRateLimited",
                detail=blocked.reason,
            )

        body = self._settings.sms_otp_template.format(otp=clear_otp)
        try:
            self._sms.send(
                SmsRequest(
                    msisdn=msisdn,
                    body=body,
                    sms_port=query.sms_port or None,
                    sender_id=self._settings.sms_sender_id,
                )
            )
        except UnsupportedDelivery as exc:
            # Do not leave the client waiting for a message that cannot be sent.
            self._store.delete_otp(msisdn)
            log.error("otp delivery unsupported", extra={"error": str(exc), "msisdn": msisdn})
            return ProvisioningOutcome(
                status_code=503,
                headers={**_no_store_headers(), "Retry-After": "3600"},
                metric="OtpDeliveryUnsupported",
                detail="port_addressed_sms_unsupported",
            )

        log.info(
            "otp challenge issued",
            extra={"msisdn": msisdn, "imsi": identity.subscriber.imsi, "sms_port": query.sms_port},
        )
        return ProvisioningOutcome(
            status_code=200,
            content_type=self._settings.xml_content_type,
            headers={**_no_store_headers(), "Content-Length": "0"},
            metric="OtpSent",
            detail="otp_sent",
        )

    def _serve_configuration(
        self,
        subscriber: Subscriber,
        query: ConfigQuery,
        method: IdentityMethod,
    ) -> ProvisioningOutcome:
        settings = self._settings
        validity = settings.provisioning_validity_seconds

        # Operator forced a disable / dormant / blocked state.
        if subscriber.forced_vers is not None:
            rule = vers_mod.rule_for(subscriber.forced_vers)
            doc = builder.build_vers_only_document(subscriber.forced_vers, validity)
            log.info(
                "serving forced vers",
                extra={
                    "imsi": subscriber.imsi,
                    "vers": subscriber.forced_vers,
                    "action": rule.action.value,
                },
            )
            return ProvisioningOutcome(
                status_code=200,
                body=writer.to_xml(doc),
                content_type=settings.xml_content_type,
                headers=_no_store_headers(),
                metric="ConfigDisabled",
                detail=rule.action.value,
                version=subscriber.forced_vers,
            )

        version = subscriber.provisioning_version
        if version <= 0:
            version = vers_mod.next_version(version)
            subscriber.provisioning_version = version
            self._store.put_subscriber(subscriber)

        # The client already holds this revision: RCC.14 permits a VERS-only
        # answer instead of re-sending the whole document.
        if vers_mod.client_holds_current(query.vers, version):
            doc = builder.build_vers_only_document(version, validity)
            return ProvisioningOutcome(
                status_code=200,
                body=writer.to_xml(doc),
                content_type=settings.xml_content_type,
                headers=_no_store_headers(),
                metric="ConfigUnchanged",
                detail="vers_only",
                version=version,
            )

        token = token_mod.issue_token(
            store=self._store,
            imsi=subscriber.imsi,
            imei=query.imei,
            ttl_seconds=settings.token_ttl_seconds,
            bind_imei=settings.token_bind_imei,
        )

        dm_password = subscriber.dm_password
        if settings.dm_enabled and settings.dm_bootstrap_in_cp and not dm_password:
            dm_password = secrets.token_urlsafe(18)
            subscriber.dm_password = dm_password
            self._store.put_subscriber(subscriber)

        doc = builder.build_document(
            settings=settings,
            query=query,
            imsi=subscriber.imsi,
            msisdn=subscriber.msisdn,
            version=version,
            validity=validity,
            profile=subscriber.rcs_profile or query.rcs_profile or settings.default_rcs_profile,
            token=token,
            overrides=subscriber.overrides,
            dm_password=dm_password,
        )
        self._record_device(subscriber, query)

        log.info(
            "configuration served",
            extra={
                "imsi": subscriber.imsi,
                "version": version,
                "method": method.value,
                "parms": doc.parm_count(),
            },
        )
        return ProvisioningOutcome(
            status_code=200,
            body=writer.to_xml(doc),
            content_type=settings.xml_content_type,
            headers=_no_store_headers(),
            metric="ConfigServed",
            detail=method.value,
            version=version,
        )

    def _record_device(self, subscriber: Subscriber, query: ConfigQuery) -> None:
        """Register the device so the OMA-DM plane can manage it later."""
        device_id = query.device_key
        existing = self._store.get_device(device_id)
        device = existing or Device(device_id=device_id)
        device.imsi = subscriber.imsi
        device.manufacturer = query.terminal_vendor or device.manufacturer
        device.model = query.terminal_model or device.model
        device.sw_version = query.terminal_sw_version or device.sw_version
        device.client_vendor = query.client_vendor or device.client_vendor
        device.client_version = query.client_version or device.client_version
        device.last_seen_at = int(time.time())
        self._store.put_device(device)
