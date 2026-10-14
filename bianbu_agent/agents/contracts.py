"""Agent definitions, registries, and file-backed persistence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from collections.abc import Iterable, Mapping


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _capability_names(values: object) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError("capabilities must be a sequence of names")
    try:
        candidates = iter(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("capabilities must be a sequence of names") from exc
    return tuple(
        dict.fromkeys(
            normalized
            for value in candidates
            if (normalized := str(value).strip())
        )
    )


@dataclass(frozen=True, slots=True)
class AgentModel:
    """Provider and remote model selected by one Agent."""

    provider: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider",
            _required_text(self.provider, "model.provider"),
        )
        object.__setattr__(self, "name", _required_text(self.name, "model.name"))


def agent_model_data(model: AgentModel | str | None) -> object:
    """Return the JSON/YAML-safe representation of an Agent model selection."""

    if isinstance(model, AgentModel):
        return {"provider": model.provider, "name": model.name}
    return None


def parse_agent_model(value: object) -> AgentModel | str | None:
    """Parse structured selections while retaining direct SDK string aliases."""

    if isinstance(value, Mapping):
        if set(value) != {"provider", "name"}:
            raise ValueError("model must contain provider and name")
        return AgentModel(provider=value["provider"], name=value["name"])
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    raise ValueError("model must be a provider/name mapping")


@dataclass(frozen=True, slots=True)
class AgentDeclaration:
    """The Power selectors one Agent declared in its own manifest.

    ``AgentManager.materialize`` replaces an AgentSpec's selectors with the
    names the current Generation can actually satisfy.  That erases the
    difference between "declared nothing" and "declared but not installed",
    which is exactly the difference an expert needs to see when it has to
    provision a Power before it can answer.
    """

    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mcps: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("tools", "skills", "mcps", "plugins"):
            object.__setattr__(
                self,
                field_name,
                _capability_names(getattr(self, field_name)),
            )

    @classmethod
    def from_spec(cls, spec: "AgentSpec") -> "AgentDeclaration":
        return cls(
            tools=tuple(spec.tools),
            skills=tuple(spec.skills),
            mcps=tuple(spec.mcps),
            plugins=tuple(spec.plugins),
        )


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Stable, framework-neutral definition of one runnable Agent."""

    id: str
    name: str
    name_zh: str = ""
    name_en: str = ""
    description: str = ""
    description_zh: str = ""
    soul: str = ""
    model: AgentModel | str | None = None
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    mcps: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    hooks: tuple[str, ...] = ()
    permissions: Mapping[str, object] = field(default_factory=dict)
    plugins: tuple[str, ...] = ()
    # Runtime-only view of what the manifest declared.  ``materialize`` fills it
    # in so prompt building can report declared-but-missing Powers; it is never
    # persisted, because the stored spec *is* the declaration.
    declared: AgentDeclaration | None = None
    # Runtime-only view of the Skills the user attached to one message.  They
    # also appear in ``skills`` so the run lease, ``skill_view`` and the tool
    # scope resolve them, but prompt building has to state that they were
    # explicitly requested instead of listing them as ambient Agent Skills.
    requested_skills: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _required_text(self.id, "id"))
        object.__setattr__(self, "name", _required_text(self.name, "name"))
        for field_name in ("name_zh", "name_en", "description", "description_zh", "soul"):
            object.__setattr__(
                self,
                field_name,
                str(getattr(self, field_name)).strip(),
            )
        model = (
            self.model
            if isinstance(self.model, AgentModel)
            else parse_agent_model(self.model)
        )
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "tools", _capability_names(self.tools))
        object.__setattr__(self, "skills", _capability_names(self.skills))
        object.__setattr__(self, "mcps", _capability_names(self.mcps))
        object.__setattr__(self, "hooks", _capability_names(self.hooks))
        object.__setattr__(self, "plugins", _capability_names(self.plugins))
        object.__setattr__(
            self, "requested_skills", _capability_names(self.requested_skills)
        )
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(dict(self.metadata)),
        )
        object.__setattr__(
            self,
            "permissions",
            MappingProxyType(dict(self.permissions)),
        )
        if self.declared is not None and not isinstance(self.declared, AgentDeclaration):
            raise ValueError("declared must be an AgentDeclaration")


@runtime_checkable
class AgentRegistry(Protocol):
    async def get(self, agent_id: str) -> AgentSpec | None: ...

    async def list(self) -> list[AgentSpec]: ...

    async def create(self, spec: AgentSpec) -> AgentSpec: ...

    async def update(self, spec: AgentSpec) -> AgentSpec: ...

    async def delete(self, agent_id: str) -> bool: ...


class InMemoryAgentRegistry:
    """Concurrency-safe registry for embedding, tests, and small hosts."""

    def __init__(self, agents: Iterable[AgentSpec] = ()) -> None:
        selected = tuple(agents)
        if len({agent.id for agent in selected}) != len(selected):
            raise ValueError("Agent IDs must be unique")
        self._agents: dict[str, AgentSpec] = {
            agent.id: agent for agent in selected
        }
        self._lock = asyncio.Lock()

    async def get(self, agent_id: str) -> AgentSpec | None:
        async with self._lock:
            return self._agents.get(str(agent_id).strip())

    async def list(self) -> list[AgentSpec]:
        async with self._lock:
            return list(self._agents.values())

    async def create(self, spec: AgentSpec) -> AgentSpec:
        async with self._lock:
            if spec.id in self._agents:
                raise ValueError(f"agent already exists: {spec.id}")
            self._agents[spec.id] = spec
            return spec

    async def update(self, spec: AgentSpec) -> AgentSpec:
        async with self._lock:
            if spec.id not in self._agents:
                raise KeyError(spec.id)
            self._agents[spec.id] = spec
            return spec

    async def delete(self, agent_id: str) -> bool:
        normalized = str(agent_id).strip()
        async with self._lock:
            return self._agents.pop(normalized, None) is not None


__all__ = [
    "AgentModel",
    "AgentRegistry",
    "AgentSpec",
    "agent_model_data",
    "parse_agent_model",
    "InMemoryAgentRegistry",
]
