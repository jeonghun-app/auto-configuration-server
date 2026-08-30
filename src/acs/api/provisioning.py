"""The RCC.14 HTTP configuration endpoint.

Registered on every path in ``ACS_CONFIG_PATHS`` (default ``/``, ``/config`` and
``/rcs/config``) because deployed clients differ about which path they call.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from acs.api.deps import AppState
from acs.errors import MalformedRequest
from acs.observability import get_logger
from acs.protocol.request import parse_config_query

log = get_logger(__name__)
router = APIRouter(tags=["provisioning"])


async def _multi_items(request: Request) -> dict[str, list[str]]:
    """Group repeated parameters from the query string and, on POST, the body.

    Reading the body matters: the whole point of offering POST is that a client
    can keep the OTP out of the query string, which otherwise lands in every
    proxy and load balancer access log on the way.
    """
    grouped: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        grouped.setdefault(key, []).append(value)

    if request.method == "POST":
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type or "multipart/" in content_type:
            form = await request.form()
            for key, raw in form.multi_items():
                # An uploaded file is never a configuration parameter.
                if isinstance(raw, str):
                    grouped.setdefault(key, []).append(raw)
    return grouped


async def handle_configuration_request(request: Request) -> Response:
    """Serve one RCC.14 configuration request."""
    app_state: AppState = request.app.state.acs
    settings = app_state.settings

    try:
        query = parse_config_query(
            await _multi_items(request),
            user_agent=request.headers.get("user-agent", ""),
            max_value_length=settings.max_query_value_length,
        )
    except MalformedRequest as exc:
        app_state.metrics.emit("MalformedRequest", 1)
        log.info("malformed configuration request", extra={"reason": str(exc)})
        # RCC.14 does not pin this down; 400 is the default and some operator
        # ACSs answer 403 instead. Configurable via ACS_MALFORMED_REQUEST_STATUS.
        return Response(status_code=settings.malformed_request_status)

    if query.unknown:
        log.info("unknown configuration parameters", extra={"names": list(query.unknown)})

    outcome = app_state.provisioning.handle(
        query=query,
        headers=dict(request.headers),
        peer=request.client.host if request.client else None,
        method=request.method,
    )

    app_state.metrics.emit(
        outcome.metric or "ConfigRequest",
        1,
        dimensions={"Outcome": outcome.metric or "unknown"},
    )
    if outcome.body:
        app_state.metrics.emit("ConfigBytes", len(outcome.body), unit="Bytes")

    log.info(
        "configuration request handled",
        extra={
            "status": outcome.status_code,
            "outcome": outcome.metric,
            "detail": outcome.detail,
            "version": outcome.version,
            **query.redactable(),
        },
    )

    headers = dict(outcome.headers)
    media_type = outcome.content_type or None
    if not outcome.body:
        # RCC.14 signals "OTP sent, repeat the request with OTP=" with a 200 and
        # an empty body, so Content-Length: 0 must be explicit.
        headers.setdefault("Content-Length", "0")
        return Response(status_code=outcome.status_code, headers=headers)

    return Response(
        content=outcome.body,
        status_code=outcome.status_code,
        media_type=media_type,
        headers=headers,
    )


def register_config_paths(app_router: APIRouter, paths: list[str]) -> None:
    """Attach the configuration handler to every configured path."""
    for path in paths:
        normalised = path if path.startswith("/") else f"/{path}"
        app_router.add_api_route(
            normalised,
            handle_configuration_request,
            methods=["GET"],
            name=f"configuration:{normalised}",
            summary="RCC.14 client configuration request",
            include_in_schema=normalised == "/config",
        )
        # A few clients POST the OTP step rather than repeating the GET, since a
        # query-string OTP leaks into every proxy log on the way.
        app_router.add_api_route(
            normalised,
            handle_configuration_request,
            methods=["POST"],
            name=f"configuration-post:{normalised}",
            include_in_schema=False,
        )
