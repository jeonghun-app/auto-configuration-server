"""FastAPI application factory."""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from acs import __version__
from acs.api import admin, dev, dm, health, msisdn_ui, provisioning
from acs.api.deps import AppState
from acs.config import Settings, get_settings
from acs.errors import AcsError
from acs.observability import configure_logging, get_logger, request_id_var
from acs.store.base import Store

log = get_logger(__name__)

DESCRIPTION = """
GSMA RCS Auto Configuration Server.

* **RCC.14 / RCC.07 OMA-CP** — client configuration over HTTP, returning a
  `wap-provisioningdoc`.
* **OMA-DM (SyncML DM 1.2)** — device management plane for VoLTE and device
  inventory, bootstrapped from the OMA-CP `w7` characteristic.
"""


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    """Build the ASGI application.

    ``settings`` and ``store`` are injectable so tests can run the whole stack
    in-process against an in-memory store.
    """
    resolved = settings or get_settings()
    configure_logging(resolved)

    problems = resolved.validate_startup()
    if problems:
        # Fail loudly at construction. A misconfigured ACS that starts anyway can
        # disable RCS on every device that talks to it.
        raise RuntimeError("invalid configuration: " + "; ".join(problems))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = AppState.build(resolved, store)
        app.state.acs = state
        omacp_count, omadm_count = state.warm_catalogues()
        log.info(
            "acs started",
            extra={
                "version": __version__,
                "env": resolved.env,
                "store": resolved.store_backend,
                "sms": state.sms.name,
                "omacp_parameters": omacp_count,
                "omadm_nodes": omadm_count,
                "config_paths": resolved.config_path_list,
                "dm_enabled": resolved.dm_enabled,
            },
        )
        yield
        log.info("acs stopped")

    app = FastAPI(
        title="GSMA RCS Auto Configuration Server",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if not resolved.is_prod else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not resolved.is_prod else None,
    )

    _install_middleware(app)
    _install_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(msisdn_ui.router)
    if resolved.dm_enabled:
        app.include_router(dm.router)
    if resolved.dev_endpoints_enabled and not resolved.is_prod:
        app.include_router(dev.router)

    config_router = APIRouter()
    provisioning.register_config_paths(config_router, resolved.config_path_list)
    app.include_router(config_router)

    return app


def _install_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-Id"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
        return response


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AcsError)
    async def acs_error_handler(request: Request, exc: AcsError) -> Response:  # noqa: ARG001
        log.info("domain error", extra={"reason": exc.reason, "status": exc.status_code})
        if exc.status_code == 200:
            return Response(status_code=200, headers={"Content-Length": "0"})
        return Response(status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> Response:  # noqa: ARG001
        # Never leak a stack trace or internal detail to a device.
        log.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse(status_code=500, content={"error": "internal_error"})


app = None
"""Populated lazily by :func:`create_app`; uvicorn uses the factory instead."""
