"""Health endpoints.

``/healthz`` is shallow on purpose: the ALB polls it every few seconds, so it must
not call AWS. A dependency hiccup would otherwise take healthy tasks out of
service and turn a partial outage into a total one.

``/readyz`` is the deep check — store reachability plus catalogue integrity.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from acs import __version__
from acs.api.deps import AppState, state

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness (no external calls)")
def healthz(app_state: AppState = Depends(state)) -> dict[str, object]:
    return {
        "status": "ok",
        "service": app_state.settings.service_name,
        "version": __version__,
        "env": app_state.settings.env,
    }


@router.get("/readyz", summary="Readiness (checks store and catalogues)")
def readyz(response: Response, app_state: AppState = Depends(state)) -> dict[str, object]:
    checks: dict[str, object] = {}

    try:
        checks["store"] = "ok" if app_state.store.health() else "unavailable"
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        checks["store"] = f"error: {type(exc).__name__}"

    try:
        omacp_entries, omadm_nodes = app_state.warm_catalogues()
        checks["omacp_parameters"] = omacp_entries
        checks["omadm_nodes"] = omadm_nodes
    except Exception as exc:  # noqa: BLE001
        checks["catalog"] = f"error: {exc}"

    checks["sms_provider"] = app_state.sms.name
    checks["store_backend"] = app_state.settings.store_backend
    checks["dm_enabled"] = app_state.settings.dm_enabled

    ready = checks.get("store") == "ok" and "catalog" not in checks
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "not_ready", "checks": checks}
