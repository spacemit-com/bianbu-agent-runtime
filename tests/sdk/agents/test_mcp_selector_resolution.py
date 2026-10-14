"""An Agent may name an MCP by its APM name, this device installs ``mcp.<name>``."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from bianbu_agent import (
    AgentDeclaration,
    AgentManager,
    AgentSpec,
    InMemoryAgentRegistry,
    ModelRegistry,
    ToolContribution,
    ToolSpec,
)
from bianbu_agent.api.agents import _capability_state, agent_view
from bianbu_agent.engines.default import _power_dependencies_section
from bianbu_agent.mcp import resolve_mcp_selectors, selects_mcp
from bianbu_agent.server.runtime import _agent_selects_mcp


@dataclass
class FakeModel:
    model: str


@dataclass
class FakeMCP:
    owner_plugin_id: str | None = None


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


def _generation() -> Generation:
    return Generation(
        tools={
            "mcp.nws/nws_get_forecast": ToolContribution(
                plugin_id="mcp.runtime-nws",
                spec=ToolSpec(
                    name="nws_get_forecast",
                    description="Forecast",
                    input_schema={"type": "object"},
                    metadata={
                        "origin": "mcp",
                        "mcp_id": "mcp.nws",
                        "qualified_id": "mcp.nws/nws_get_forecast",
                    },
                ),
                handler=_handler,
            )
        },
        mcps={"mcp.nws": FakeMCP()},
        mcp_tools={"mcp.nws": ("mcp.nws/nws_get_forecast",)},
    )


def _manager(generation: Generation) -> AgentManager:
    return AgentManager(
        store=InMemoryAgentRegistry(),
        model_registry=ModelRegistry(
            default_model=FakeModel("default"),
            models={"default": FakeModel("default")},
        ),
        generation_provider=lambda: generation,
    )


def test_apm_mcp_name_resolves_to_the_installed_runtime_id() -> None:
    assert resolve_mcp_selectors(("nws",), {"mcp.nws": object()}) == ("mcp.nws",)
    assert resolve_mcp_selectors(("mcp.nws",), {"mcp.nws": object()}) == ("mcp.nws",)
    assert resolve_mcp_selectors(("other",), {"mcp.nws": object()}) == ("other",)
    # A wildcard keeps meaning "everything installed", never a snapshot of it.
    assert resolve_mcp_selectors(("*",), {"mcp.nws": object()}) == ("*",)
    assert resolve_mcp_selectors(("*",), {}) == ("*",)


def test_selects_mcp_matches_both_names_and_the_wildcard() -> None:
    assert selects_mcp(("nws",), "mcp.nws") is True
    assert selects_mcp(("mcp.nws",), "mcp.nws") is True
    assert selects_mcp(("*",), "mcp.nws") is True
    assert selects_mcp(("other",), "mcp.nws") is False
    assert selects_mcp((), "mcp.nws") is False


def test_a_stored_agent_authorizes_the_server_it_declared_by_apm_name() -> None:
    assert _agent_selects_mcp(AgentSpec(id="nws", name="NWS", mcps=("nws",)), "mcp.nws")
    assert _agent_selects_mcp(
        AgentSpec(id="nws", name="NWS", mcps=("mcp.nws",)), "mcp.nws"
    )
    assert _agent_selects_mcp(AgentSpec(id="all", name="All", mcps=("*",)), "mcp.nws")
    assert not _agent_selects_mcp(AgentSpec(id="nws", name="NWS", mcps=()), "mcp.nws")
    assert not _agent_selects_mcp(
        AgentSpec(id="nws", name="NWS", mcps=("other",)), "mcp.nws"
    )


@pytest.mark.asyncio
async def test_a_declared_apm_name_selects_the_installed_server_and_its_tools() -> None:
    generation = _generation()
    manager = _manager(generation)
    stored = await manager.create(
        AgentSpec(id="nws", name="NWS", model="default", mcps=("nws",)),
        generation=generation,
    )

    resolved = await manager.materialize(stored.id, generation)

    assert resolved.mcps == ("mcp.nws",)
    assert resolved.tools == ("mcp.nws/nws_get_forecast",)
    # The declaration itself is untouched, exactly like the preset Tools.
    assert (await manager.get("nws")).mcps == ("nws",)


@pytest.mark.asyncio
async def test_a_still_missing_mcp_keeps_the_gap() -> None:
    generation = _generation()
    manager = _manager(generation)
    stored = await manager.create(
        AgentSpec(id="nws", name="NWS", model="default", mcps=("absent",)),
        generation=generation,
        allow_capability_gap=True,
    )

    resolved = await manager.materialize(stored.id, generation, allow_capability_gap=True)

    assert resolved.mcps == ()


def test_capability_state_and_view_report_the_installed_server() -> None:
    generation = _generation()
    spec = AgentSpec(id="nws", name="NWS", mcps=("nws",))

    missing, tools_missing = _capability_state(spec, generation)
    view = agent_view(spec, providers={}, generation=generation)

    assert missing == []
    assert tools_missing is False
    assert view.mcps == ["mcp.nws"]


def test_the_prompt_reports_no_false_gap_for_a_resolved_server() -> None:
    generation = _generation()
    spec = AgentSpec(
        id="nws",
        name="NWS",
        mcps=("mcp.nws",),
        declared=AgentDeclaration(mcps=("nws",)),
    )

    section = _power_dependencies_section(spec, generation)

    assert "MCP servers: declared [mcp.nws]; available [mcp.nws]" in section
    assert "missing" not in section


def test_a_wildcard_survives_resolution_in_the_api_view() -> None:
    generation = _generation()
    spec = AgentSpec(id="all", name="All", mcps=("*",))

    missing, tools_missing = _capability_state(spec, generation)
    view = agent_view(spec, providers={}, generation=generation)

    assert missing == []
    assert tools_missing is False
    # The editor saves what the API hands it, so a wildcard must not be
    # rewritten into the servers installed on this device today.
    assert view.mcps == ["*"]


def test_install_and_selection_agree_on_the_runtime_id() -> None:
    from bianbu_agent.mcp.managed import (
        managed_mcp_id,
        parse_managed_mcp,
        parse_remote_mcp,
    )

    for name in ("nws", "Filesystem", "文件系统", "---", "News Feed"):
        assert parse_managed_mcp(name, "/bin/true").plugin_id == managed_mcp_id(name)
        assert parse_remote_mcp(name, "https://example.test/mcp").plugin_id == managed_mcp_id(name)
        assert resolve_mcp_selectors((name,), {managed_mcp_id(name)}) == (managed_mcp_id(name),)
