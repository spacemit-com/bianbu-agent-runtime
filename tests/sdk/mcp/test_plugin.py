from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage

from types import SimpleNamespace

from bianbu_agent import (
    AgentModel,
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    RuntimeConfig,
)
from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent.inventory import (
    CapabilityCatalogBuilder,
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InstalledCapability,
    InventorySnapshot,
)
from bianbu_agent.mcp.client import MCPCallResult, MCPClient, MCPRemoteTool
from bianbu_agent.home import AgentHome
from bianbu_agent.mcp.managed import ManagedMCPDefinition
from bianbu_agent.mcp.plugin import MCPDirectoryPlugin
from bianbu_agent.mcp.preparation import MCPPreparationManager
from bianbu_agent.mcp.readiness import MCP_READY, MCPProbeResult
from bianbu_agent.mcp.sources import runtime_mcp_id
from bianbu_agent.tools.builtins import BUILTIN_MODULES
from bianbu_agent.plugins import (
    PluginContext,
    PluginGeneration,
    PluginHealth,
    PluginManifest,
    PluginRuntime,
    PluginScope,
    PluginState,
    PluginStatus,
    ToolCallContext,
)
from bianbu_agent.server.runtime import _validate_file_backed


class _Client:
    async def start(self):
        pass

    async def close(self):
        pass

    def __init__(self, tools: tuple[MCPRemoteTool, ...]) -> None:
        self.tools = tools
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.call_options: list[dict[str, object]] = []
        self.result = MCPCallResult("ok")
        self.list_error: Exception | None = None

    async def list_tools(self) -> tuple[MCPRemoteTool, ...]:
        if self.list_error is not None:
            raise self.list_error
        return self.tools

    async def call_tool(
        self, name: str, arguments: dict[str, object], **kwargs
    ) -> MCPCallResult:
        self.calls.append((name, dict(arguments)))
        self.call_options.append(dict(kwargs))
        return self.result


class _ConcurrentClient(_Client):
    async def call_tool(
        self, name: str, arguments: dict[str, object], **kwargs
    ) -> MCPCallResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.05)
        finally:
            self.active -= 1
        return await super().call_tool(name, arguments, **kwargs)

    def __init__(self, tools: tuple[MCPRemoteTool, ...]) -> None:
        super().__init__(tools)
        self.active = 0
        self.max_active = 0


def _manifest(plugin_id: str = "acme.filesystem") -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        version="0.0.0",
    )


def _plugin_root(tmp_path: Path, document: dict[str, object] | None = None) -> Path:
    root = tmp_path / "acme.filesystem"
    root.mkdir()
    (root / "mcp.json").write_text(
        json.dumps(
            document
            or {"transport": "stdio", "command": "fake", "env": {"TOKEN": "${TOKEN}"}}
        ),
        encoding="utf-8",
    )
    return root


def _context(tmp_path: Path) -> PluginContext:
    return PluginContext(
        plugin_id="acme.filesystem",
        data_dir=tmp_path / "data",
    )


async def _setup(plugin: MCPDirectoryPlugin, context: PluginContext):
    await plugin.setup(context)
    assert context.scope is not None
    return context.scope.snapshot()


async def _until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the expected state was not reached")


@pytest.mark.asyncio
async def test_a_server_that_still_needs_its_package_is_registered_as_preparing(
    tmp_path: Path,
) -> None:
    """A Generation must not wait for a package that is still downloading.

    ``mcp add`` only registers a declaration now, so the first Generation after
    an install meets an ``npx`` server whose package is not on disk yet.  It
    registers the server as ``preparing`` — visible in the Console — and the
    background preparation publishes its tools through the reload.
    """

    home = AgentHome(tmp_path / "home").prepare()
    release = asyncio.Event()
    prepared: list[str] = []

    def prepare(definition):
        prepared.append(definition.npm_package)
        target = home.mcp_runtime_dir / definition.plugin_id
        return (
            ManagedMCPDefinition(
                plugin_id=definition.plugin_id,
                display_name=definition.display_name,
                executable=sys.executable,
                args=(str(Path(__file__)),),
                transport="stdio",
                npm_package=definition.npm_package,
                npm_arguments=definition.npm_arguments,
            ),
            target,
        )

    async def probe(_config):
        await release.wait()
        return MCPProbeResult(MCP_READY, ("search",), "")

    manager = MCPPreparationManager(
        home,
        prepare=prepare,
        probe=probe,
        reload_debounce_seconds=0.0,
    )
    root = _plugin_root(
        tmp_path,
        {
            "display_name": "Bilibili",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "bilibili-mcp-js"],
        },
    )

    def context() -> PluginContext:
        return PluginContext(
            plugin_id="acme.filesystem",
            data_dir=tmp_path / "data",
            scope=PluginScope(
                "acme.filesystem",
                base_services={MCPPreparationManager.service_name: manager},
            ),
        )

    waiting = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        client_factory=lambda _config: _Client(()),
    )
    try:
        snapshot = await _setup(waiting, context())

        assert snapshot.tools == ()
        assert snapshot.mcp_runtime is not None
        assert snapshot.mcp_runtime.display_name == "Bilibili"
        health = await waiting.health()
        assert health.status == "preparing"

        release.set()
        await _until(lambda: manager.state("acme.filesystem").ready)

        # The Generation that follows starts the local installation.
        ready = MCPDirectoryPlugin(
            _manifest(),
            root=root,
            workspace=tmp_path / "workspace",
            client_factory=lambda _config: _Client(
                (MCPRemoteTool("search", "Search", "", {}),)
            ),
        )
        snapshot = await _setup(ready, context())

        assert [tool.spec.name for tool in snapshot.tools] == ["search"]
        assert (await ready.health()).status == "healthy"
        assert prepared == ["bilibili-mcp-js"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_tool_handlers_allow_concurrent_mcp_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKEN", "not-returned")
    root = _plugin_root(tmp_path)
    client = _ConcurrentClient(
        (
            MCPRemoteTool("slow-a", "Slow A", "", {"type": "object"}),
            MCPRemoteTool("slow-b", "Slow B", "", {"type": "object"}),
        )
    )
    plugin = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        client_factory=lambda _config: client,
    )
    contributions = await _setup(plugin, _context(tmp_path))

    await asyncio.gather(
        contributions.tools[0].handler(
            ToolCallContext("run-1", "session-1", "agent-1"), {}
        ),
        contributions.tools[1].handler(
            ToolCallContext("run-2", "session-2", "agent-1"), {}
        ),
    )

    assert client.max_active == 2


@pytest.mark.asyncio
async def test_discovers_namespaced_tools_and_resolves_normalized_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOKEN", "not-returned")
    root = _plugin_root(tmp_path)
    client = _Client(
        (
            MCPRemoteTool(
                "Find-Files",
                "Find files",
                "Find files by glob",
                {"type": "object", "properties": {"glob": {"type": "string"}}},
            ),
            MCPRemoteTool(
                "find.files",
                "Find indexed files",
                "Search the file index",
                {"type": "object", "properties": {}},
            ),
        )
    )
    plugin = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        client_factory=lambda _config: client,
    )

    contributions = await _setup(plugin, _context(tmp_path))

    assert tuple(tool.spec.name for tool in contributions.tools) == (
        "find_files_c653e4b2",
        "find_files_0e2492df",
    )
    assert tuple(tool.spec.metadata["qualified_id"] for tool in contributions.tools) == (
        "acme.filesystem/find_files_c653e4b2",
        "acme.filesystem/find_files_0e2492df",
    )
    assert contributions.tools[0].spec.metadata == {
        "qualified_id": "acme.filesystem/find_files_c653e4b2",
        "display_name": "Find files",
        "remote_name": "Find-Files",
        "origin": "mcp",
        "plugin_id": "acme.filesystem",
        "mcp_id": "acme.filesystem",
        "runtime_id": "acme.filesystem",
        "owner_plugin_id": None,
        "source_kind": "standalone",
        "source_path": str((root / "mcp.json").resolve()),
        "manifest_path": str((root / "mcp.json").resolve()),
        "version": "0.0.0",
    }


@pytest.mark.asyncio
async def test_health_reprobes_server_and_reports_connection_failure(tmp_path: Path) -> None:
    root = _plugin_root(tmp_path, {"transport": "stdio", "command": "fake"})
    client = _Client((MCPRemoteTool("echo", "Echo", "", {"type": "object"}),))
    plugin = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        client_factory=lambda _config: client,
    )
    await plugin.setup(_context(tmp_path))
    client.list_error = ConnectionError("synthetic secret must not escape")

    health = await plugin.health()

    assert health.status == "unhealthy"
    assert health.message == "MCP server health check failed (ConnectionError)"


@pytest.mark.asyncio
async def test_tool_handler_calls_preserved_remote_name_and_marks_remote_error(
    tmp_path: Path,
) -> None:
    root = _plugin_root(tmp_path, {"transport": "stdio", "command": "fake"})
    client = _Client(
        (
            MCPRemoteTool(
                "Find-Files",
                "Find files",
                "",
                {"type": "object", "properties": {}},
            ),
        )
    )
    client.result = MCPCallResult(
        "permission denied",
        is_error=True,
        metadata={
            "request_id": "remote-1",
            "run_id": "remote-run",
            "session_id": "remote-session",
            "agent_id": "remote-agent",
            "mcp_tool": "remote-tool",
        },
    )
    plugin = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        client_factory=lambda _config: client,
    )
    contribution = (await _setup(plugin, _context(tmp_path))).tools[0]

    result = await contribution.handler(
        ToolCallContext("run-1", "session-1", "agent-1"),
        {"glob": "*.md"},
    )

    assert client.calls == [("Find-Files", {"glob": "*.md"})]
    assert result.content == "permission denied"
    assert result.metadata == {
        "mcp_error": True,
        "remote_name": "Find-Files",
        "request_id": "remote-1",
        "mcp_tool": "acme.filesystem/find_files",
        "run_id": "run-1",
        "session_id": "session-1",
        "agent_id": "agent-1",
    }


@pytest.mark.asyncio
async def test_tool_handler_attaches_the_runtime_host_context(tmp_path: Path) -> None:
    """每个 MCP 调用都带 ``bianbu/host``；插件侧靠它拿会话身份。

    子进程继承不到 Worker 的上下文，所以会话、运行、Agent 和账户只能是
    调用时附在 ``_meta`` 上的提示。少了它，Memory 这类按会话落证据的服务端
    就只能拿到空 session_id。
    """

    root = _plugin_root(tmp_path, {"transport": "stdio", "command": "fake"})
    client = _Client(
        (
            MCPRemoteTool(
                "Find-Files",
                "Find files",
                "",
                {"type": "object", "properties": {}},
            ),
        )
    )
    plugin = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        client_factory=lambda _config: client,
    )
    contribution = (await _setup(plugin, _context(tmp_path))).tools[0]

    await contribution.handler(
        ToolCallContext(
            run_id="run-1",
            session_id="session-1",
            agent_id="agent-1",
            account_id="account-1",
        ),
        {"glob": "*.md"},
    )

    assert client.call_options == [
        {
            "progress_callback": None,
            "meta": {
                "bianbu/host": {
                    "session_id": "session-1",
                    "run_id": "run-1",
                    "agent_id": "agent-1",
                    "account_id": "account-1",
                }
            },
        }
    ]


@pytest.mark.asyncio
async def test_tool_handler_rechecks_whole_server_agent_authorization(
    tmp_path: Path,
) -> None:
    root = _plugin_root(tmp_path, {"transport": "stdio", "command": "fake"})
    client = _Client(
        (MCPRemoteTool("forecast", "Forecast", "", {"type": "object"}),)
    )

    async def authorize(agent_id: str, mcp_id: str) -> bool:
        return agent_id == "weather-agent" and mcp_id == "mcp.weather"

    plugin = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        mcp_id="mcp.weather",
        authorize=authorize,
        client_factory=lambda _config: client,
    )
    contribution = (await _setup(plugin, _context(tmp_path))).tools[0]

    with pytest.raises(PermissionError, match="has not selected MCP server"):
        await contribution.handler(
            ToolCallContext("run-1", "session-1", "other-agent"),
            {},
        )
    assert client.calls == []
    await contribution.handler(
        ToolCallContext("run-2", "session-2", "weather-agent"),
        {},
    )
    assert client.calls == [("forecast", {})]


@pytest.mark.asyncio
async def test_runtime_mcp_authorization_honors_agent_wildcard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / ".bianbu-agent"
    mcp_root = data_dir / "mcps" / "mcp.filesystem"
    mcp_root.mkdir(parents=True)
    (mcp_root / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": "fake"}),
        encoding="utf-8",
    )
    calls: list[tuple[str, dict[str, object]]] = []

    async def list_tools(_client: MCPClient) -> tuple[MCPRemoteTool, ...]:
        return (
            MCPRemoteTool(
                "list_allowed_directories",
                "List Allowed Directories",
                "",
                {"type": "object", "properties": {}},
            ),
        )

    async def call_tool(
        _client: MCPClient,
        name: str,
        arguments: dict[str, object],
        **_kwargs,
    ) -> MCPCallResult:
        calls.append((name, dict(arguments)))
        return MCPCallResult("/desktop")

    async def start(_client):
        pass

    monkeypatch.setattr(MCPClient, "start", start)
    monkeypatch.setattr(MCPClient, "list_tools", list_tools)
    monkeypatch.setattr(MCPClient, "call_tool", call_tool)
    agents = (
        AgentSpec(
            id="wildcard",
            name="Wildcard",
            model=AgentModel("test", "test"),
            mcps=("*",),
            plugins=("*",),
        ),
        AgentSpec(
            id="explicit",
            name="Explicit",
            model=AgentModel("test", "test"),
            mcps=("mcp.filesystem",),
            plugins=("*",),
        ),
        AgentSpec(id="unselected", name="Unselected", model=AgentModel("test", "test")),
    )
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=data_dir),
        default_model=FakeMessagesListChatModel(
            responses=[AIMessage(content="unused")]
        ),
        agent_id="wildcard",
        agent_registry=InMemoryAgentRegistry(agents),
        engine_builder=lambda *_args: object(),
        model_resolver=SimpleNamespace(resolve_agent_model=lambda selected: object()),
    )
    await runtime.start()
    try:
        contribution = runtime.plugin_runtime.current_generation.tools[
            "mcp.filesystem/list_allowed_directories"
        ]

        wildcard = await contribution.handler(
            ToolCallContext("run-wildcard", "session", "wildcard"),
            {},
        )
        explicit = await contribution.handler(
            ToolCallContext("run-explicit", "session", "explicit"),
            {},
        )
        from bianbu_agent.agents.selection import task_agent
        token = task_agent.set(AgentSpec(id="explicit", name="Explicit", model=AgentModel("test", "test"), mcps=("mcp.filesystem",), plugins=("*",)))
        try:
            await runtime.agents.update(AgentSpec(id="explicit", name="Explicit", model=AgentModel("test", "test")))
            # A running task keeps its selection after expert configuration changes.
            assert (await contribution.handler(ToolCallContext("old-run", "session", "explicit"), {})).content == "/desktop"
        finally:
            task_agent.reset(token)
        with pytest.raises(PermissionError):
            await contribution.handler(ToolCallContext("new-run", "session", "explicit"), {})
        with pytest.raises(PermissionError, match="has not selected MCP server"):
            await contribution.handler(
                ToolCallContext("run-unselected", "session", "unselected"),
                {},
            )
    finally:
        await runtime.close()

    assert wildcard.content == explicit.content == "/desktop"
    assert calls == [
        ("list_allowed_directories", {}),
        ("list_allowed_directories", {}),
        ("list_allowed_directories", {}),
    ]


@pytest.mark.asyncio
async def test_mcp_runtime_reports_connection_without_returning_resolved_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOKEN", "top-secret")
    root = _plugin_root(
        tmp_path,
        {
            "display_name": "Acme Filesystem",
            "transport": "stdio",
            "command": "fake",
            "env": {"TOKEN": "${TOKEN}"},
        },
    )
    client = _Client(
        (MCPRemoteTool("echo", "Echo", "Echo input", {"type": "object"}),)
    )
    plugin = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        client_factory=lambda _config: client,
    )
    runtime = (await _setup(plugin, _context(tmp_path))).mcp_runtime
    assert runtime is not None

    assert runtime.mcp_id == "acme.filesystem"
    assert runtime.display_name == "Acme Filesystem"
    assert runtime.transport == "stdio"
    assert runtime.command == "fake"
    assert runtime.owner_plugin_id is None
    assert runtime.source_kind == "standalone"
    assert runtime.tools == (
        {"id": "acme.filesystem/echo", "name": "echo", "display_name": "Echo"},
    )
    assert "top-secret" not in repr(runtime)


@pytest.mark.asyncio
async def test_strict_generation_accepts_only_digest_backed_mcp_contributions(
    tmp_path: Path,
) -> None:
    root = _plugin_root(tmp_path, {"transport": "stdio", "command": "fake"})
    runtime_id = runtime_mcp_id("acme.filesystem")
    client = _Client(
        (MCPRemoteTool("echo", "Echo", "", {"type": "object"}),)
    )
    plugin = MCPDirectoryPlugin(
        _manifest(runtime_id),
        root=root,
        workspace=tmp_path / "workspace",
        mcp_id="acme.filesystem",
        client_factory=lambda _config: client,
    )
    contributions = await _setup(
        plugin, PluginContext(plugin_id=runtime_id, data_dir=tmp_path / "data")
    )
    files = regular_files(root)
    source = CapabilitySource(
        kind=CapabilityKind.MCP,
        capability_id="acme.filesystem",
        version="0.0.0",
        root=root,
        manifest_path=root / "mcp.json",
        content_digest=digest_directory(root, files),
        origin=CapabilityOrigin.STANDALONE,
    )
    catalog = CapabilityCatalogBuilder(workspace_root=tmp_path / "workspace").build(
        InventorySnapshot(
            revision=1,
            items=(InstalledCapability(source=source, enabled=True),),
        )
    )

    generation = PluginGeneration(
        id=1,
        plugins={
            runtime_id: PluginRuntime(
                plugin_id=runtime_id,
                instance=plugin,
                config={},
                contributions=contributions,
            )
        },
        statuses={
            runtime_id: PluginStatus(
                plugin_id=runtime_id,
                state=PluginState.RUNNING,
                health=PluginHealth.healthy(),
            )
        },
        capability_catalog=catalog,
    )

    # Built-in Tools ship with every generation; the digest-backed MCP
    # contribution is added on top of them instead of replacing the set.
    assert set(BUILTIN_MODULES) <= set(generation.tools)
    assert "acme.filesystem/echo" in generation.tools
    assert tuple(generation.mcps) == ("acme.filesystem",)
    _validate_file_backed(generation)

    (root / "mcp.json").write_text(
        '{"transport":"stdio","command":"changed"}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="file-backed catalog"):
        PluginGeneration(
            id=2,
            plugins={
                runtime_id: PluginRuntime(
                    plugin_id=runtime_id,
                    instance=plugin,
                    config={},
                    contributions=contributions,
                )
            },
            statuses={},
            capability_catalog=catalog,
        )


@pytest.mark.asyncio
async def test_generation_groups_first_class_mcp_tools_by_public_server(
    tmp_path: Path,
) -> None:
    root = _plugin_root(tmp_path, {"transport": "stdio", "command": "fake"})
    runtime_id = runtime_mcp_id("mcp.weather")
    manifest = _manifest(runtime_id)
    client = _Client(
        (
            MCPRemoteTool("forecast", "Forecast", "", {"type": "object"}),
            MCPRemoteTool("air_quality", "Air quality", "", {"type": "object"}),
        )
    )
    plugin = MCPDirectoryPlugin(
        manifest,
        root=root,
        workspace=tmp_path / "workspace",
        mcp_id="mcp.weather",
        client_factory=lambda _config: client,
    )
    contributions = await _setup(
        plugin, PluginContext(plugin_id=runtime_id, data_dir=tmp_path / "data")
    )
    source = CapabilitySource(
        kind=CapabilityKind.MCP,
        capability_id="mcp.weather",
        version="0.0.0",
        root=root,
        manifest_path=root / "mcp.json",
        content_digest=digest_directory(root, regular_files(root)),
        origin=CapabilityOrigin.STANDALONE,
    )
    catalog = CapabilityCatalogBuilder(workspace_root=tmp_path / "workspace").build(
        InventorySnapshot(
            revision=1,
            items=(InstalledCapability(source=source, enabled=True),),
        )
    )

    generation = PluginGeneration(
        id=1,
        plugins={
            runtime_id: PluginRuntime(
                plugin_id=runtime_id,
                instance=plugin,
                config={},
                contributions=contributions,
            )
        },
        statuses={
            runtime_id: PluginStatus(
                plugin_id=runtime_id,
                state=PluginState.RUNNING,
                health=PluginHealth.healthy(),
            )
        },
        capability_catalog=catalog,
    )

    assert set(generation.mcps) == {"mcp.weather"}
    assert generation.mcps["mcp.weather"].runtime_id == runtime_id
    assert generation.mcps["mcp.weather"].owner_plugin_id is None
    assert generation.mcp_tools["mcp.weather"] == (
        "mcp.weather/air_quality",
        "mcp.weather/forecast",
    )


def test_runtime_file_gate_rejects_spoofed_mcp_metadata_without_plugin_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mcp.json"
    source.write_text('{"transport":"stdio","command":"fake"}', encoding="utf-8")

    async def handler(_context, _arguments):
        return None

    from bianbu_agent.plugins import ToolContribution, ToolSpec

    contribution = ToolContribution(
        plugin_id="attacker.plugin",
        spec=ToolSpec(
            "spoofed",
            "",
            {"type": "object"},
            metadata={
                "origin": "mcp",
                "plugin_id": "attacker.plugin",
                "source_path": str(source),
            },
        ),
        handler=handler,
    )
    generation = type(
        "Generation",
        (),
        {"tools": {"attacker.plugin/spoofed": contribution}, "skills": {}, "capability_catalog": None},
    )()

    with pytest.raises(ValueError, match="not backed by visible files"):
        _validate_file_backed(generation)


class _FlakyClient(_Client):
    """A client whose first ``start()`` fails exactly like a refused connection."""

    def __init__(self, tools, error: BaseException, failures: int = 1) -> None:
        super().__init__(tools)
        self.error = error
        self.failures = failures
        self.starts = 0
        self.closed = 0

    async def start(self) -> None:
        self.starts += 1
        if self.starts <= self.failures:
            raise self.error

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_setup_retries_a_transport_that_never_connected(tmp_path: Path) -> None:
    root = _plugin_root(tmp_path, {"transport": "stdio", "command": "fake"})
    tools = (MCPRemoteTool("echo", "Echo", "", {"type": "object"}),)
    attempts: list[_FlakyClient] = []

    def factory(_config):
        # Every attempt must open a fresh transport: a failed MCP session keeps
        # anyio cancel scopes that cannot be reused.
        client = _FlakyClient(tools, _empty_message_error(), failures=1 if not attempts else 0)
        attempts.append(client)
        return client

    plugin = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        client_factory=factory,
    )

    contributions = await _setup(plugin, _context(tmp_path))

    assert len(attempts) == 2
    assert attempts[0].starts == 1 and attempts[0].closed == 1
    assert attempts[1].starts == 1 and attempts[1].closed == 0
    assert tuple(tool.spec.name for tool in contributions.tools) == ("echo",)


@pytest.mark.asyncio
async def test_setup_does_not_retry_a_rejected_declaration(tmp_path: Path) -> None:
    root = _plugin_root(tmp_path, {"transport": "stdio", "command": "fake"})
    client = _FlakyClient(
        (MCPRemoteTool("echo", "Echo", "", {"type": "object"}),),
        ValueError("MCP server returned duplicate tool names"),
    )
    plugin = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        client_factory=lambda _config: client,
    )

    with pytest.raises(ValueError):
        await plugin.setup(_context(tmp_path))

    assert client.starts == 1


def _empty_message_error() -> BaseException:
    """Reproduce httpx/anyio: a transport error that stringifies to nothing."""

    error = ConnectionError()
    assert str(error) == ""
    return error
