from pathlib import Path
from types import SimpleNamespace

import pytest

from bianbu_agent import ToolCallContext
from bianbu_agent.tools import ToolPackageContext
from bianbu_agent.tools.runtime import (
    ToolExecutionScope,
    ToolPermissionError,
    ToolRuntime,
    ToolRuntimeFactory,
    ToolRuntimeServices,
)
from bianbu_agent.tools.runtime_services import RuntimeServices
from bianbu_agent import AgentHome, AgentSpec, OpenAICompatibleModel


def _package_context(tmp_path: Path, factory: object) -> ToolPackageContext:
    root = tmp_path / "read_file"
    root.mkdir(exist_ok=True)
    return ToolPackageContext(
        name="read_file",
        package_root=root,
        source_digest="a" * 64,
        workspace_root=tmp_path / "workspace",
    )


@pytest.mark.asyncio
async def test_runtime_derives_tool_identity_and_exact_agent_scope(tmp_path: Path) -> None:
    async def resolve(agent_id: str):
        assert agent_id == "writer"
        return ("read_file", "search_files"), ("review",)

    factory = ToolRuntimeFactory(
        workspace_root=tmp_path / "workspace",
        scope_resolver=resolve,
    )
    runtime = await factory.for_call(
        _package_context(tmp_path, factory),
        ToolCallContext("run-1", "session-1", "writer"),
    )

    assert runtime.scope.tool_name == "read_file"
    assert runtime.scope.enabled_tools == ("read_file", "search_files")
    assert runtime.scope.allowed_skills == ("review",)
    assert runtime.scope.permissions == frozenset()


@pytest.mark.asyncio
async def test_runtime_fails_closed_for_unknown_agent(tmp_path: Path) -> None:
    async def unknown(_agent_id: str):
        raise KeyError("missing")

    factory = ToolRuntimeFactory(
        workspace_root=tmp_path / "workspace",
        scope_resolver=unknown,
    )
    with pytest.raises(ToolPermissionError, match="unknown Agent"):
        await factory.for_call(
            _package_context(tmp_path, factory),
            ToolCallContext("run", "session", "missing"),
        )


def test_nested_tool_calls_use_current_scope_and_wrapped_tool(tmp_path: Path) -> None:
    observed = []

    class WrappedTool:
        name = "read_file"
        args_schema = None

        async def ainvoke(self, arguments, config):
            observed.append((arguments, config))
            return {"content": "ok"}

    scope = ToolExecutionScope(
        tool_name="terminal",
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        workspace=tmp_path,
        enabled_tools=("terminal", "read_file"),
        allowed_skills=None,
        permissions=frozenset(),
        hook_ids=("vendor.hooks/old.before_tool",),
    )
    runtime = ToolRuntime(
        scope=scope,
        state_root=tmp_path / "state",
        services=SimpleNamespace(available_tools=[WrappedTool()]),
    )

    assert runtime.tools.invoke_sync("read_file", {"path": "README.md"}) == {
        "content": "ok"
    }
    assert observed[0][0] == {"path": "README.md"}
    assert observed[0][1]["configurable"]["run_id"] == "run-1"
    assert observed[0][1]["configurable"]["hook_ids"] == (
        "vendor.hooks/old.before_tool",
    )


def test_nested_tool_calls_cannot_escape_scope_or_reenter_current_tool(
    tmp_path: Path,
) -> None:
    scope = ToolExecutionScope(
        tool_name="terminal",
        run_id="run",
        session_id="session",
        agent_id="agent",
        workspace=tmp_path,
        enabled_tools=("terminal", "read_file"),
        allowed_skills=None,
        permissions=frozenset(),
    )
    runtime = ToolRuntime(
        scope=scope,
        state_root=tmp_path / "state",
        services=SimpleNamespace(available_tools=[]),
    )

    assert runtime.tools.can_invoke("read_file") is True
    assert runtime.tools.can_invoke("terminal") is False
    with pytest.raises(ToolPermissionError, match="not available"):
        runtime.tools.invoke_sync("write_file", {"path": "x"})


@pytest.mark.asyncio
async def test_child_tool_calls_receive_parent_hook_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Child:
        def invoke(self, _input, *, config):
            captured["config"] = config
            return {"messages": [SimpleNamespace(content="child result")]}

    monkeypatch.setattr(
        "bianbu_agent.engines.langgraph.build_langgraph_agent",
        lambda **_kwargs: Child(),
    )
    scope = ToolExecutionScope(
        tool_name="delegate",
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        workspace=tmp_path,
        enabled_tools=(),
        allowed_skills=None,
        permissions=frozenset(),
        hook_ids=("vendor.hooks/old.before_tool",),
    )
    runtime = ToolRuntime(
        scope=scope,
        state_root=tmp_path / "state",
        services=RuntimeServices(
            sessions_root=tmp_path / "sessions",
            workspace=tmp_path,
            model="test-model",
        ),
    )

    assert await runtime.agents.run_child(
        "delegate this", system_prompt="child system", child_key="child-1"
    ) == "child result"
    assert captured["config"]["configurable"]["hook_ids"] == (
        "vendor.hooks/old.before_tool",
    )


def test_capability_child_call_propagates_provider_and_runtime_capture_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    callback = object()

    class Child:
        def invoke(self, _input, *, config):
            captured["config"] = config
            return {"messages": [SimpleNamespace(content="child result")]}

    class Resolver:
        def resolve_capability(self, name: str):
            assert name in {"chat", "vision"}
            if name == "vision":
                return None
            return OpenAICompatibleModel(
                model="child-model",
                provider_id="custom:child",
                api_key="secret",
                base_url="https://models.example.test/v1",
            )

    monkeypatch.setattr(
        "bianbu_agent.engines.langgraph.build_langgraph_agent",
        lambda **_kwargs: Child(),
    )
    home = AgentHome(tmp_path / "state").prepare()
    factory = ToolRuntimeServices(home=home, model=None, model_resolver=Resolver())
    factory.bind_sdk(
        SimpleNamespace(home=home, provider_capture_callback=callback)
    )
    factory.begin_generation_candidate()
    generation_factory = factory.for_tool_generation("child_runner")

    result = generation_factory._services.run_child(
        "session-child",
        "delegate this",
        "child system",
        "child-1",
        run_id="run-child",
        agent_id="agent-child",
    )

    assert result == "child result"
    assert captured["config"]["callbacks"] == [callback]
    assert captured["config"]["metadata"] == {
        "provider_id": "custom:child",
        "agent_id": "agent-child",
        "session_id": "session-child",
        "run_id": "run-child",
    }


@pytest.mark.asyncio
async def test_product_scope_uses_generation_materialized_mcp_access(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / "state").prepare()
    generation = SimpleNamespace()

    class Agents:
        async def get(self, agent_id: str) -> AgentSpec:
            assert agent_id == "weather"
            return AgentSpec(
                id="weather",
                name="Weather",
                tools=("clock",),
                mcps=("mcp.weather",),
            )

        async def materialize(self, agent_id: str, selected_generation, **kwargs) -> AgentSpec:
            assert selected_generation is generation
            spec = await self.get(agent_id)
            return AgentSpec(
                id=spec.id,
                name=spec.name,
                tools=("clock", "mcp.weather/forecast"),
                mcps=spec.mcps,
            )

    runtime = ToolRuntimeServices(home=home, model=None)
    runtime.sdk = SimpleNamespace(home=home, agents=Agents())
    runtime._generation = generation

    tools, skills = await runtime.resolve_agent_capabilities("weather")

    assert tools == ("clock", "mcp.weather/forecast")
    assert skills == ()
    assert await runtime._agent_selects_tool(
        "weather", "mcp.weather/forecast"
    ) is True
    assert await runtime._agent_selects_tool("weather", "mcp.weather/alerts") is False
