"""FastAPI application factory backed only by public runtime contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from bianbu_agent.api import install_api_routes
from bianbu_agent.server.auth import AdminAuthMiddleware, auth_router
from bianbu_agent.server.metrics import RuntimeMetrics
from bianbu_agent.server.maintenance import UpgradeMaintenanceMiddleware
from bianbu_agent.server.runtime import BianbuAgentRuntime


_WEB_ROOT = Path(__file__).parent.parent / "console" / "web"
# First-party assets whose URL carries the console asset version. Vendored
# bundles stay unversioned: their directory name already changes with an
# upstream bump.
_CONSOLE_ASSETS = (
    "js/think_filter.js",
    "js/session-runtime.js",
    "js/trajectory.js",
    "js/console.js",
    "css/console.css",
)


def _console_asset_version(static_root: Path) -> str:
    """Digest the first-party assets so a cached URL cannot outlive a fix.

    Every asset in `_CONSOLE_ASSETS` must be referenced with this value,
    otherwise an upgrade that only touches it keeps serving the cached copy.
    """
    digest = hashlib.sha256()
    for name in _CONSOLE_ASSETS:
        digest.update(name.encode("utf-8"))
        digest.update((static_root / name).read_bytes())
    return digest.hexdigest()[:16]


def create_app(
    runtime: BianbuAgentRuntime,
    *,
    title: str = "Bianbu Agent",
    manage_lifespan: bool = True,
) -> FastAPI:
    """Create the single Runtime API and Web Console application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if manage_lifespan:
            await runtime.start()
        try:
            await app.state.session_input_broker.recover()
            yield
        finally:
            await app.state.session_title_manager.close()
            await app.state.session_input_broker.close()
            await app.state.mcp_health_cache.close()
            if manage_lifespan:
                await runtime.close()

    app = FastAPI(title=title, lifespan=lifespan)
    app.state.metrics = getattr(runtime, "metrics", RuntimeMetrics())
    app.add_middleware(AdminAuthMiddleware)
    app.add_middleware(UpgradeMaintenanceMiddleware)
    app.include_router(auth_router)

    @app.middleware("http")
    async def collect_runtime_metrics(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            app.state.metrics.inc(
                "runtime_control_http_requests_total",
                route=request.url.path, status=500,
            )
            raise
        app.state.metrics.inc(
            "runtime_control_http_requests_total",
            route=request.url.path, status=response.status_code,
        )
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(app.state.metrics.render(), media_type="text/plain; version=0.0.4")
    install_api_routes(app, runtime)
    static_root = _WEB_ROOT / "static"
    asset_version = _console_asset_version(static_root)
    if static_root.is_dir():
        app.mount("/assets", StaticFiles(directory=static_root), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/chat", include_in_schema=False)
    async def product_console() -> HTMLResponse:
        template = (_WEB_ROOT / "chat.html").read_text(encoding="utf-8")
        return HTMLResponse(
            template.replace(
                "{{BIANBU_DEFAULT_LANG}}",
                json.dumps(runtime.config.language, ensure_ascii=False),
            ).replace("{{BIANBU_ASSET_VERSION}}", asset_version),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    return app


async def serve_app(
    app: FastAPI, *, host: str = "127.0.0.1", port: int = 9899, log_level: str = "info"
) -> None:
    """Run one already-composed FastAPI application with the runtime server."""

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level=log_level,
            workers=1,
        )
    )
    await server.serve()


__all__ = ["create_app", "serve_app"]
