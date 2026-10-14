from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bianbu_agent import (
    AgentManager,
    AgentSpec,
    AgentValidationError,
    InMemoryAgentRegistry,
    ModelRegistry,
    ToolContribution,
    ToolSpec,
)
from bianbu_agent.plugins import (
    HookContribution,
    HookPoint,
    SkillContribution,
    SkillSpec,
)
from bianbu_agent.agents.stores.sqlite import SQLiteAgentStore


@dataclass
class FakeModel:
    model: str


@dataclass
class Generation:
    tools: dict
    skills: dict
    mcps: dict
    mcp_tools: dict
    hooks: dict = field(default_factory=dict)
    plugins: dict = field(default_factory=dict)


async def _handler(arguments):
    return arguments


@pytest.mark.asyncio
async def test_sqlite_agent_store_crud_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "agents.sqlite3"
    store = SQLiteAgentStore(path)
    await store.open()
    spec = AgentSpec(
        id="reviewer",
        name="Reviewer",
        permissions={"level": "L3"},
        metadata={"z": 1, "a": 2},
    )
    await store.create(spec)
    await store.close()

    reopened = SQLiteAgentStore(path)
    await reopened.open()
    assert await reopened.get("reviewer") == spec
    assert [item.id for item in await reopened.list()] == ["reviewer"]
    updated = AgentSpec(id="reviewer", name="Senior Reviewer")
    assert await reopened.update(updated) == updated
    assert await reopened.delete("reviewer") is True
    assert await reopened.delete("reviewer") is False
    await reopened.close()


@pytest.mark.asyncio
async def test_agent_manager_validates_and_lists_current_effective_capabilities(
    tmp_path: Path,
) -> None:
    store = SQLiteAgentStore(tmp_path / "agents.sqlite3")
    await store.open()
    generation = Generation(
        tools={
            "clock": ToolContribution(
                plugin_id="catalog.test",
                spec=ToolSpec(
                    name="clock",
                    description="Clock",
                    input_schema={"type": "object"},
                ),
                handler=_handler,
            ),
            "echo": ToolContribution(
                plugin_id="catalog.test",
                spec=ToolSpec(
                    name="echo",
                    description="Echo",
                    input_schema={"type": "object"},
                    metadata={"version": "1.0.0"},
                ),
                handler=_handler,
            ),
            "mcp.weather/forecast": ToolContribution(
                plugin_id="mcp.runtime-test",
                spec=ToolSpec(
                    name="forecast",
                    description="Weather forecast",
                    input_schema={"type": "object"},
                    metadata={
                        "origin": "mcp",
                        "mcp_id": "mcp.weather",
                        "qualified_id": "mcp.weather/forecast",
                    },
                ),
                handler=_handler,
            ),
            "mcp.filesystem/read": ToolContribution(
                plugin_id="mcp.runtime-test",
                spec=ToolSpec(
                    name="read",
                    description="Read a file",
                    input_schema={"type": "object"},
                    metadata={
                        "origin": "mcp",
                        "mcp_id": "mcp.filesystem",
                        "qualified_id": "mcp.filesystem/read",
                    },
                ),
                handler=_handler,
            ),
        },
        skills={
            "review": SkillContribution(
                plugin_id="catalog.test",
                spec=SkillSpec(name="review", description="Review", instructions="Do it"),
            )
        },
        mcps={
            "mcp.weather": object(),
            "mcp.filesystem": object(),
            "mcp.empty": object(),
        },
        mcp_tools={
            "mcp.weather": ("mcp.weather/forecast",),
            "mcp.filesystem": ("mcp.filesystem/read",),
        },
    )
    starter = AgentSpec(
        id="com.bianbu.0123456789ab",
        name="Starter",
        tools=("*",),
        skills=("*",),
    )
    await store.create(starter)
    manager = AgentManager(
        store=store,
        model_registry=ModelRegistry(
            default_model=FakeModel("default"),
            models={"fast": FakeModel("fast")},
        ),
        generation_provider=lambda: generation,
    )

    assert await manager.get(starter.id) == starter
    assert await store.get(starter.id) == starter
    resolved = await manager.materialize(starter.id, generation)
    assert resolved.tools == ("clock", "echo")
    assert resolved.skills == ("review",)
    assert (await manager.get(starter.id)).tools == ("*",)
    assert [item.name for item in await manager.list_tools(starter.id)] == [
        "clock",
        "echo",
    ]
    created = await manager.create(
        AgentSpec(
            id="reviewer",
            name="Reviewer",
            model="fast",
            tools=("echo",),
            skills=("review",),
        )
    )
    assert created.id == "reviewer"
    assert [item.name for item in await manager.list_tools("reviewer")] == ["echo"]
    assert [item.name for item in await manager.list_skills("reviewer")] == ["review"]

    weather = await manager.create(
        AgentSpec(
            id="weather",
            name="Weather",
            tools=("clock",),
            mcps=("mcp.weather",),
        )
    )
    assert weather.mcps == ("mcp.weather",)
    materialized_weather = await manager.materialize("weather", generation)
    assert materialized_weather.tools == ("clock", "mcp.weather/forecast")
    assert materialized_weather.mcps == ("mcp.weather",)
    assert [item.name for item in await manager.list_tools("weather")] == [
        "clock",
        "forecast",
    ]

    all_mcps = await manager.create(
        AgentSpec(id="all-mcps", name="All MCPs", mcps=("*",))
    )
    materialized_all_mcps = await manager.materialize(all_mcps.id, generation)
    assert materialized_all_mcps.mcps == (
        "mcp.empty",
        "mcp.filesystem",
        "mcp.weather",
    )
    assert materialized_all_mcps.tools == (
        "mcp.filesystem/read",
        "mcp.weather/forecast",
    )
    assert [item.name for item in await manager.list_tools(all_mcps.id)] == [
        "read",
        "forecast",
    ]
    generation.mcps.clear()
    generation.mcp_tools.clear()
    materialized_empty_mcps = await manager.materialize(all_mcps.id, generation)
    assert materialized_empty_mcps.mcps == ()
    assert materialized_empty_mcps.tools == ()

    with pytest.raises(AgentValidationError, match="select the MCP server"):
        await manager.create(
            AgentSpec(
                id="legacy",
                name="Legacy",
                tools=("mcp.weather/forecast",),
            )
        )
    with pytest.raises(AgentValidationError, match="missing MCP servers: mcp.missing"):
        await manager.create(
            AgentSpec(id="missing-mcp", name="Missing", mcps=("mcp.missing",))
        )

    generation.tools.clear()
    with pytest.raises(AgentValidationError, match="missing tools: echo"):
        await manager.list_tools("reviewer")
    with pytest.raises(AgentValidationError, match="missing"):
        await manager.create(AgentSpec(id="broken", name="Broken", tools=("missing",)))
    with pytest.raises(AgentValidationError, match="model"):
        await manager.create(AgentSpec(id="model", name="Model", model="missing"))
    with pytest.raises(AgentValidationError, match="wildcard"):
        await manager.create(
            AgentSpec(id="mixed", name="Mixed", tools=("*", "clock"))
        )
    with pytest.raises(AgentValidationError, match="wildcard"):
        await manager.create(
            AgentSpec(
                id="mixed-mcps",
                name="Mixed MCPs",
                mcps=("*", "mcp.weather"),
            )
        )
    updated_starter = await manager.update(
        AgentSpec(id=starter.id, name="Changed")
    )
    assert updated_starter.name == "Changed"
    with pytest.raises(ValueError, match="agent_id"):
        await manager.get("")
    assert await manager.delete(starter.id) is True
    await store.close()


@pytest.mark.asyncio
async def test_agent_manager_reports_missing_explicit_agent() -> None:
    generation = Generation(tools={}, skills={}, mcps={}, mcp_tools={})
    manager = AgentManager(
        store=InMemoryAgentRegistry(),
        model_registry=ModelRegistry(default_model=FakeModel("default")),
        generation_provider=lambda: generation,
    )

    with pytest.raises(KeyError, match="missing"):
        await manager.get("missing")


@pytest.mark.asyncio
async def test_agent_manager_derives_hooks_from_selected_plugins(tmp_path: Path) -> None:
    """Hooks are Plugin members: selecting a Plugin activates the hooks it owns."""

    store = SQLiteAgentStore(tmp_path / "agents.sqlite3")
    await store.open()
    generation = Generation(
        tools={},
        skills={},
        mcps={},
        mcp_tools={},
        plugins={
            "system.owner": object(),
            "bianbu.catalog.filesystem": object(),
        },
        hooks={
            "platform.context": HookContribution(
                plugin_id="system.owner",
                point=HookPoint.BEFORE_RUN,
                handler=_handler,
                id="platform.context",
            ),
            "bianbu.catalog.filesystem/greeting.direct-reply": HookContribution(
                plugin_id="bianbu.catalog.filesystem",
                point=HookPoint.BEFORE_RUN,
                handler=_handler,
                id="greeting.direct-reply",
                metadata={
                    "qualified_id": "bianbu.catalog.filesystem/greeting.direct-reply"
                },
            ),
        },
    )
    manager = AgentManager(
        store=store,
        model_registry=ModelRegistry(default_model=FakeModel("default")),
        generation_provider=lambda: generation,
    )

    explicit = AgentSpec(id="explicit", name="Explicit", plugins=("system.owner",))
    manager.validate(explicit, generation=generation)
    await store.create(explicit)
    materialized = await manager.materialize("explicit", generation)
    assert materialized.hooks == ("platform.context",)

    wildcard = AgentSpec(id="wildcard", name="Wildcard", plugins=("*",))
    await store.create(wildcard)
    materialized_wildcard = await manager.materialize("wildcard", generation)
    assert materialized_wildcard.hooks == (
        "bianbu.catalog.filesystem/greeting.direct-reply",
        "platform.context",
    )

    plain = AgentSpec(id="plain", name="Plain")
    await store.create(plain)
    assert (await manager.materialize("plain", generation)).hooks == ()

    with pytest.raises(AgentValidationError, match="missing plugins: missing.plugin"):
        manager.validate(
            AgentSpec(id="unknown", name="Unknown", plugins=("missing.plugin",)),
            generation=generation,
        )
    await store.close()



@pytest.mark.asyncio
async def test_agent_manager_enables_the_plugin_that_owns_a_declared_member() -> None:
    """声明插件成员就不能把成员静默丢掉：它的插件必须一起被选中。

    插件成员的暴露由 ``_select_members`` 按 ``plugins`` 决定，所以只写
    ``skills: [spacemit-power/k3-media-pipeline]`` 的专家曾经既看不到这个 Skill，
    也不会被报成缺失——正是「创建专家时把插件的东西写进 skills，结果没识别出来」。
    """

    generation = Generation(
        tools={},
        skills={
            "spacemit-power/k3-media-pipeline": SkillContribution(
                plugin_id="spacemit-power",
                spec=SkillSpec(
                    name="k3-media-pipeline",
                    display_name="K3 Media Pipeline",
                    description="Media",
                    instructions="Run it",
                ),
            ),
        },
        mcps={},
        mcp_tools={},
        plugins={"spacemit-power": object()},
    )
    manager = AgentManager(
        store=InMemoryAgentRegistry(),
        model_registry=ModelRegistry(default_model=FakeModel("default")),
        generation_provider=lambda: generation,
    )
    expert = await manager.create(
        AgentSpec(
            id="media",
            name="Media",
            skills=("spacemit-power/k3-media-pipeline",),
        )
    )

    # 声明原样保存：用户点名的成员不会被改写成别的选择器。
    assert (await manager.get(expert.id)).skills == ("spacemit-power/k3-media-pipeline",)

    resolved = await manager.materialize(expert.id, generation)
    assert resolved.skills == ("spacemit-power/k3-media-pipeline",)
    assert resolved.plugins == ("spacemit-power",)
    assert [item.name for item in await manager.list_skills(expert.id)] == [
        "k3-media-pipeline"
    ]
