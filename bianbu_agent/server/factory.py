"""Configuration-driven composition for the Bianbu Agent runtime service."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI
from bianbu_agent.config import RuntimeConfig
from bianbu_agent.home import AgentHome
from bianbu_agent.models import YAMLModelResolver
from bianbu_agent.server.runtime import BianbuAgentRuntime
from bianbu_agent.tools import ToolRuntimeServices


def create_runtime(
    config: RuntimeConfig,
    *,
    role: str = "worker",
    agent_id: str | None = None,
    run_store: object | None = None,
    session_store: object | None = None,
) -> BianbuAgentRuntime:
    """Compose one Runtime exclusively from validated configuration."""

    home = AgentHome(config.data_dir).prepare()
    model_resolver = YAMLModelResolver(home.models_dir)
    runtime_services = ToolRuntimeServices(
        home=home,
        model=None,
        model_resolver=model_resolver,
        config=config,
        sessions_root=home.sessions_dir,
        runtime_home=(home.root / "runtime" / "worker" if role == "worker" else home.root / "runtime"),
    )
    runtime = BianbuAgentRuntime(
        config=config,
        default_model=None,
        agent_id=agent_id,
        model_resolver=model_resolver,
        managed_plugin_services={"runtime_factory": runtime_services},
        role=role,
        run_store=run_store,
        session_store=session_store,
    )
    runtime_services.bind_sdk(runtime)
    return runtime


def create_runtime_app(
    config: RuntimeConfig,
    *,
    manage_lifespan: bool = True,
    role: str = "control",
) -> FastAPI:
    """Compose the Runtime and its sole FastAPI application from config."""

    from bianbu_agent.server.app import create_app

    return create_app(
        create_runtime(config, role=role),
        title=config.service.title,
        manage_lifespan=manage_lifespan,
    )


__all__ = ["create_runtime", "create_runtime_app"]
