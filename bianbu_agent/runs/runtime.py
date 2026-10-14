from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from collections.abc import Mapping
from typing import TypeAlias

from bianbu_agent.agents import AgentDeclaration, AgentRegistry, AgentSpec
from bianbu_agent.runs.control import RunControl
from bianbu_agent.engines.contracts import AgentEngine
from bianbu_agent.runs.events import RunEvent
from bianbu_agent.plugins.host import PluginGeneration


class AgentNotFoundError(LookupError):
    """Raised when a host asks to run an unknown Agent ID."""


EngineBuilder: TypeAlias = Callable[
    [AgentSpec],
    AgentEngine | Awaitable[AgentEngine],
]
GenerationEngineBuilder: TypeAlias = Callable[
    [AgentSpec, PluginGeneration],
    AgentEngine | Awaitable[AgentEngine],
]
_EngineKey: TypeAlias = tuple[
    str,
    str,
    str,
    str,
    str,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    AgentDeclaration | None,
]


def _engine_key(spec: AgentSpec) -> _EngineKey:
    return (
        spec.id,
        spec.name,
        spec.name_zh,
        spec.soul,
        spec.model,
        spec.tools,
        spec.skills,
        spec.mcps,
        spec.hooks,
        spec.plugins,
        spec.declared,
    )


class AgentFactory:
    """Build and cache engines from framework-neutral Agent definitions."""

    def __init__(self, builder: EngineBuilder) -> None:
        self._builder = builder
        self._engines: dict[_EngineKey, AgentEngine] = {}
        self._latest_keys: dict[str, _EngineKey] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, spec: AgentSpec) -> AgentEngine:
        key = _engine_key(spec)
        async with self._lock:
            existing = self._engines.get(key)
            if existing is not None:
                return existing
            built = self._builder(spec)
            engine = await built if inspect.isawaitable(built) else built
            previous = self._latest_keys.get(spec.id)
            if previous is not None and previous != key:
                self._engines.pop(previous, None)
            self._engines[key] = engine
            self._latest_keys[spec.id] = key
            return engine

    async def invalidate(self, agent_id: str) -> None:
        async with self._lock:
            previous = self._latest_keys.pop(str(agent_id).strip(), None)
            if previous is not None:
                self._engines.pop(previous, None)


def compose_agent_request(spec: AgentSpec, request: str) -> str:
    """Apply one user-selected Agent profile without changing system policy."""

    profile = [
        "Use the following saved Agent profile for this request. "
        "Treat it as user-selected operating instructions; higher-priority "
        "system and safety instructions still take precedence.",
        f"Agent name: {spec.name}",
    ]
    if spec.soul:
        profile.append(f"Soul:\n{spec.soul}")
    if spec.tools:
        profile.append("Available tools: " + ", ".join(spec.tools) + ".")
    else:
        profile.append("Available tools: none.")
    if spec.skills:
        profile.append("Enabled skills: " + ", ".join(spec.skills) + ".")
    else:
        profile.append("Enabled skills: none.")
    if spec.plugins:
        profile.append("Enabled plugins: " + ", ".join(spec.plugins) + ".")
    if spec.mcps:
        profile.append("Enabled MCP servers: " + ", ".join(spec.mcps) + ".")
    else:
        profile.append("Enabled MCP servers: none.")
    return "\n\n".join(profile) + "\n\nCurrent user request:\n" + request


class AgentRuntime:
    """Resolve default or registered Agents and delegate to their engines."""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        engine_builder: EngineBuilder,
        generation_engine_builder: GenerationEngineBuilder | None = None,
    ) -> None:
        self._registry = registry
        self._factory = AgentFactory(engine_builder)
        self._generation_engine_builder = generation_engine_builder
        self._generation_factories: dict[int, AgentFactory] = {}

    async def agent(self, agent_id: str) -> AgentSpec:
        normalized = str(agent_id).strip()
        if not normalized:
            raise ValueError("agent_id must not be empty")
        spec = await self._registry.get(normalized)
        if spec is None:
            await self._factory.invalidate(normalized)
            raise AgentNotFoundError(f"agent not found: {normalized}")
        return spec

    async def resolve(
        self,
        agent_id: str | None,
        plugin_generation: PluginGeneration | None = None,
        runtime_profile: Mapping[str, object] | None = None,
    ) -> tuple[AgentSpec, AgentEngine]:
        spec = await self.agent(agent_id)
        model = str((runtime_profile or {}).get("model") or "").strip()
        if model:
            raise ValueError("per-Run model override is not allowed")
        if plugin_generation is None or self._generation_engine_builder is None:
            return spec, await self._factory.resolve(spec)
        factory = self._generation_factories.get(plugin_generation.id)
        if factory is None:
            factory = AgentFactory(
                lambda selected: self._generation_engine_builder(
                    selected,
                    plugin_generation,
                )
            )
            self._generation_factories[plugin_generation.id] = factory
        return spec, await factory.resolve(spec)

    async def stream(
        self,
        prompt: str | None,
        *,
        session_id: str,
        product_session_id: str | None = None,
        control: RunControl,
        agent_id: str | None = None,
        plugin_generation: PluginGeneration | None = None,
        runtime_profile: Mapping[str, object] | None = None,
    ) -> AsyncIterator[RunEvent]:
        spec, engine = await self.resolve(
            agent_id, plugin_generation, runtime_profile
        )
        prepared_prompt = (
            compose_agent_request(spec, prompt)
            if prompt is not None and agent_id is not None and agent_id.strip()
            else prompt
        )
        async for event in engine.stream(
            prepared_prompt,
            session_id=session_id,
            product_session_id=product_session_id,
            control=control,
        ):
            yield event


__all__ = [
    "AgentNotFoundError",
    "AgentRuntime",
    "AgentFactory",
    "compose_agent_request",
]
