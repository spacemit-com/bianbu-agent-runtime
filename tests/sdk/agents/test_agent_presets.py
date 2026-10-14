"""Preset capabilities come from ``config.yaml`` and are forced onto every Agent."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from bianbu_agent import (
    AgentManager,
    AgentSpec,
    InMemoryAgentRegistry,
    ModelRegistry,
    ToolContribution,
    ToolSpec,
)
from bianbu_agent.config import RuntimePresetsConfig


@dataclass
class FakeModel:
    model: str


@dataclass
class Generation:
    tools: dict = field(default_factory=dict)
    skills: dict = field(default_factory=dict)
    mcps: dict = field(default_factory=dict)
    mcp_tools: dict = field(default_factory=dict)
    hooks: dict = field(default_factory=dict)
    plugins: dict = field(default_factory=dict)


async def _handler(arguments):
    return arguments


def _tool(name: str, *, owner: str | None = None) -> ToolContribution:
    metadata = {} if owner is None else {"owner_agent_id": owner}
    return ToolContribution(
        plugin_id="catalog.test",
        spec=ToolSpec(
            name=name,
            description=name,
            input_schema={"type": "object"},
            metadata=metadata,
        ),
        handler=_handler,
    )


def _generation() -> Generation:
    return Generation(
        tools={
            "read_file": _tool("read_file"),
            "terminal": _tool("terminal"),
            "write_file": _tool("write_file"),
        }
    )


def _manager(
    generation: Generation,
    presets: RuntimePresetsConfig,
) -> AgentManager:
    return AgentManager(
        store=InMemoryAgentRegistry(),
        model_registry=ModelRegistry(
            default_model=FakeModel("default"),
            models={"default": FakeModel("default")},
        ),
        generation_provider=lambda: generation,
        presets=presets,
    )


@pytest.mark.asyncio
async def test_preset_tools_are_materialized_without_an_agent_declaration() -> None:
    generation = _generation()
    presets = RuntimePresetsConfig(tools=("read_file", "terminal", "absent_tool"))
    manager = _manager(generation, presets)
    spec = await manager.create(AgentSpec(id="narrow", name="Narrow", model="default"))

    resolved = await manager.materialize(spec.id, generation)

    # A Tool this device does not ship is skipped instead of materialized.
    assert resolved.tools == ("read_file", "terminal")
    # The stored declaration stays what the package wrote; the preset is a
    # Runtime view, exactly like the APM handbook Skill.
    assert (await manager.get("narrow")).tools == ()
    assert [item.name for item in await manager.list_tools("narrow")] == [
        "read_file",
        "terminal",
    ]


@pytest.mark.asyncio
async def test_preset_tools_lead_a_selection_and_never_repeat() -> None:
    generation = _generation()
    presets = RuntimePresetsConfig(tools=("read_file", "terminal"))
    manager = _manager(generation, presets)
    spec = await manager.create(
        AgentSpec(
            id="editor",
            name="Editor",
            model="default",
            tools=("write_file", "read_file"),
        )
    )

    resolved = await manager.materialize(spec.id, generation)

    assert resolved.tools == ("read_file", "terminal", "write_file")


@pytest.mark.asyncio
async def test_an_agent_cannot_drop_a_preset_tool() -> None:
    generation = _generation()
    presets = RuntimePresetsConfig(tools=("read_file", "terminal"))
    manager = _manager(generation, presets)
    spec = await manager.create(
        AgentSpec(id="slim", name="Slim", model="default", tools=("write_file",))
    )

    resolved = await manager.materialize(spec.id, generation)

    assert set(resolved.tools) == {"write_file", "read_file", "terminal"}


@pytest.mark.asyncio
async def test_presets_come_from_configuration_only() -> None:
    generation = _generation()
    manager = _manager(generation, RuntimePresetsConfig())
    spec = await manager.create(AgentSpec(id="plain", name="Plain", model="default"))

    resolved = await manager.materialize(spec.id, generation)

    assert resolved.tools == ()
    assert resolved.skills == ()


@pytest.mark.asyncio
async def test_preset_tools_skip_another_agents_owned_tool() -> None:
    generation = Generation(tools={"read_file": _tool("read_file", owner="other")})
    presets = RuntimePresetsConfig(tools=("read_file",))
    manager = _manager(generation, presets)
    spec = await manager.create(AgentSpec(id="narrow", name="Narrow", model="default"))

    resolved = await manager.materialize(spec.id, generation)

    assert resolved.tools == ()
