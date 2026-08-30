"""Development-only endpoints.

``/dev/sms`` exposes the mock SMS outbox so an automated test harness can read
the OTP it was just sent. It is gated twice: ``ACS_DEV_ENDPOINTS_ENABLED`` must be
true *and* the environment must not be staging or prod.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from acs.api.deps import AppState, state

router = APIRouter(prefix="/dev", tags=["dev"])


def require_dev(app_state: AppState = Depends(state)) -> AppState:
    settings = app_state.settings
    if not settings.dev_endpoints_enabled or settings.is_prod:
        raise HTTPException(status_code=404, detail="not found")
    return app_state


@router.get("/sms", summary="Read the mock SMS outbox (dev only)")
def list_sms(
    msisdn: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    app_state: AppState = Depends(require_dev),
) -> list[dict[str, object]]:
    messages = app_state.store.list_sms(msisdn or None, limit)
    return [
        {
            "msisdn": m.msisdn,
            "body": m.body,
            "sms_port": m.sms_port,
            "binary": m.binary,
            "provider": m.provider,
            "sent_at": m.sent_at,
        }
        for m in messages
    ]
