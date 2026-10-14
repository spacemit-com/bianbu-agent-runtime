"""J12 交付接口：MCP 注解 → 审批、MCP 进度 → 运行事件、取消语义。

对应 docs/j12-riscv-delivery-runtime-interfaces.md 的 R1/R2/R3：
riscv-software-delivery Skill 要求 Runtime 能用服务端自述的破坏性提示做审批，
并把长耗时安装的真实进度透出；两者都不能靠 Runtime 硬编码工具名。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from bianbu_agent import RunControl
from bianbu_agent.engines import encode_tool_name
from bianbu_agent.engines.langgraph import LangGraphEngine, build_langgraph_agent
from bianbu_agent.engines.langgraph.plugin_tools import build_plugin_tools
from bianbu_agent.mcp.client import MCPCallResult, MCPRemoteTool, _annotation_flags
from bianbu_agent.mcp.plugin import MCPDirectoryPlugin, _annotation_metadata
from bianbu_agent.plugins import PluginContext
from bianbu_agent.plugins.contributions import ToolCallContext
from bianbu_agent.plugins.models import PluginManifest
from tests.sdk._approval import stream_answering_approvals


class _ToolLoopModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class _Client:
    """最小 MCP 客户端替身：可声明注解，也可在调用中回报进度。"""

    def __init__(
        self,
        tools: tuple[MCPRemoteTool, ...],
        *,
        progress: tuple[float, float | None, str | None] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.tools = tools
        self.progress = progress
        self.error = error
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.calls_meta: list[object] = []

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_tools(self) -> tuple[MCPRemoteTool, ...]:
        return self.tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        progress_callback=None,
        **_kwargs: object,
    ) -> MCPCallResult:
        self.calls.append((name, dict(arguments)))
        self.calls_meta.append(_kwargs.get("meta"))
        if self.error is not None:
            raise self.error
        if progress_callback is not None and self.progress is not None:
            await progress_callback(*self.progress)
        return MCPCallResult("installed")


def _manifest() -> PluginManifest:
    return PluginManifest(
        id="acme.filesystem",
        version="0.0.0",
    )


async def _contribution(tmp_path: Path, client: _Client, remote: MCPRemoteTool):
    root = tmp_path / "acme.filesystem"
    root.mkdir(exist_ok=True)
    (root / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": "fake"}),
        encoding="utf-8",
    )
    plugin = MCPDirectoryPlugin(
        _manifest(),
        root=root,
        workspace=tmp_path / "workspace",
        mcp_id="acme.filesystem",
        client_factory=lambda _config: client,
    )
    context = PluginContext(plugin_id="acme.filesystem", data_dir=tmp_path / "data")
    await plugin.setup(context)
    assert context.scope is not None
    contributions = context.scope.snapshot().tools
    assert len(contributions) == 1
    return contributions[0]


# ── R1 注解 → 审批元数据 ────────────────────────────────────────────

def test_annotation_flags_accept_camel_and_snake_names() -> None:
    from types import SimpleNamespace

    assert _annotation_flags(
        SimpleNamespace(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    ) == {
        "read_only": True,
        "destructive": False,
        "idempotent": True,
        "open_world": False,
    }
    assert _annotation_flags({"read_only_hint": True, "destructive_hint": True}) == {
        "read_only": True,
        "destructive": True,
    }


def test_annotation_flags_drop_unknown_and_non_boolean_values() -> None:
    assert _annotation_flags({"bogus": 1, "destructive_hint": "yes"}) == {}
    assert _annotation_flags(None) == {}


def test_destructive_annotation_maps_to_requires_approval() -> None:
    # 未声明注解：不产生任何字段，行为与接入注解前完全一致
    assert _annotation_metadata({}) == {}

    flags = _annotation_metadata({"destructive": True})
    assert flags["requires_approval"] is True
    assert flags["destructive"] is True

    # 只读工具不进入静态保护；服务端显式声明过才写 read_only
    read_only = _annotation_metadata({"read_only": True})
    assert read_only["read_only"] is True
    assert "requires_approval" not in read_only

    # readOnlyHint 优先于 destructiveHint（只读工具本就不可破坏）
    both = _annotation_metadata({"read_only": True, "destructive": True})
    assert both["requires_approval"] is False
    assert both["mcp_annotations"] == {"read_only": True, "destructive": True}


@pytest.mark.asyncio
async def test_plugin_metadata_carries_server_annotations(tmp_path: Path) -> None:
    remote = MCPRemoteTool(
        "install",
        "Install",
        "Install a package",
        {"type": "object", "properties": {}},
        {"destructive": True},
    )
    contribution = await _contribution(tmp_path, _Client((remote,)), remote)

    metadata = contribution.spec.metadata
    assert metadata["requires_approval"] is True
    assert metadata["destructive"] is True
    assert metadata["mcp_annotations"] == {"destructive": True}
    # 服务端没声明 readOnlyHint 时不臆造只读性
    assert "read_only" not in metadata


@pytest.mark.asyncio
@pytest.mark.usefixtures("allow_model_dlp")
async def test_graph_requires_approval_for_destructive_mcp_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    remote = MCPRemoteTool(
        "install",
        "Install",
        "Install a package",
        {"type": "object", "properties": {}},
        {"destructive": True},
    )
    client = _Client((remote,))
    contribution = await _contribution(tmp_path, client, remote)
    tool = build_plugin_tools([contribution])[0]
    model = _ToolLoopModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "id": "call-install",
                    "name": tool.name,
                    "args": {},
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="install",
        tools=[tool],
    )

    events = await stream_answering_approvals(
        LangGraphEngine(graph),
        "install it",
        session_id="thread-annotations",
        control=RunControl(
            request_id="run-annotations",
            session_id="thread-annotations",
        ),
        answer=lambda payload: [{"type": "reject"}],
    )

    assert any(event.kind == "approval_required" for event in events)
    assert client.calls == []


@pytest.mark.asyncio
async def test_graph_requires_approval_for_destructive_mcp_tool_without_security(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BIANBU_SECURITY=0 时服务端自述的破坏性提示仍必须触发审批。

    关掉 securityd 曾经把 ``destructiveHint`` 一起静默丢掉：K3/10.0.91.72 的
    源部署服务（BIANBU_SECURITY=0）里 appcli_run 直接执行、不弹审批，而同一轮
    的 terminal 又照常弹 —— 用户看到的是「一部分工具确认、一部分工具偷偷跑」。
    审批是工具契约，不是 securityd 的能力开关。
    """
    monkeypatch.setenv("BIANBU_SECURITY", "0")
    remote = MCPRemoteTool(
        "install",
        "Install",
        "Install a package",
        {"type": "object", "properties": {}},
        {"destructive": True},
    )
    client = _Client((remote,))
    contribution = await _contribution(tmp_path, client, remote)
    tool = build_plugin_tools([contribution])[0]
    model = _ToolLoopModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "id": "call-install-no-security",
                    "name": tool.name,
                    "args": {},
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="install",
        tools=[tool],
    )

    events = await stream_answering_approvals(
        LangGraphEngine(graph),
        "install it",
        session_id="thread-annotations-no-security",
        control=RunControl(
            request_id="run-annotations-no-security",
            session_id="thread-annotations-no-security",
        ),
        answer=lambda payload: [{"type": "reject"}],
    )

    assert any(event.kind == "approval_required" for event in events)
    assert client.calls == []


@pytest.mark.asyncio
async def test_read_only_mcp_tool_stays_unattended_without_security(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只读工具不该因为关掉 securityd 就凭空多出审批。"""
    monkeypatch.setenv("BIANBU_SECURITY", "0")
    remote = MCPRemoteTool(
        "list",
        "List",
        "List packages",
        {"type": "object", "properties": {}},
        {"read_only": True},
    )
    client = _Client((remote,))
    contribution = await _contribution(tmp_path, client, remote)
    tool = build_plugin_tools([contribution])[0]
    model = _ToolLoopModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "id": "call-list-no-security",
                    "name": tool.name,
                    "args": {},
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="list",
        tools=[tool],
    )

    events = [
        event
        async for event in LangGraphEngine(graph).stream(
            "list them",
            session_id="thread-readonly-no-security",
            control=RunControl(
                request_id="run-readonly-no-security",
                session_id="thread-readonly-no-security",
            ),
        )
    ]

    assert not any(event.kind == "approval_required" for event in events)
    assert [name for name, _ in client.calls] == [remote.name]


# ── R2 MCP 进度 → 运行事件 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_progress_reaches_run_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIANBU_SECURITY", "0")
    remote = MCPRemoteTool(
        "install",
        "Install",
        "Install a package",
        {"type": "object", "properties": {}},
        {"read_only": False},
    )
    client = _Client((remote,), progress=(2.0, 3.0, "installing six"))
    contribution = await _contribution(tmp_path, client, remote)
    tool = build_plugin_tools([contribution])[0]
    model = _ToolLoopModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "id": "call-progress",
                    "name": tool.name,
                    "args": {},
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="install",
        tools=[tool],
    )

    events = [
        event
        async for event in LangGraphEngine(graph).stream(
            "install it",
            session_id="thread-progress",
            control=RunControl(request_id="run-progress", session_id="thread-progress"),
        )
    ]

    assert client.calls == [("install", {})]
    forwarded = [
        event.payload
        for event in events
        if event.kind == "progress"
        and event.payload.get("phase") == "tool_progress"
    ]
    assert forwarded == [{
        "phase": "tool_progress",
        "tool": contribution.spec.metadata["qualified_id"],
        "progress": 2.0,
        "total": 3.0,
        "message": "installing six",
    }]


@pytest.mark.asyncio
async def test_progress_without_total_omits_the_denominator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIANBU_SECURITY", "0")
    remote = MCPRemoteTool(
        "install",
        "Install",
        "Install a package",
        {"type": "object", "properties": {}},
        {"read_only": False},
    )
    client = _Client((remote,), progress=(1.0, None, None))
    contribution = await _contribution(tmp_path, client, remote)
    tool = build_plugin_tools([contribution])[0]
    model = _ToolLoopModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "id": "call-progress-2",
                    "name": tool.name,
                    "args": {},
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="install",
        tools=[tool],
    )

    events = [
        event
        async for event in LangGraphEngine(graph).stream(
            "install it",
            session_id="thread-progress-2",
            control=RunControl(request_id="run-progress-2", session_id="thread-progress-2"),
        )
    ]

    payloads = [
        event.payload
        for event in events
        if event.kind == "progress"
        and event.payload.get("phase") == "tool_progress"
    ]
    assert len(payloads) == 1
    assert "total" not in payloads[0]
    assert "message" not in payloads[0]


async def test_tool_progress_sink_absent_outside_a_graph() -> None:
    """不在图内执行时进度静默丢弃，工具本身不能因此失败。"""

    from bianbu_agent.engines.langgraph.plugin_tools import _stream_progress_sink

    _stream_progress_sink({"phase": "tool_progress"})


# ── R3 取消语义 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancelled_mcp_call_is_not_reported_as_connection_error(
    tmp_path: Path,
) -> None:
    remote = MCPRemoteTool(
        "install",
        "Install",
        "Install a package",
        {"type": "object", "properties": {}},
        {},
    )
    client = _Client((remote,), error=asyncio.CancelledError())
    contribution = await _contribution(tmp_path, client, remote)

    with pytest.raises(asyncio.CancelledError):
        await contribution.handler(ToolCallContext("run-cancel", "session", "agent"), {})
