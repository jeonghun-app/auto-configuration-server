"""OMA-DM (SyncML DM 1.2) endpoint.

Devices are bootstrapped onto this endpoint by the OMA-CP ``w7`` characteristic
emitted during RCS provisioning, so the same ACS deployment serves both planes.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from acs.api.deps import AppState
from acs.observability import get_logger
from acs.protocol.omadm.motree import get_tree
from acs.protocol.omadm.syncml import CONTENT_TYPE_XML

log = get_logger(__name__)
router = APIRouter(tags=["oma-dm"])

MAX_BODY_BYTES = 512 * 1024


@router.post("/dm", summary="OMA-DM SyncML session")
async def dm_session(request: Request) -> Response:
    app_state: AppState = request.app.state.acs
    payload = await request.body()
    if len(payload) > MAX_BODY_BYTES:
        return Response(status_code=413)

    outcome = app_state.dm.handle(payload, request.headers.get("content-type", ""))
    app_state.metrics.emit(
        outcome.metric or "DmRequest",
        1,
        dimensions={"Outcome": outcome.metric or "unknown"},
    )
    log.info(
        "dm package handled",
        extra={
            "status": outcome.status_code,
            "outcome": outcome.metric,
            "detail": outcome.detail,
            "finished": outcome.session_finished,
        },
    )
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        **outcome.headers,
    }
    if not outcome.body:
        headers.setdefault("Content-Length", "0")
        return Response(status_code=outcome.status_code, headers=headers)
    return Response(
        content=outcome.body,
        status_code=outcome.status_code,
        media_type=outcome.content_type or CONTENT_TYPE_XML,
        headers=headers,
    )


@router.get("/dm/mo", summary="List the supported management objects")
def list_management_objects() -> dict[str, object]:
    """Introspection endpoint: which MOs this server can manage.

    Read-only and free of subscriber data, so it is safe to expose. Useful when
    adding a new management object: the new nodes appear here immediately.
    """
    tree = get_tree()
    return {
        "objects": [
            {
                "id": mo.id,
                "urn": mo.urn,
                "root": mo.root,
                "title": mo.title,
                "spec": mo.spec,
                "nodes": mo.node_count(),
            }
            for mo in tree.objects
        ],
        "total_nodes": len(tree.all_nodes()),
        "verified_nodes": tree.verified_count,
    }
