"""Control-plane readiness and managed Worker status."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from bianbu_agent.server.maintenance import upgrade_snapshot

from bianbu_agent.api.contracts import (
    RuntimeStatusEnvelope,
    RuntimeStatusView,
    RuntimeWorkersEnvelope,
    RuntimeWorkersView,
)


router = APIRouter()
health_router = APIRouter()


@router.get('/runtime/maintenance')
async def runtime_maintenance() -> dict:
    return {'data': await upgrade_snapshot()}


def _view(runtime) -> RuntimeStatusView:
    return RuntimeStatusView.model_validate(runtime.runtime_status())


@router.get("/runtime/status", response_model=RuntimeStatusEnvelope)
async def runtime_status(request: Request) -> RuntimeStatusEnvelope:
    return RuntimeStatusEnvelope(data=_view(request.app.state.runtime))


@router.get("/runtime/workers", response_model=RuntimeWorkersEnvelope)
async def runtime_workers(request: Request) -> RuntimeWorkersEnvelope:
    runtime = request.app.state.runtime
    snapshot = runtime.runtime_workers_status()
    return RuntimeWorkersEnvelope(data=RuntimeWorkersView.model_validate(snapshot))


@health_router.get("/api/health", include_in_schema=False)
async def runtime_health(request: Request) -> JSONResponse:
    """Small process-health probe for systemd/ingress, intentionally undocumented."""
    view = _view(request.app.state.runtime)
    status_code = 200 if view.control_ready else 503
    return JSONResponse(
        {"status": view.status, "control_ready": view.control_ready, "agent_ready": view.agent_ready},
        status_code=status_code,
    )


__all__ = ["health_router", "router"]
