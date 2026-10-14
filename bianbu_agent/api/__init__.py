"""Versioned resource-oriented HTTP API installation."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from bianbu_agent.api.errors import install_error_handlers
from bianbu_agent.api.agents import router as agent_router
from bianbu_agent.api.workspace import router as workspace_router
from bianbu_agent.api.attachments import router as attachment_router
from bianbu_agent.api.runs import RunBroker
from bianbu_agent.api.sessions import router as session_router
from bianbu_agent.api.events import router as event_router
from bianbu_agent.api.models import router as model_router
from bianbu_agent.api.operations import router as operations_router
from bianbu_agent.api.local_models import router as local_model_router
from bianbu_agent.api.packages import router as package_router
from bianbu_agent.api.mcp_health import MCPHealthCache
from bianbu_agent.api.mcp_servers import (
    health_router as mcp_health_router,
    router as mcp_server_router,
)
from bianbu_agent.api.capabilities import router as capability_router
from bianbu_agent.api.monitoring import router as monitoring_router
from bianbu_agent.api.executions import router as execution_router
from bianbu_agent.api.runtime_status import (
    health_router as runtime_health_router,
    router as runtime_status_router,
)
from bianbu_agent.api.security import router as security_router
from bianbu_agent.server.runtime import BianbuAgentRuntime
from bianbu_agent.sessions.input import SessionInputBroker
from bianbu_agent.sessions.title_manager import SessionTitleManager


router = APIRouter(prefix="/api/v1")
router.include_router(session_router)
router.include_router(workspace_router)
router.include_router(attachment_router)
router.include_router(event_router)
router.include_router(agent_router)
router.include_router(model_router)
router.include_router(package_router)
router.include_router(operations_router)
router.include_router(local_model_router)
router.include_router(mcp_server_router)
router.include_router(mcp_health_router)
router.include_router(capability_router)
router.include_router(monitoring_router)
router.include_router(execution_router)
router.include_router(runtime_status_router)


def install_api_routes(app: FastAPI, runtime: BianbuAgentRuntime) -> None:
    """Attach the complete versioned API boundary to one application."""

    app.state.runtime = runtime
    app.state.run_broker = RunBroker(runtime)
    app.state.session_input_broker = SessionInputBroker(runtime)
    app.state.session_title_manager = SessionTitleManager(runtime)
    app.state.mcp_health_cache = MCPHealthCache(runtime)
    install_error_handlers(app)
    app.include_router(router)
    # Integrated security plugin uses a stable unversioned service endpoint.
    app.include_router(security_router)
    app.include_router(runtime_health_router)


__all__ = ["install_api_routes", "router"]
