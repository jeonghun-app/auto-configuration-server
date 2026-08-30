"""Operator admin API.

Fail-closed by design: with ``ACS_ADMIN_TOKEN`` unset (the default) every route
answers 503. There is no default token, so an accidentally exposed deployment
cannot be administered by guessing one.
"""

from __future__ import annotations

import hmac
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from acs.api.deps import AppState
from acs.auth import token as token_mod
from acs.domain.models import Subscriber
from acs.observability import get_logger
from acs.protocol import vers as vers_mod
from acs.protocol.omacp.catalog import available_profiles, get_catalog
from acs.protocol.omadm.motree import get_tree
from acs.security.pii import normalise_msisdn

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(
    request: Request,
    authorization: str = Header(default=""),
) -> AppState:
    """Bearer-token gate for the admin API."""
    app_state: AppState = request.app.state.acs
    expected = app_state.settings.admin_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="admin API disabled: ACS_ADMIN_TOKEN is not configured",
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid admin credentials")
    return app_state


class SubscriberIn(BaseModel):
    msisdn: str
    entitled: bool = True
    rcs_profile: str = ""
    provisioning_version: int = Field(default=1, ge=0)
    forced_vers: int | None = None
    imei_allowlist: list[str] = Field(default_factory=list)
    overrides: dict[str, str] = Field(default_factory=dict)
    volte_enabled: bool = True

    @field_validator("msisdn")
    @classmethod
    def _msisdn(cls, value: str) -> str:
        normalised = normalise_msisdn(value)
        if normalised is None:
            raise ValueError("msisdn must be a valid E.164 number")
        return normalised

    @field_validator("forced_vers")
    @classmethod
    def _forced(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value not in vers_mod.FORCEABLE_VERSIONS:
            raise ValueError(f"forced_vers must be one of {list(vers_mod.FORCEABLE_VERSIONS)}")
        return value


class SubscriberOut(BaseModel):
    imsi: str
    msisdn: str
    entitled: bool
    provisioning_version: int
    forced_vers: int | None
    rcs_profile: str
    volte_enabled: bool
    imei_allowlist: list[str]
    overrides: dict[str, str]
    dm_bootstrapped: bool
    updated_at: int

    @classmethod
    def of(cls, subscriber: Subscriber) -> SubscriberOut:
        return cls(
            imsi=subscriber.imsi,
            msisdn=subscriber.msisdn,
            entitled=subscriber.entitled,
            provisioning_version=subscriber.provisioning_version,
            forced_vers=subscriber.forced_vers,
            rcs_profile=subscriber.rcs_profile,
            volte_enabled=subscriber.volte_enabled,
            imei_allowlist=list(subscriber.imei_allowlist),
            overrides=dict(subscriber.overrides),
            dm_bootstrapped=bool(subscriber.dm_password),
            updated_at=subscriber.updated_at,
        )


def _load(app_state: AppState, imsi: str) -> Subscriber:
    subscriber = app_state.store.get_subscriber(imsi)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="subscriber not found")
    return subscriber


@router.get("/subscribers", response_model=list[SubscriberOut])
def list_subscribers(
    limit: int = Query(default=100, ge=1, le=1000),
    app_state: AppState = Depends(require_admin),
) -> list[SubscriberOut]:
    return [SubscriberOut.of(s) for s in app_state.store.list_subscribers(limit)]


@router.get("/subscribers/{imsi}", response_model=SubscriberOut)
def get_subscriber(imsi: str, app_state: AppState = Depends(require_admin)) -> SubscriberOut:
    return SubscriberOut.of(_load(app_state, imsi))


@router.put("/subscribers/{imsi}", response_model=SubscriberOut, status_code=200)
def put_subscriber(
    imsi: str,
    payload: SubscriberIn,
    app_state: AppState = Depends(require_admin),
) -> SubscriberOut:
    if not imsi.isdigit() or not 5 <= len(imsi) <= 15:
        raise HTTPException(status_code=400, detail="IMSI must be 5-15 digits")
    existing = app_state.store.get_subscriber(imsi)
    subscriber = Subscriber(
        imsi=imsi,
        msisdn=payload.msisdn,
        entitled=payload.entitled,
        provisioning_version=payload.provisioning_version,
        forced_vers=payload.forced_vers,
        rcs_profile=payload.rcs_profile,
        imei_allowlist=payload.imei_allowlist,
        overrides=payload.overrides,
        volte_enabled=payload.volte_enabled,
        dm_password=existing.dm_password if existing else "",
    )
    app_state.store.put_subscriber(subscriber)
    log.info("subscriber upserted", extra={"imsi": imsi, "created": existing is None})
    return SubscriberOut.of(subscriber)


@router.delete("/subscribers/{imsi}", status_code=204, response_class=Response)
def delete_subscriber(imsi: str, app_state: AppState = Depends(require_admin)) -> Response:
    _load(app_state, imsi)
    app_state.store.revoke_tokens_for_imsi(imsi)
    app_state.store.delete_subscriber(imsi)
    log.info("subscriber deleted", extra={"imsi": imsi})
    return Response(status_code=204)


@router.post("/subscribers/{imsi}/invalidate", response_model=SubscriberOut)
def invalidate_configuration(
    imsi: str, app_state: AppState = Depends(require_admin)
) -> SubscriberOut:
    """Bump the configuration version so the client re-provisions."""
    subscriber = _load(app_state, imsi)
    subscriber.provisioning_version = vers_mod.next_version(subscriber.provisioning_version)
    subscriber.forced_vers = None
    app_state.store.put_subscriber(subscriber)
    log.info(
        "configuration invalidated",
        extra={"imsi": imsi, "version": subscriber.provisioning_version},
    )
    return SubscriberOut.of(subscriber)


@router.post("/subscribers/{imsi}/disable", response_model=SubscriberOut)
def disable_subscriber(
    imsi: str,
    vers: int = Query(default=-2, description="Disable value to force"),
    app_state: AppState = Depends(require_admin),
) -> SubscriberOut:
    """Force a disable/dormant/blocked configuration version."""
    if vers not in vers_mod.FORCEABLE_VERSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"vers must be one of {list(vers_mod.FORCEABLE_VERSIONS)}",
        )
    subscriber = _load(app_state, imsi)
    subscriber.forced_vers = vers
    app_state.store.put_subscriber(subscriber)
    revoked = app_state.store.revoke_tokens_for_imsi(imsi)
    rule = vers_mod.rule_for(vers)
    log.info(
        "subscriber disabled",
        extra={"imsi": imsi, "vers": vers, "action": rule.action.value, "revoked": revoked},
    )
    return SubscriberOut.of(subscriber)


@router.post("/subscribers/{imsi}/enable", response_model=SubscriberOut)
def enable_subscriber(imsi: str, app_state: AppState = Depends(require_admin)) -> SubscriberOut:
    subscriber = _load(app_state, imsi)
    subscriber.forced_vers = None
    subscriber.entitled = True
    subscriber.provisioning_version = vers_mod.next_version(subscriber.provisioning_version)
    app_state.store.put_subscriber(subscriber)
    return SubscriberOut.of(subscriber)


@router.post("/subscribers/{imsi}/revoke-tokens")
def revoke_tokens(imsi: str, app_state: AppState = Depends(require_admin)) -> dict[str, int]:
    _load(app_state, imsi)
    return {"revoked": app_state.store.revoke_tokens_for_imsi(imsi)}


class IssuedToken(BaseModel):
    token: str
    imsi: str
    imei: str | None
    expires_at: int


@router.post("/subscribers/{imsi}/issue-token", response_model=IssuedToken)
def issue_token(
    imsi: str,
    imei: str = Query(default="", description="Bind the token to this IMEI"),
    app_state: AppState = Depends(require_admin),
) -> IssuedToken:
    """Mint a provisioning token for a subscriber, out of band.

    Two legitimate uses:

    * **Pre-provisioning.** An operator hands a device a token so it can fetch
      configuration on first boot without an SMS round trip.
    * **Verifying a deployment.** The OTP flow needs a real SMS, and the mock
      outbox does not exist outside development. This lets
      ``scripts/verify_stack.py`` exercise the whole configuration path against a
      staging or production deployment without spending money on SMS.

    The token is returned once and only its digest is stored, exactly as for a
    token issued through the normal flow. It is bound to the IMSI and, when
    ``imei`` is supplied, to that handset.
    """
    subscriber = _load(app_state, imsi)
    if not subscriber.entitled:
        raise HTTPException(status_code=409, detail="subscriber is not entitled")

    settings = app_state.settings
    token = token_mod.issue_token(
        store=app_state.store,
        imsi=subscriber.imsi,
        imei=imei or None,
        ttl_seconds=settings.token_ttl_seconds,
        bind_imei=settings.token_bind_imei and bool(imei),
    )
    log.info(
        "provisioning token issued out of band",
        extra={"imsi": imsi, "imei": imei or None, "bound": bool(imei)},
    )
    return IssuedToken(
        token=token,
        imsi=subscriber.imsi,
        imei=imei or None,
        expires_at=int(time.time()) + settings.token_ttl_seconds,
    )


@router.get("/devices")
def list_devices(
    limit: int = Query(default=100, ge=1, le=1000),
    app_state: AppState = Depends(require_admin),
) -> list[dict[str, object]]:
    """Device inventory built from RCC.14 parameters and OMA-DM DevInfo."""
    return [
        {
            "device_id": d.device_id,
            "manufacturer": d.manufacturer,
            "model": d.model,
            "sw_version": d.sw_version,
            "client_vendor": d.client_vendor,
            "client_version": d.client_version,
            "dm_client_version": d.dm_client_version,
            "last_seen_at": d.last_seen_at,
            "mo_node_count": len(d.mo_values),
        }
        for d in app_state.store.list_devices(limit)
    ]


@router.get("/coverage")
def coverage(app_state: AppState = Depends(require_admin)) -> dict[str, object]:
    """Specification coverage summary, straight from the catalogues."""
    catalog = get_catalog(app_state.settings.default_rcs_profile)
    tree = get_tree()
    return {
        "omacp": {
            "profile": app_state.settings.default_rcs_profile,
            "available_profiles": available_profiles(),
            "parameters": len(catalog.entries),
            "verified": catalog.verified_count,
            "app_ids": catalog.app_ids(),
        },
        "omadm": {
            "management_objects": len(tree.objects),
            "urns": tree.urns,
            "nodes": len(tree.all_nodes()),
            "verified": tree.verified_count,
        },
        "vers_rules": [
            {
                "version": rule.version,
                "action": rule.action.value,
                "verified": rule.verified,
                "spec": rule.spec_ref,
            }
            for rule in vers_mod.VERS_RULES
        ],
    }
