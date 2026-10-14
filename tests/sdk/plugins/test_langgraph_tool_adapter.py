from __future__ import annotations

import logging
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from bianbu_agent import memory_identity
from bianbu_agent import RunControl
from bianbu_agent.engines.langgraph import LangGraphEngine
from bianbu_agent.engines import encode_tool_name
from bianbu_agent.engines.langgraph.plugin_tools import build_plugin_tools
from bianbu_agent.plugins import (
    ContextPatch,
    HookContribution,
    HookPoint,
    PluginContributions,
    PluginGeneration,
    PluginRuntime,
    ToolContribution,
    ToolResult,
    ToolSpec,
)


@pytest.mark.asyncio
async def test_system_upgrade_ends_graph_without_status_polling():
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    async def handler(context, arguments):
        return ToolResult('系统更新任务已提交；升级结果待确认。')

    tool = build_plugin_tools([ToolContribution(
        'bianbu.bibit.intent',
        ToolSpec('pkg_update', 'Submit upgrade', {'type': 'object', 'properties': {}},
                 metadata={'qualified_id': 'bianbu.bibit.intent/bibit-admin/pkg_update'}),
        handler,
    )])[0]
    assert tool.return_direct
    model = Model(responses=[
        AIMessage(content='', tool_calls=[{'name': tool.name, 'args': {}, 'id': 'upgrade'}]),
        AIMessage(content='This polling turn must not run'),
    ])
    result = await create_agent(model=model, tools=[tool]).ainvoke(
        {'messages': [HumanMessage(content='升级系统')]},
        config={'configurable': {'run_id': 'run', 'session_id': 'session', 'agent_id': 'agent', 'workspace_ref': '/workspace'}},
    )
    assert model.i == 1
    assert result['messages'][-1].content == '系统更新任务已提交；升级结果待确认。'


def test_unrelated_upgrade_tool_does_not_end_graph():
    async def handler(context, arguments):
        return ToolResult('ok')

    tool = build_plugin_tools([ToolContribution(
        'other.plugin', ToolSpec('pkg_update', '', {'type': 'object'}), handler,
    )])[0]
    assert not tool.return_direct


def test_upgrade_result_uses_actual_message_not_success_claim():
    from bianbu_agent.engines.langgraph.plugin_tools import upgrade_result_message, SYSTEM_UPGRADE_TOOL

    assert upgrade_result_message(SYSTEM_UPGRADE_TOOL, '{"ok": false, "message": "需要确认"}') == '需要确认'
    assert upgrade_result_message(SYSTEM_UPGRADE_TOOL, '{"ok": true, "message": "任务已提交"}') == '任务已提交'
    assert upgrade_result_message('other/tool', 'ok') is None


@pytest.mark.asyncio
async def test_runtime_stream_stops_after_upgrade_submission():
    from bianbu_agent.engines.langgraph.plugin_tools import SYSTEM_UPGRADE_TOOL

    tool_name = encode_tool_name(SYSTEM_UPGRADE_TOOL)
    result = '{"ok":true,"message":"系统更新任务已提交，将在后台启动"}'
    tool_result = ToolMessage(content=result, name=tool_name, tool_call_id='upgrade')

    class Graph:
        def __init__(self):
            self.consumed_model_reply = False
            self.finished = False

        async def astream(self, _input, **_kwargs):
            try:
                yield AIMessage(content='', tool_calls=[
                    {'name': tool_name, 'args': {}, 'id': 'upgrade'},
                ]), {}
                yield tool_result, {}
                self.consumed_model_reply = True
                yield AIMessage(content='Polling upgrade progress'), {}
            finally:
                self.finished = True

    graph = Graph()
    events = [event async for event in LangGraphEngine(graph).stream(
        '升级系统', session_id='upgrade-session',
        control=RunControl(request_id='upgrade-run', session_id='upgrade-session'),
    )]
    assert any(event.kind == 'tool_end' for event in events)
    assert any(event.kind == 'token' and '已提交' in event.payload['text'] for event in events)
    assert not graph.consumed_model_reply
    assert graph.finished


@pytest.mark.parametrize('content', [
    '{"ok":false,"message":"需要确认降级"}',
    '{"ok":true,"message":"更新已完成"}',
    '系统更新任务已提交',
])
def test_upgrade_handoff_requires_explicit_submission(content):
    from bianbu_agent.engines.langgraph.plugin_tools import SYSTEM_UPGRADE_TOOL, upgrade_submitted

    assert not upgrade_submitted(SYSTEM_UPGRADE_TOOL, content)
    assert not upgrade_submitted('other/tool', '{"ok":true,"message":"任务已提交"}')


@pytest.mark.asyncio
async def test_tool_contribution_becomes_langchain_structured_tool() -> None:
    seen = {}

    async def handler(context, arguments):
        seen["context"] = context
        seen["arguments"] = arguments
        return ToolResult("PLUGIN_OK", metadata={"answer": 42})

    contribution = ToolContribution(
        "sample.plugin",
        ToolSpec(
            "sample_tool",
            "Sample",
            {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
        ),
        handler,
    )

    tools = build_plugin_tools([contribution])

    assert len(tools) == 1
    assert isinstance(tools[0], StructuredTool)
    assert await tools[0].ainvoke(
        {"value": 7},
        config={
            "configurable": {
                "run_id": "run",
                "session_id": "session",
                "agent_id": "agent",
                "workspace_ref": "/workspace",
            }
        },
    ) == "PLUGIN_OK"
    assert seen["arguments"] == {"value": 7}
    assert seen["context"].workspace_ref == "/workspace"
    assert tools[0].metadata["plugin_id"] == "sample.plugin"


@pytest.mark.asyncio
async def test_tool_adapter_bounds_result_before_langchain_builds_tool_message() -> None:
    async def handler(_context, _arguments):
        return ToolResult("BEGIN" + ("z" * 30_000) + "END")

    tool = build_plugin_tools(
        [
            ToolContribution(
                "sample.plugin",
                ToolSpec("sample_tool", "Sample", {"type": "object"}),
                handler,
            )
        ]
    )[0]

    content = await tool.ainvoke({})

    assert len(content) <= 16_384
    assert content.startswith("BEGIN")
    assert content.endswith("END")
    assert "[tool result truncated:" in content


def test_public_tool_spec_does_not_hold_langchain_objects() -> None:
    spec = ToolSpec("plain_tool", "Plain", {"type": "object"})
    assert not any(
        "langchain" in field.type.__class__.__module__
        for field in spec.__dataclass_fields__.values()
    )


@pytest.mark.asyncio
async def test_tool_hook_runner_receives_agent_selected_hook_ids() -> None:
    selections: list[tuple[str, ...]] = []

    class Hooks:
        async def run_hooks(self, _point, _context, *, hook_ids):
            selections.append(tuple(hook_ids))
            return ContextPatch()

    async def handler(_context, _arguments):
        return ToolResult("ok")

    tool = build_plugin_tools(
        [
            ToolContribution(
                "sample.plugin",
                ToolSpec("sample_tool", "Sample", {"type": "object"}),
                handler,
            )
        ],
        hook_runner=Hooks(),
        hook_ids=("greeting.direct-reply",),
    )[0]

    assert await tool.ainvoke({}) == "ok"
    assert selections == [
        ("greeting.direct-reply",),
        ("greeting.direct-reply",),
    ]


@pytest.mark.asyncio
async def test_tool_and_tool_hooks_receive_the_same_account_and_agent_ids() -> None:
    seen: list[tuple[str, str, str]] = []

    class Hooks:
        async def run_hooks(self, point, context, *, hook_ids):
            seen.append((point.value, context.account_id, context.agent_id))
            return ContextPatch()

    async def handler(context, _arguments):
        seen.append(("tool", context.account_id, context.agent_id))
        return ToolResult("ok")

    tool = build_plugin_tools(
        [
            ToolContribution(
                "sample.plugin",
                ToolSpec("sample_tool", "Sample", {"type": "object"}),
                handler,
            )
        ],
        hook_runner=Hooks(),
        hook_ids=(),
    )[0]

    assert await tool.ainvoke(
        {},
        config={
            "configurable": {
                "account_id": "system-user-42",
                "agent_id": "writer",
            }
        },
    ) == "ok"
    assert seen == [
        ("before_tool", "system-user-42", "writer"),
        ("tool", "system-user-42", "writer"),
        ("after_tool", "system-user-42", "writer"),
    ]


@pytest.mark.asyncio
async def test_tool_hooks_receive_tool_origin_metadata() -> None:
    seen = []

    class Hooks:
        async def run_hooks(self, point, context, *, hook_ids):
            seen.append((point.value, dict(context.metadata["tool_metadata"])))
            return ContextPatch()

    async def handler(_context, _arguments):
        return ToolResult("ok")

    tool = build_plugin_tools(
        [
            ToolContribution(
                "bianbu.memory",
                ToolSpec(
                    "memory_retrieve",
                    "Retrieve memory",
                    {"type": "object"},
                    metadata={
                        "qualified_id": "bianbu.memory/memory_retrieve",
                        "origin": "plugin",
                        "plugin_id": "bianbu.memory",
                    },
                ),
                handler,
            )
        ],
        hook_runner=Hooks(),
        hook_ids=(),
    )[0]

    assert await tool.ainvoke({}) == "ok"
    assert seen == [
        (
            "before_tool",
            {"qualified_id": "bianbu.memory/memory_retrieve",
             "origin": "plugin", "plugin_id": "bianbu.memory"},
        ),
        (
            "after_tool",
            {"qualified_id": "bianbu.memory/memory_retrieve",
             "origin": "plugin", "plugin_id": "bianbu.memory"},
        ),
    ]


@pytest.mark.asyncio
async def test_tool_adapter_rejects_an_explicit_empty_account_id() -> None:
    async def handler(_context, _arguments):
        return ToolResult("ok")

    tool = build_plugin_tools(
        [
            ToolContribution(
                "sample.plugin",
                ToolSpec("sample_tool", "Sample", {"type": "object"}),
                handler,
            )
        ]
    )[0]

    with pytest.raises(ValueError, match="account_id"):
        await tool.ainvoke(
            {}, config={"configurable": {"account_id": ""}}
        )


def _identity_tool(seen: list[tuple[str, str]]) -> StructuredTool:
    async def handler(context, _arguments):
        seen.append((context.account_id, context.agent_id))
        return ToolResult("ok")

    return build_plugin_tools(
        [
            ToolContribution(
                "sample.plugin",
                ToolSpec("sample_tool", "Sample", {"type": "object"}),
                handler,
            )
        ]
    )[0]


@pytest.mark.asyncio
async def test_tool_context_falls_back_to_the_registered_memory_identity(
    monkeypatch,
) -> None:
    """K3 2026-09-22: a placeholder identity is refused as impersonation.

    memoryd compares the caller's account/agent with the registration the Worker
    published from its environment, so a Run whose context lost the fields must
    be recovered from that same environment instead of ``default`` /
    ``default-agent``.
    """

    monkeypatch.setenv(memory_identity.ACCOUNT_ENV, "bianbu")
    monkeypatch.setenv(memory_identity.AGENT_NAME_ENV, "com.bianbu.ee2b4775b346")
    seen: list[tuple[str, str]] = []

    assert await _identity_tool(seen).ainvoke(
        {}, config={"configurable": {"run_id": "run", "session_id": "session"}}
    ) == "ok"
    assert seen == [("bianbu", "com.bianbu.ee2b4775b346")]


@pytest.mark.asyncio
async def test_tool_context_keeps_the_run_identity_when_it_carries_one(
    monkeypatch,
) -> None:
    monkeypatch.setenv(memory_identity.ACCOUNT_ENV, "registered-account")
    monkeypatch.setenv(memory_identity.AGENT_NAME_ENV, "registered-agent")
    seen: list[tuple[str, str]] = []

    assert await _identity_tool(seen).ainvoke(
        {},
        config={"configurable": {"account_id": "caller-account", "agent_id": "caller-agent"}},
    ) == "ok"
    assert seen == [("caller-account", "caller-agent")]


@pytest.mark.asyncio
async def test_tool_context_names_the_recovered_identity_field_once(
    monkeypatch, caplog
) -> None:
    monkeypatch.delenv(memory_identity.ACCOUNT_ENV, raising=False)
    monkeypatch.delenv(memory_identity.AGENT_NAME_ENV, raising=False)
    monkeypatch.setattr(
        "bianbu_agent.engines.langgraph.plugin_tools._recovered_identities", set()
    )
    seen: list[tuple[str, str]] = []
    tool = _identity_tool(seen)

    with caplog.at_level(logging.WARNING, logger="bianbu_agent.security"):
        await tool.ainvoke({}, config={"configurable": {}})
        await tool.ainvoke({}, config={"configurable": {}})

    # Without the environment a development run keeps the old placeholders ...
    assert seen == [("default", "default-agent"), ("default", "default-agent")]
    # ... but says which field had to be recovered, and only once per field.
    warnings = [
        record.getMessage()
        for record in caplog.records
        if "已回退为" in record.getMessage()
    ]
    assert len(warnings) == 2
    assert any("account_id" in message for message in warnings)
    assert any("agent_id" in message for message in warnings)
    # No environment in this test, so the log must say the variable is unset
    # rather than read as if it held the placeholder value.
    assert all("未设置" in message for message in warnings)


@pytest.mark.asyncio
async def test_generation_tool_resolves_hook_ids_from_invoking_agent() -> None:
    selections: list[tuple[str, ...]] = []

    class Hooks:
        async def run_hooks(self, _point, _context, *, hook_ids):
            selections.append(tuple(hook_ids))
            return ContextPatch()

    async def resolve(agent_id: str) -> tuple[str, ...]:
        assert agent_id == "plain-agent"
        return ()

    async def handler(_context, _arguments):
        return ToolResult("ok")

    tool = build_plugin_tools(
        [
            ToolContribution(
                "sample.plugin",
                ToolSpec("sample_tool", "Sample", {"type": "object"}),
                handler,
            )
        ],
        hook_runner=Hooks(),
        hook_ids_resolver=resolve,
    )[0]

    assert await tool.ainvoke(
        {}, config={"configurable": {"agent_id": "plain-agent"}}
    ) == "ok"
    assert selections == [(), ()]


@pytest.mark.asyncio
async def test_shared_tool_adapter_prefers_run_hook_snapshot_for_nested_tool_hooks() -> None:
    """A later Agent edit must not alter a nested Tool call's Hook boundary."""

    calls: list[str] = []

    def hook(hook_id: str, point: HookPoint) -> HookContribution:
        async def handler(_context):
            calls.append(hook_id)
            return ContextPatch()

        return HookContribution(
            "vendor.hooks",
            point,
            handler,
            id=hook_id,
            metadata={"qualified_id": f"vendor.hooks/{hook_id}"},
        )

    old_before = "old.before_tool"
    old_after = "old.after_tool"
    generation = PluginGeneration(
        id=1,
        plugins={
            "vendor.hooks": PluginRuntime(
                plugin_id="vendor.hooks",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(
                        hook(old_before, HookPoint.BEFORE_TOOL),
                        hook(old_after, HookPoint.AFTER_TOOL),
                        hook("new.before_tool", HookPoint.BEFORE_TOOL),
                        hook("new.after_tool", HookPoint.AFTER_TOOL),
                    )
                ),
            )
        },
        statuses={},
    )

    async def edited_agent_hooks(_agent_id: str) -> tuple[str, ...]:
        raise AssertionError("a nested call must use the Run snapshot, not re-read Agent")

    async def handler(_context, _arguments):
        return ToolResult("ok")

    tool = build_plugin_tools(
        [
            ToolContribution(
                "vendor.tool",
                ToolSpec("tool", "Tool", {"type": "object"}),
                handler,
            )
        ],
        hook_runner=generation,
        hook_ids_resolver=edited_agent_hooks,
    )[0]

    assert await tool.ainvoke(
        {},
        config={
            "configurable": {
                "run_id": "paused-run",
                "session_id": "session",
                "agent_id": "edited-agent",
                "hook_ids": (
                    "vendor.hooks/old.before_tool",
                    "vendor.hooks/old.after_tool",
                ),
            }
        },
    ) == "ok"
    assert calls == [old_before, old_after]


@pytest.mark.asyncio
async def test_tool_hook_context_reaches_tool_result_without_changing_arguments():
    seen = []
    async def handler(_, args):
        seen.append(args)
        return ToolResult('original')
    class Hooks:
        async def run_hooks(self, point, context, **kwargs):
            return ContextPatch(context_sections=(point.value,))
    tool = build_plugin_tools([ToolContribution('example.plugin',
        ToolSpec('echo', 'Echo', {'type':'object','properties':{'value':{'type':'string'}}}), handler)], hook_runner=Hooks())[0]
    result = await tool.ainvoke({'value':'unchanged'})
    assert result == 'original\n\nHook context:\nbefore_tool\n\nafter_tool'
    assert seen == [{'value':'unchanged'}]


@pytest.mark.asyncio
async def test_failed_tool_call_drops_after_tool_hook_context():
    """调用自己失败时，事后复核的结论不能贴到结果里。

    K3 实测：`appcli_run` 参数错误 → `{"ok": false, "message": "参数错误…"}`，
    后面却跟着「Hook context: 独立复核未通过」，模型把参数错误读成复核结论。
    Hook 仍然执行（审计/状态照旧），只是不让它改写工具结果。
    """
    ran = []

    async def handler(_, args):
        return ToolResult('参数错误', metadata={'mcp_error': True})

    class Hooks:
        async def run_hooks(self, point, context, **kwargs):
            ran.append(point.value)
            return ContextPatch(context_sections=(point.value,))

    tool = build_plugin_tools(
        [ToolContribution('example.plugin', ToolSpec(
            'run',
            'Run',
            {'type': 'object', 'properties': {'value': {'type': 'string'}}},
        ), handler)],
        hook_runner=Hooks(),
    )[0]

    result = await tool.ainvoke({'value': 'unchanged'})

    assert result == '参数错误\n\nHook context:\nbefore_tool'
    assert ran == ['before_tool', 'after_tool']


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['mask', 'block', 'allow'])
async def test_tool_result_dlp_is_applied_before_return_to_model(action):
    raw = '手机号：13824681357'
    masked = '手机号：[PII-phone-001]'
    seen = []

    async def handler(_context, _arguments):
        return ToolResult(raw)

    class Hooks:
        async def run_hooks(self, point, context, **kwargs):
            if point != HookPoint.AFTER_TOOL:
                return ContextPatch()
            seen.append(context.metadata.get('result'))
            payload = ({'dlp_masked_result': masked} if action == 'mask' else
                       {'result_blocked': True, 'block_reason': '受限敏感信息'} if action == 'block' else {})
            return ContextPatch(metadata={'bianbu.security': {'securityd': payload}})

    tool = build_plugin_tools([ToolContribution('sample', ToolSpec(
        'read', 'Read', {'type': 'object'}), handler)], hook_runner=Hooks())[0]
    if action == 'block':
        with pytest.raises(PermissionError, match='受限敏感信息') as caught:
            await tool.ainvoke({})
        assert caught.value.should_terminate_run
    else:
        assert await tool.ainvoke({}) == (masked if action == 'mask' else raw)
    assert seen == [raw], 'post_tool_call must inspect the actual result'
