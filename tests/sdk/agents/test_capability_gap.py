"""Declared Powers a device has not installed yet must not block an expert."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from bianbu_agent import (
    AgentDeclaration,
    AgentManager,
    AgentSpec,
    AgentValidationError,
    InMemoryAgentRegistry,
    ModelRegistry,
)
from bianbu_agent.config import RuntimePresetsConfig
from bianbu_agent.engines.default import _power_dependencies_section
from bianbu_agent.plugins import SkillContribution, SkillSpec


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


def _skill(name: str) -> SkillContribution:
    return SkillContribution(
        plugin_id="catalog.test",
        spec=SkillSpec(name=name, description=f"{name} skill", instructions="Do it"),
    )


def _generation(*, presets: bool = True) -> Generation:
    skills = {"review": _skill("review")}
    if presets:
        skills["using-apm"] = _skill("using-apm")
    return Generation(
        skills=skills,
        mcps={"mcp.weather": object()},
        plugins={"vendor.bundle": object()},
    )


def _manager() -> AgentManager:
    return AgentManager(
        store=InMemoryAgentRegistry(),
        model_registry=ModelRegistry(
            default_model=FakeModel("default"),
            models={"default": FakeModel("default")},
        ),
        generation_provider=lambda: _generation(),
        # The shipped profile declares this preset; the values live in
        # config.yaml, so the test states the profile it exercises.
        presets=RuntimePresetsConfig(skills=("using-apm",)),
    )


def _declared_expert() -> AgentSpec:
    return AgentSpec(
        id="hub-expert",
        name="Hub Expert",
        model="default",
        skills=("review", "translation"),
        mcps=("mcp.weather", "mcp.finance"),
        plugins=("vendor.bundle", "vendor.missing"),
    )


@pytest.mark.asyncio
async def test_declared_but_uninstalled_powers_fail_closed_by_default() -> None:
    manager = _manager()
    generation = _generation()
    spec = await manager.create(
        _declared_expert(), generation=generation, allow_capability_gap=True
    )

    with pytest.raises(AgentValidationError) as error:
        await manager.materialize(spec.id, generation)

    message = str(error.value)
    assert "missing plugins: vendor.missing" in message
    assert "missing skills: translation" in message
    assert "missing MCP servers: mcp.finance" in message


@pytest.mark.asyncio
async def test_capability_gap_lets_an_installed_expert_start() -> None:
    manager = _manager()
    generation = _generation()
    spec = await manager.create(
        _declared_expert(), generation=generation, allow_capability_gap=True
    )

    resolved = await manager.materialize(
        spec.id, generation, allow_capability_gap=True
    )

    assert resolved.skills == ("using-apm", "review")
    assert resolved.mcps == ("mcp.weather",)
    assert resolved.plugins == ("vendor.bundle", "vendor.missing")
    # The declaration survives materialization, so the prompt can name the gap.
    assert resolved.declared == AgentDeclaration(
        skills=("review", "translation"),
        mcps=("mcp.weather", "mcp.finance"),
        plugins=("vendor.bundle", "vendor.missing"),
    )


@pytest.mark.asyncio
async def test_missing_tools_stay_fatal_with_a_capability_gap() -> None:
    manager = _manager()
    generation = _generation()
    with pytest.raises(AgentValidationError, match="missing tools"):
        await manager.create(
            AgentSpec(
                id="hub-expert",
                name="Hub Expert",
                model="default",
                tools=("catalog.test/greet",),
                skills=("translation",),
            ),
            generation=generation,
            allow_capability_gap=True,
        )


@pytest.mark.asyncio
async def test_every_expert_receives_the_apm_handbook() -> None:
    manager = _manager()
    generation = _generation()
    spec = await manager.create(
        AgentSpec(id="narrow", name="Narrow", model="default")
    )

    resolved = await manager.materialize(spec.id, generation)

    assert resolved.skills == ("using-apm",)


@pytest.mark.asyncio
async def test_apm_handbook_is_skipped_when_the_device_lacks_it() -> None:
    manager = _manager()
    generation = _generation(presets=False)
    spec = await manager.create(
        AgentSpec(id="reviewer", name="Reviewer", model="default", skills=("review",))
    )

    resolved = await manager.materialize(spec.id, generation)

    assert resolved.skills == ("review",)


@pytest.mark.asyncio
async def test_agent_creation_records_gaps_instead_of_refusing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", "1")
    manager = _manager()
    generation = _generation()

    stored = await manager.create(
        _declared_expert(),
        generation=generation,
        allow_capability_gap=True,
    )
    resolved = await manager.materialize(
        stored.id, generation, allow_capability_gap=True
    )

    section = _power_dependencies_section(resolved, generation)
    assert "## Power Dependencies" in section
    assert "Execution mode: sandbox" in section
    assert "Skills: declared [review, translation]; available [using-apm, review]; missing [translation]" in section
    assert (
        "MCP servers: declared [mcp.weather, mcp.finance]; available [mcp.weather]; "
        "missing [mcp.finance]" in section
    )
    assert (
        "Plugins: declared [vendor.bundle, vendor.missing]; available [vendor.bundle]; "
        "missing [vendor.missing]" in section
    )
    # The sandbox cannot run `apm` at all (read-only data root, no writable
    # HOME), so the section asks for the gap instead of the host recipe.
    assert "using-apm Skill's self-provisioning flow" not in section
    assert "runs in a sandbox" in section
    assert "read-only" in section


@pytest.mark.asyncio
async def test_host_workers_get_the_apm_self_provisioning_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", "0")
    manager = _manager()
    generation = _generation()

    stored = await manager.create(
        _declared_expert(),
        generation=generation,
        allow_capability_gap=True,
    )
    resolved = await manager.materialize(
        stored.id, generation, allow_capability_gap=True
    )

    section = _power_dependencies_section(resolved, generation)
    assert "Execution mode: host" in section
    assert "using-apm Skill's self-provisioning flow" in section
    assert "runs in a sandbox" not in section


@pytest.mark.asyncio
async def test_power_dependencies_reports_execution_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANBU_SANDBOX_ACTIVE", "0")
    spec = AgentSpec(
        id="host-expert",
        name="Host Expert",
        metadata={"execution": {"requested_mode": "host"}},
    )

    section = _power_dependencies_section(spec)

    assert "Execution mode: host (requested: host)" in section


def test_tool_gap_is_reported_without_repeating_tool_policy() -> None:
    generation = _generation()
    wildcard = AgentSpec(
        id="wildcard",
        name="Wildcard",
        tools=("terminal",),
        declared=AgentDeclaration.from_spec(
            AgentSpec(id="wildcard", name="Wildcard", tools=("*",))
        ),
    )

    # Tool Policy already carries the exposed names, so a satisfied wildcard
    # must not repeat the whole list here.
    assert _tool_line(wildcard, generation) == "- Tools: declared [*]"

    declared = AgentDeclaration.from_spec(
        AgentSpec(id="wildcard", name="Wildcard", tools=("terminal", "read_file"))
    )
    narrowed = replace(wildcard, tools=("terminal",), declared=declared)

    assert _tool_line(narrowed, generation) == (
        "- Tools: declared [terminal, read_file]; missing [read_file]"
    )


def _tool_line(agent: AgentSpec, generation: Generation) -> str:
    section = _power_dependencies_section(agent, generation)
    return next(line for line in section.splitlines() if line.startswith("- Tools:"))
