from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

import anyio
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, ErrorData, ResourceLink

from bianbu_agent.mcp.client import MCPCallResult, MCPClient, MCPRemoteTool
import bianbu_agent.mcp.client as client_module
from bianbu_agent.mcp.config import MCPServerConfig


class _Block:
    def __init__(self, document: dict[str, object]) -> None:
        self._document = document
        self.text = document.get("text")

    def model_dump(self, **_kwargs) -> dict[str, object]:
        return dict(self._document)


class _Session:
    def __init__(self, *, tools=(), result=None) -> None:
        self.events: list[object] = []
        self._tools = tuple(tools)
        self._result = result

    async def initialize(self) -> None:
        self.events.append("initialize")

    async def list_tools(self):
        self.events.append("list_tools")
        return SimpleNamespace(tools=self._tools)

    async def call_tool(
        self, name: str, arguments: dict[str, object], **_kwargs
    ):
        self.events.append(("call_tool", name, arguments))
        return self._result


def _factory(session: _Session):
    @asynccontextmanager
    async def open_session(_config):
        yield session

    return open_session


@pytest.mark.asyncio
async def test_lists_remote_tools_after_initializing_session() -> None:
    session = _Session(
        tools=(
            SimpleNamespace(
                name="find-files",
                title="Find files",
                description="Search a directory",
                inputSchema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                annotations=SimpleNamespace(title="Annotated title"),
            ),
        )
    )
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=_factory(session),
    )

    tools = await client.list_tools()

    assert tools == (
        MCPRemoteTool(
            name="find-files",
            display_name="Find files",
            description="Search a directory",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
    )
    assert session.events == ["initialize", "list_tools"]


@pytest.mark.asyncio
async def test_calls_remote_tool_and_prefers_structured_content() -> None:
    result = SimpleNamespace(
        content=(_Block({"type": "text", "text": "fallback"}),),
        structuredContent={"matches": ["a.txt", "b.txt"]},
        isError=False,
        meta={"request_id": "request-1"},
    )
    session = _Session(result=result)
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=_factory(session),
    )

    call = await client.call_tool("find-files", {"query": "*.txt"})

    assert call == MCPCallResult(
        content='{"matches":["a.txt","b.txt"]}',
        is_error=False,
        structured_content={"matches": ["a.txt", "b.txt"]},
        metadata={"request_id": "request-1"},
    )
    assert session.events == [
        "initialize",
        ("call_tool", "find-files", {"query": "*.txt"}),
    ]


@pytest.mark.asyncio
async def test_preserves_multiple_content_blocks_and_error_flag() -> None:
    result = SimpleNamespace(
        content=(
            _Block({"type": "text", "text": "permission denied"}),
            _Block({"type": "image", "data": "YWJj", "mimeType": "image/png"}),
        ),
        structuredContent=None,
        isError=True,
        meta=None,
    )
    session = _Session(result=result)
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=_factory(session),
    )

    call = await client.call_tool("render", {})

    assert call.content == (
        '[{"text":"permission denied","type":"text"},'
        '{"data_omitted":true,"mimeType":"image/png","size_bytes":3,'
        '"type":"image"}]'
    )
    assert "YWJj" not in call.content
    assert call.is_error is True
    assert call.structured_content is None
    assert call.metadata == {}


@pytest.mark.asyncio
async def test_redacts_binary_blocks_nested_in_structured_content() -> None:
    result = SimpleNamespace(
        content=(_Block({"type": "text", "text": "fallback"}),),
        structuredContent={
            "preview": {
                "type": "image",
                "data": "YWJj",
                "mimeType": "image/png",
            }
        },
        isError=False,
        meta=None,
    )
    session = _Session(result=result)
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=_factory(session),
    )

    call = await client.call_tool("render", {})

    assert "YWJj" not in call.content
    assert call.structured_content == {
        "preview": {
            "type": "image",
            "data_omitted": True,
            "mimeType": "image/png",
            "size_bytes": 3,
        }
    }


@pytest.mark.asyncio
async def test_redacts_blob_from_embedded_resource_content() -> None:
    result = SimpleNamespace(
        content=(
            _Block(
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///tmp/report.png",
                        "blob": "YWJj",
                    },
                }
            ),
        ),
        structuredContent=None,
        isError=False,
        meta=None,
    )
    session = _Session(result=result)
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=_factory(session),
    )

    call = await client.call_tool("read-resource", {})

    assert "YWJj" not in call.content
    assert '"data_omitted":true' in call.content
    assert '"size_bytes":3' in call.content


@pytest.mark.asyncio
async def test_returns_single_text_block_without_json_wrapper() -> None:
    result = SimpleNamespace(
        content=(_Block({"type": "text", "text": "hello"}),),
        structuredContent=None,
        isError=False,
        meta=None,
    )
    session = _Session(result=result)
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=_factory(session),
    )

    call = await client.call_tool("echo", {"message": "hello"})

    assert call.content == "hello"


@pytest.mark.asyncio
async def test_streamable_http_builds_sdk_http_client_with_headers_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def transport(url: str, *, http_client=None, terminate_on_close=True):
        captured.update(
            url=url,
            authorization=http_client.headers.get("Authorization"),
            timeout=http_client.timeout.read,
            terminate_on_close=terminate_on_close,
        )
        yield object(), object(), lambda: None

    class Session:
        def __init__(self, _read, _write) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(client_module, "streamable_http_client", transport)
    monkeypatch.setattr(client_module, "ClientSession", Session)
    config = MCPServerConfig(
        transport="streamable-http",
        url="https://mcp.example.test/rpc",
        headers={"Authorization": "Bearer token"},
        timeout_seconds=17,
    )

    async with client_module._open_session(config):
        pass

    assert captured == {
        "url": "https://mcp.example.test/rpc",
        "authorization": "Bearer token",
        "timeout": 17,
        "terminate_on_close": True,
    }


@pytest.mark.asyncio
async def test_lists_every_paginated_tool_page_in_one_initialized_session() -> None:
    first = SimpleNamespace(
        name="first",
        title=None,
        annotations=None,
        description="First",
        inputSchema={"type": "object"},
    )
    second = SimpleNamespace(
        name="second",
        title=None,
        annotations=None,
        description="Second",
        inputSchema={"type": "object"},
    )

    class PaginatedSession(_Session):
        async def list_tools(self, cursor=None):
            self.events.append(("list_tools", cursor))
            if cursor is None:
                return SimpleNamespace(tools=(first,), nextCursor="page-2")
            return SimpleNamespace(tools=(second,), nextCursor=None)

    session = PaginatedSession()
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=_factory(session),
    )

    tools = await client.list_tools()

    assert tuple(tool.name for tool in tools) == ("first", "second")
    assert session.events == [
        "initialize",
        ("list_tools", None),
        ("list_tools", "page-2"),
    ]


@pytest.mark.asyncio
async def test_serializes_resource_links_with_urls_in_json_mode() -> None:
    result = SimpleNamespace(
        content=(
            ResourceLink(
                type="resource_link",
                name="documentation",
                uri="https://example.test/docs",
            ),
        ),
        structuredContent=None,
        isError=False,
        meta=None,
    )
    session = _Session(result=result)
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=_factory(session),
    )

    call = await client.call_tool("documentation", {})

    assert call.content == (
        '[{"name":"documentation","type":"resource_link",'
        '"uri":"https://example.test/docs"}]'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,wrapped", [(None, False), ("host", False), ("sandbox", False), ("host", True)])
async def test_stdio_passes_only_declared_values_for_sdk_safe_environment_merge(
    monkeypatch: pytest.MonkeyPatch, mode, wrapped,
) -> None:
    captured: dict[str, str] = {}

    @asynccontextmanager
    async def transport(server):
        captured.update(server.env)
        yield object(), object()

    class Session:
        def __init__(self, _read, _write) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    reserved = {
        "BIANBU_RUNTIME_ROLE",
        "BIANBU_WORKER_EFFECTIVE_MODE",
        "BIANBU_SANDBOX_ACTIVE",
        "BIANBU_AGENT_MEMORY_ENABLED",
        "BIANBU_AGENT_MEMORY_SOCKET",
        "BIANBU_AGENT_MEMORY_ACCOUNT_ID",
        "BIANBU_AGENT_MEMORY_AGENT_NAME",
    }
    for name in reserved:
        monkeypatch.delenv(name, raising=False)
    expected = {"DECLARED_TOKEN": "allowed"}
    memory = {
        "BIANBU_AGENT_MEMORY_ENABLED": "true",
        "BIANBU_AGENT_MEMORY_SOCKET": "/run/bianbu/agent-memoryd.sock",
        "BIANBU_AGENT_MEMORY_ACCOUNT_ID": "default",
        "BIANBU_AGENT_MEMORY_AGENT_NAME": "bianbu-agent-default",
    }
    for name, value in memory.items():
        monkeypatch.setenv(name, value)
    expected.update(memory)
    if mode is not None:
        actual = {"BIANBU_RUNTIME_ROLE": "agent-worker", "BIANBU_WORKER_EFFECTIVE_MODE": mode,
                  "BIANBU_SANDBOX_ACTIVE": "1" if mode == "sandbox" else "0"}
        for name, value in actual.items():
            monkeypatch.setenv(name, value)
        expected.update(actual)
    if wrapped:
        expected.update(BIANBU_WORKER_EFFECTIVE_MODE="sandbox", BIANBU_SANDBOX_ACTIVE="1")
    monkeypatch.setattr(client_module, "_apply_mcp_sandbox",
                        lambda config: ("bwrap", ["--", config.command]) if wrapped
                        else (config.command, list(config.args)))
    monkeypatch.setenv("UNDECLARED_SECRET", "must-not-leak")
    monkeypatch.setattr(client_module, "stdio_client", transport)
    monkeypatch.setattr(client_module, "ClientSession", Session)
    config = MCPServerConfig(
        transport="stdio",
        command="server",
        env={"DECLARED_TOKEN": "allowed", **{name: "spoofed" for name in reserved}},
    )

    async with client_module._open_session(config):
        pass

    assert captured == expected


@pytest.mark.asyncio
async def test_managed_connection_retains_session_and_owns_context_in_one_task():
    import asyncio
    owners, closes = [], []
    session = _Session(result=SimpleNamespace(content=[], isError=False))
    @asynccontextmanager
    async def factory(config):
        owner = asyncio.current_task()
        owners.append(owner)
        try:
            yield session
        finally:
            assert asyncio.current_task() is owner
            closes.append(owner)
    client = MCPClient(MCPServerConfig(display_name='persistent', transport='stdio',
                                      command='/bin/false'), session_factory=factory)
    await client.start()
    await client.list_tools()
    await asyncio.create_task(client.call_tool('first', {}))
    await asyncio.create_task(client.call_tool('second', {}))
    assert session.events.count('initialize') == 1
    assert len(owners) == 1 and not closes
    await asyncio.create_task(client.close())
    assert closes == owners
    await client.close()


@pytest.mark.asyncio
async def test_persistent_stdio_connection_keeps_process_until_close(tmp_path):
    import asyncio
    import os
    import sys
    script = tmp_path / 'server.py'
    script.write_text('import os\nfrom mcp.server.fastmcp import FastMCP\n'
        'app=FastMCP("lease")\n@app.tool()\ndef pid() -> int:\n'
        '    return os.getpid()\napp.run(transport="stdio")\n')
    client = MCPClient(MCPServerConfig(transport='stdio', command=sys.executable,
                                      args=(str(script),), timeout_seconds=10))
    try:
        await client.start()
        await client.list_tools()
        first = await asyncio.create_task(client.call_tool('pid', {}))
        second = await asyncio.create_task(client.call_tool('pid', {}))
        assert first.content == second.content
        pid = int(first.structured_content['result'])
        os.kill(pid, 0)
    finally:
        await asyncio.create_task(client.close())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_persistent_start_timeout_closes_transport_without_cancelling_caller():
    import asyncio
    closed = []
    async def initialize():
        await asyncio.Event().wait()
    @asynccontextmanager
    async def factory(_config):
        try:
            yield SimpleNamespace(initialize=initialize)
        finally:
            closed.append(True)
    client = MCPClient(MCPServerConfig(transport='stdio', command='fake',
                                      timeout_seconds=.02), session_factory=factory)
    with pytest.raises(TimeoutError):
        await client.start()
    assert closed == [True]
    await asyncio.sleep(0)
    assert not asyncio.current_task().cancelling()


class _FailingSession(_Session):
    """Session whose transport is already gone when a call is attempted."""

    async def call_tool(self, name: str, arguments: dict[str, object], **_kwargs):
        self.events.append(("call_tool", name, arguments))
        raise anyio.ClosedResourceError


class _LostSession(_Session):
    """Session that accepted a request and then lost the transport."""

    async def call_tool(self, name: str, arguments: dict[str, object], **_kwargs):
        self.events.append(("call_tool", name, arguments))
        raise McpError(ErrorData(code=CONNECTION_CLOSED, message="Connection closed"))


class _DeadPingSession(_Session):
    async def send_ping(self) -> None:
        raise anyio.ClosedResourceError


def _session_sequence(*sessions):
    opened: list[_Session] = []
    remaining = list(sessions)

    @asynccontextmanager
    async def factory(_config):
        session = remaining.pop(0) if remaining else _Session(result=_echo_result())
        opened.append(session)
        yield session

    return factory, opened


def _echo_result():
    return SimpleNamespace(
        content=(_Block({"type": "text", "text": "recovered"}),),
        structuredContent=None,
        isError=False,
        meta=None,
    )


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was never satisfied")


@pytest.mark.asyncio
async def test_retries_a_request_that_never_reached_the_transport(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "_RECONNECT_DELAYS", (0.01,))
    factory, opened = _session_sequence(_FailingSession(), _Session(result=_echo_result()))
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=factory,
        keepalive_seconds=None,
    )
    try:
        await client.start()
        call = await client.call_tool("echo", {"message": "hi"})
    finally:
        await client.close()

    assert call.content == "recovered"
    assert len(opened) == 2


@pytest.mark.asyncio
async def test_does_not_replay_a_request_the_transport_already_accepted(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "_RECONNECT_DELAYS", (0.01,))
    lost = _LostSession(result=_echo_result())
    factory, opened = _session_sequence(lost, _Session(result=_echo_result()))
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=factory,
        keepalive_seconds=None,
    )
    try:
        await client.start()
        with pytest.raises(McpError):
            await client.call_tool("echo", {"message": "hi"})
        await _wait_until(lambda: len(opened) == 2)
        call = await client.call_tool("echo", {"message": "hi"})
    finally:
        await client.close()

    assert lost.events == [
        "initialize",
        ("call_tool", "echo", {"message": "hi"}),
    ]
    assert call.content == "recovered"


@pytest.mark.asyncio
async def test_keepalive_replaces_a_session_whose_child_died(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "_RECONNECT_DELAYS", (0.01,))
    factory, opened = _session_sequence(_DeadPingSession(), _Session(result=_echo_result()))
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=factory,
        keepalive_seconds=0.01,
    )
    try:
        await client.start()
        await _wait_until(lambda: len(opened) == 2)
    finally:
        await client.close()

    assert isinstance(opened[0], _DeadPingSession)


@pytest.mark.asyncio
async def test_closed_client_does_not_open_another_session(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "_RECONNECT_DELAYS", (0.01,))
    factory, opened = _session_sequence(_DeadPingSession(), _Session(result=_echo_result()))
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=factory,
        keepalive_seconds=0.01,
    )
    await client.start()
    await client.close()
    with suppress(Exception):
        await client.list_tools()

    assert len(opened) == 1


@pytest.mark.asyncio
async def test_start_reports_a_failed_first_connection_without_retrying() -> None:
    attempts: list[int] = []

    @asynccontextmanager
    async def factory(_config):
        attempts.append(1)
        raise ConnectionError("connection refused")
        yield  # pragma: no cover

    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=factory,
        keepalive_seconds=None,
    )

    with pytest.raises(ConnectionError, match="connection refused"):
        await client.start()

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_restarts_the_owner_loop_after_it_was_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(client_module, "_RECONNECT_DELAYS", (0.01,))
    factory, opened = _session_sequence(
        _Session(result=_echo_result()),
        _Session(result=_echo_result()),
    )
    client = MCPClient(
        MCPServerConfig(transport="stdio", command="server"),
        session_factory=factory,
        keepalive_seconds=None,
    )
    try:
        await client.start()
        # A transport owns an AnyIO cancel scope: when it unwinds, it cancels
        # the task holding the session instead of leaving an exception behind.
        client._owner.cancel()
        with suppress(asyncio.CancelledError):
            await client._owner

        call = await client.call_tool("echo", {"message": "hi"})
    finally:
        await client.close()

    assert call.content == "recovered"
    assert len(opened) == 2
