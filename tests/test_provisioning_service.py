"""The RCC.14 decision flow, exercised without HTTP."""

from __future__ import annotations

import pytest
from tests.conftest import TEST_IMEI, TEST_IMSI, TEST_MSISDN

from acs.auth import otp as otp_mod
from acs.auth import token as token_mod
from acs.auth.identity import IdentityDecision, IdentityMethod
from acs.config import Settings
from acs.domain.models import Subscriber
from acs.domain.service import ProvisioningService
from acs.protocol.omacp import writer
from acs.protocol.request import ConfigQuery
from acs.sms.base import MockSmsSender, SmsRequest, SmsResult, UnsupportedDelivery
from acs.store.memory import MemoryStore


def query(**extra: object) -> ConfigQuery:
    params: dict[str, object] = {"imsi": TEST_IMSI, "imei": TEST_IMEI, "vers": 0}
    params.update(extra)
    return ConfigQuery(**params)  # type: ignore[arg-type]


def read_otp(store: MemoryStore) -> str:
    messages = store.list_sms(TEST_MSISDN)
    return "".join(ch for ch in messages[0].body if ch.isdigit())


# ------------------------------------------------------------------ 200 empty
@pytest.mark.spec
def test_first_request_sends_an_otp_and_answers_200_empty(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    outcome = service.handle(query())
    assert outcome.status_code == 200
    assert outcome.body == b""
    assert outcome.headers["Content-Length"] == "0"
    assert seeded_store.get_otp(TEST_MSISDN) is not None
    assert len(seeded_store.list_sms(TEST_MSISDN)) == 1


def test_repeating_the_bootstrap_request_does_not_resend_the_sms(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    service.handle(query())
    outcome = service.handle(query())
    assert outcome.status_code == 200
    assert outcome.metric == "OtpPendingReuse"
    assert len(seeded_store.list_sms(TEST_MSISDN)) == 1


# ----------------------------------------------------------------- 200 + XML
@pytest.mark.spec
def test_correct_otp_returns_the_configuration(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    service.handle(query())
    outcome = service.handle(query(otp=read_otp(seeded_store)))
    assert outcome.status_code == 200
    assert outcome.version == 1
    assert writer.validate_structure(outcome.body) == []
    assert b'AppID" value="ap2002' in outcome.body


def test_wrong_otp_returns_511(service: ProvisioningService) -> None:
    service.handle(query())
    outcome = service.handle(query(otp="000000"))
    assert outcome.status_code == 511
    assert outcome.detail.startswith("otp_")


# ---------------------------------------------------------------------- 511
@pytest.mark.spec
def test_unknown_subscriber_returns_511(service: ProvisioningService) -> None:
    outcome = service.handle(query(imsi="001019999999999"))
    assert outcome.status_code == 511
    assert outcome.metric == "Challenge511"


def test_request_without_any_identifier_returns_511(service: ProvisioningService) -> None:
    assert service.handle(ConfigQuery()).status_code == 511


def test_invalid_token_sends_the_client_back_to_bootstrapping(
    service: ProvisioningService,
) -> None:
    outcome = service.handle(query(token="bogus"))
    assert outcome.status_code == 511
    assert outcome.detail == "token_invalid"


# ---------------------------------------------------------------------- 403
@pytest.mark.spec
def test_non_entitled_subscriber_returns_403(
    service: ProvisioningService, seeded_store: MemoryStore, subscriber: Subscriber
) -> None:
    subscriber.entitled = False
    seeded_store.put_subscriber(subscriber)
    outcome = service.handle(query())
    assert outcome.status_code == 403
    assert outcome.body == b""


def test_imei_not_on_the_allowlist_returns_403(
    service: ProvisioningService, seeded_store: MemoryStore, subscriber: Subscriber
) -> None:
    subscriber.imei_allowlist = ["356938035643800"]
    seeded_store.put_subscriber(subscriber)
    assert service.handle(query()).status_code == 403


# -------------------------------------------------------------------- tokens
def test_token_skips_the_otp_challenge(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    service.handle(query())
    first = service.handle(query(otp=read_otp(seeded_store)))
    token = _token_from(first.body)
    seeded_store.delete_otp(TEST_MSISDN)

    outcome = service.handle(query(token=token, vers=0))
    assert outcome.status_code == 200
    assert outcome.detail == IdentityMethod.TOKEN.value


def test_token_from_another_handset_is_refused(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    token = token_mod.issue_token(seeded_store, TEST_IMSI, TEST_IMEI, 3600)
    outcome = service.handle(query(token=token, imei="356938035643800"))
    assert outcome.status_code == 511


def test_revoked_token_forces_rebootstrap(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    token = token_mod.issue_token(seeded_store, TEST_IMSI, TEST_IMEI, 3600)
    seeded_store.revoke_tokens_for_imsi(TEST_IMSI)
    assert service.handle(query(token=token)).status_code == 511


def test_orphaned_token_is_rejected(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    token = token_mod.issue_token(seeded_store, "001019999999999", TEST_IMEI, 3600)
    outcome = service.handle(query(token=token))
    assert outcome.status_code == 511
    assert outcome.detail == "token_orphaned"


# --------------------------------------------------------------- versioning
@pytest.mark.spec
def test_client_holding_the_current_version_gets_a_vers_only_document(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    token = token_mod.issue_token(seeded_store, TEST_IMSI, TEST_IMEI, 3600)
    outcome = service.handle(query(token=token, vers=1))
    assert outcome.status_code == 200
    assert outcome.metric == "ConfigUnchanged"
    assert b"ap2002" not in outcome.body
    assert b'name="version" value="1"' in outcome.body


def test_stale_client_version_receives_the_full_document(
    service: ProvisioningService, seeded_store: MemoryStore, subscriber: Subscriber
) -> None:
    subscriber.provisioning_version = 5
    seeded_store.put_subscriber(subscriber)
    token = token_mod.issue_token(seeded_store, TEST_IMSI, TEST_IMEI, 3600)
    outcome = service.handle(query(token=token, vers=3))
    assert outcome.version == 5
    assert b"ap2002" in outcome.body


@pytest.mark.spec
@pytest.mark.parametrize("forced", [0, -1, -2, -3, -4])
def test_forced_disable_values_are_served_without_configuration(
    service: ProvisioningService,
    seeded_store: MemoryStore,
    subscriber: Subscriber,
    forced: int,
) -> None:
    subscriber.forced_vers = forced
    seeded_store.put_subscriber(subscriber)
    token = token_mod.issue_token(seeded_store, TEST_IMSI, TEST_IMEI, 3600)
    outcome = service.handle(query(token=token))
    assert outcome.status_code == 200
    assert outcome.version == forced
    assert f'value="{forced}"'.encode() in outcome.body
    assert b"ap2002" not in outcome.body


def test_zero_stored_version_is_repaired_to_one(
    service: ProvisioningService, seeded_store: MemoryStore, subscriber: Subscriber
) -> None:
    subscriber.provisioning_version = 0
    seeded_store.put_subscriber(subscriber)
    token = token_mod.issue_token(seeded_store, TEST_IMSI, TEST_IMEI, 3600)
    assert service.handle(query(token=token)).version == 1


# ------------------------------------------------------------- SMS failures
def test_port_addressed_otp_is_refused_rather_than_downgraded(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    class PortRefusingSender:
        name = "refuser"

        def send(self, request: SmsRequest) -> SmsResult:
            if request.requires_binary:
                raise UnsupportedDelivery("no UDH support")
            return SmsResult(self.name, "1")

    service = ProvisioningService(settings, seeded_store, PortRefusingSender())
    outcome = service.handle(query(sms_port=37273))
    assert outcome.status_code == 503
    assert outcome.headers["Retry-After"] == "3600"
    # The challenge must not be left dangling for a message that cannot arrive.
    assert seeded_store.get_otp(TEST_MSISDN) is None


def test_daily_quota_exhaustion_returns_429(settings: Settings, seeded_store: MemoryStore) -> None:
    tight = settings.model_copy(
        update={"otp_resend_cooldown_seconds": 0, "otp_max_sends_per_msisdn_per_day": 1}
    )
    service = ProvisioningService(tight, seeded_store, MockSmsSender(seeded_store))
    assert service.handle(query()).status_code == 200
    seeded_store.delete_otp(TEST_MSISDN)
    outcome = service.handle(query())
    assert outcome.status_code == 429
    assert "Retry-After" in outcome.headers


# ------------------------------------------------------------ identity chain
def test_enriched_identity_from_a_trusted_peer_authenticates(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    trusted = settings.model_copy(update={"trusted_proxy_cidrs": "10.0.0.0/8"})
    service = ProvisioningService(trusted, seeded_store, MockSmsSender(seeded_store))
    identity = service.resolve_identity(
        query(imsi=None), {"x-3gpp-intended-identity": TEST_MSISDN}, "10.1.2.3"
    )
    assert identity.decision is IdentityDecision.AUTHENTICATED
    assert identity.method is IdentityMethod.ENRICHMENT


def test_enriched_identity_from_an_untrusted_peer_is_ignored(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    trusted = settings.model_copy(update={"trusted_proxy_cidrs": "10.0.0.0/8"})
    service = ProvisioningService(trusted, seeded_store, MockSmsSender(seeded_store))
    identity = service.resolve_identity(
        query(imsi=None), {"x-3gpp-intended-identity": TEST_MSISDN}, "203.0.113.5"
    )
    assert identity.decision is not IdentityDecision.AUTHENTICATED


def test_bare_msisdn_parameter_is_only_a_claim(service: ProvisioningService) -> None:
    # A query parameter is not a credential: it must produce a challenge.
    identity = service.resolve_identity(query(imsi=None, msisdn=TEST_MSISDN), {}, None)
    assert identity.decision is IdentityDecision.CHALLENGE_OTP


def test_gba_challenge_replaces_511_when_enabled(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    gba_settings = settings.model_copy(update={"gba_enabled": True})
    service = ProvisioningService(
        gba_settings,
        seeded_store,
        MockSmsSender(seeded_store),
        bsf_client=__import__("acs.auth.gba", fromlist=["x"]).MockBsfClient(),
    )
    outcome = service.handle(query(imsi="001019999999999"))
    assert outcome.status_code == 401
    assert "AKAv1-MD5" in outcome.headers["WWW-Authenticate"]


def _gba_service(
    settings: Settings, seeded_store: MemoryStore, secret: str = "nonce-key"
) -> tuple[ProvisioningService, object, Settings]:
    from acs.auth.gba import MockBsfClient

    bsf = MockBsfClient({"btid-1": f"{TEST_IMSI}@ims.example.org"})
    gba_settings = settings.model_copy(update={"gba_enabled": True, "gba_nonce_secret": secret})
    service = ProvisioningService(gba_settings, seeded_store, MockSmsSender(seeded_store), bsf)
    return service, bsf, gba_settings


def _gba_authorization(
    bsf: object, gba_settings: Settings, nonce: str, uri: str = "/config", method: str = "GET"
) -> str:
    from acs.auth import gba as gba_mod

    keys = bsf.fetch_keys("btid-1", gba_settings.gba_realm)  # type: ignore[attr-defined]
    assert keys is not None
    response = gba_mod.digest_response(
        username="btid-1",
        realm=gba_settings.gba_realm,
        password=keys.ks_naf,
        method=method,
        uri=uri,
        nonce=nonce,
        cnonce="cnonce",
        nc="00000001",
    )
    return (
        f'Digest username="btid-1", realm="{gba_settings.gba_realm}", '
        f'nonce="{nonce}", uri="{uri}", response="{response}", '
        'cnonce="cnonce", nc=00000001, qop=auth'
    )


def test_gba_bootstrapped_identity_authenticates(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    from acs.auth import gba as gba_mod

    service, bsf, gba_settings = _gba_service(settings, seeded_store)
    nonce = gba_mod.make_nonce(gba_settings.gba_nonce_secret)
    header = _gba_authorization(bsf, gba_settings, nonce)
    identity = service.resolve_identity(query(imsi=None), {"authorization": header}, None, "GET")
    assert identity.decision is IdentityDecision.AUTHENTICATED
    assert identity.method is IdentityMethod.GBA


def test_gba_btid_alone_does_not_authenticate(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    # A B-TID travels in the clear in the username directive. Accepting it without
    # verifying the digest response would let anyone who has seen one provision as
    # that subscriber.
    service, _bsf, _gba_settings = _gba_service(settings, seeded_store)
    identity = service.resolve_identity(
        query(imsi=None), {"authorization": 'Digest username="btid-1"'}, None, "GET"
    )
    assert identity.decision is not IdentityDecision.AUTHENTICATED
    assert identity.detail == "gba_incomplete"


def test_gba_forged_digest_response_is_rejected(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    from acs.auth import gba as gba_mod

    service, _bsf, gba_settings = _gba_service(settings, seeded_store)
    nonce = gba_mod.make_nonce(gba_settings.gba_nonce_secret)
    header = (
        f'Digest username="btid-1", realm="{gba_settings.gba_realm}", nonce="{nonce}", '
        'uri="/config", response="00000000000000000000000000000000", '
        'cnonce="cnonce", nc=00000001, qop=auth'
    )
    identity = service.resolve_identity(query(imsi=None), {"authorization": header}, None, "GET")
    assert identity.decision is not IdentityDecision.AUTHENTICATED
    assert identity.detail == "gba_response_mismatch"


def test_gba_nonce_the_server_never_issued_is_rejected(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    service, bsf, gba_settings = _gba_service(settings, seeded_store)
    header = _gba_authorization(bsf, gba_settings, nonce="aW52ZW50ZWQ=")
    identity = service.resolve_identity(query(imsi=None), {"authorization": header}, None, "GET")
    assert identity.decision is not IdentityDecision.AUTHENTICATED
    assert identity.detail == "gba_nonce_invalid"


def test_gba_response_bound_to_the_http_method(
    settings: Settings, seeded_store: MemoryStore
) -> None:
    from acs.auth import gba as gba_mod

    service, bsf, gba_settings = _gba_service(settings, seeded_store)
    nonce = gba_mod.make_nonce(gba_settings.gba_nonce_secret)
    header = _gba_authorization(bsf, gba_settings, nonce, method="GET")
    # Replaying a GET-derived response on a POST must not verify.
    identity = service.resolve_identity(query(imsi=None), {"authorization": header}, None, "POST")
    assert identity.decision is not IdentityDecision.AUTHENTICATED


def test_gba_cannot_be_enabled_without_a_nonce_secret() -> None:
    problems = Settings(env="test", gba_enabled=True, gba_nonce_secret="").validate_startup()
    assert any("gba_nonce_secret" in p for p in problems)


# ------------------------------------------------------------ side effects
def test_device_inventory_is_recorded(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    service.handle(query())
    service.handle(
        query(
            otp=read_otp(seeded_store),
            terminal_vendor="Sim",
            terminal_model="SimPhone",
            terminal_sw_version="1.2",
        )
    )
    device = seeded_store.get_device(TEST_IMEI)
    assert device is not None
    assert device.model == "SimPhone"
    assert device.imsi == TEST_IMSI


def test_dm_password_is_generated_once(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    service.handle(query())
    service.handle(query(otp=read_otp(seeded_store)))
    first = seeded_store.get_subscriber(TEST_IMSI)
    assert first is not None and first.dm_password

    token = token_mod.issue_token(seeded_store, TEST_IMSI, TEST_IMEI, 3600)
    service.handle(query(token=token, vers=99))
    second = seeded_store.get_subscriber(TEST_IMSI)
    assert second is not None
    assert second.dm_password == first.dm_password


def test_every_response_forbids_caching(service: ProvisioningService) -> None:
    # Responses carry IMS credentials and tokens.
    outcome = service.handle(query())
    assert "no-store" in outcome.headers["Cache-Control"]


def _token_from(payload: bytes) -> str:
    root = writer.parse(payload)
    node = root.find("characteristic[@type='TOKEN']/parm[@name='token']")
    assert node is not None
    return node.get("value") or ""


def test_otp_verification_consumes_the_challenge(
    service: ProvisioningService, seeded_store: MemoryStore
) -> None:
    service.handle(query())
    clear = read_otp(seeded_store)
    service.handle(query(otp=clear))
    assert seeded_store.get_otp(TEST_MSISDN) is None
    assert (
        otp_mod.verify_challenge(
            seeded_store, TEST_MSISDN, clear, otp_mod.policy_from_settings(service._settings)
        )
        == otp_mod.NO_CHALLENGE
    )
