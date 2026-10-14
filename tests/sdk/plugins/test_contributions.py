from __future__ import annotations

import dataclasses

import pytest

from bianbu_agent.plugins import (
    ContextPatch,
    HookContribution,
    HookPoint,
    MCPRuntimeContribution,
    PluginContributions,
    RunContext,
    SkillContribution,
    SkillSpec,
    ToolCallContext,
    ToolContribution,
    ToolResult,
    ToolSpec,
)


async def _tool_handler(
    context: ToolCallContext,
    arguments: dict[str, object],
) -> ToolResult:
    return ToolResult(content=f"{context.run_id}:{arguments['value']}")


async def _hook_handler(context: RunContext) -> ContextPatch:
    return ContextPatch(context_sections=(context.request,))


def test_tool_and_skill_specs_are_normalized_framework_neutral_and_immutable() -> None:
    tool = ToolSpec(
        name=" read_file ",
        description=" Read ",
        input_schema={"type": "object"},
        metadata={"risk_level": "L1"},
    )
    skill = SkillSpec(
        name=" research ",
        description=" Research ",
        instructions=" Use primary sources. ",
        resources={"guide.md": "resource://guide"},
    )

    assert tool.name == "read_file"
    assert skill.instructions == "Use primary sources."
    assert not any(
        "langchain" in field.type.__class__.__module__.lower()
        or "langgraph" in field.type.__class__.__module__.lower()
        for field in dataclasses.fields(tool)
    )
    with pytest.raises(TypeError):
        tool.metadata["risk_level"] = "L4"
    with pytest.raises(TypeError):
        skill.resources["other"] = "x"


def test_contribution_collection_is_immutable_and_rejects_invalid_names() -> None:
    tool = ToolContribution(
        plugin_id="bianbu.tools.files",
        spec=ToolSpec(
            name="read_file",
            description="Read",
            input_schema={"type": "object"},
        ),
        handler=_tool_handler,
    )
    skill = SkillContribution(
        plugin_id="bianbu.skills.research",
        spec=SkillSpec(name="research", description="Research"),
    )
    hook = HookContribution(
        plugin_id="bianbu.hooks.context",
        point=HookPoint.AFTER_CONTEXT,
        handler=_hook_handler,
        priority=20,
        timeout_seconds=1.5,
    )
    mcp_runtime = MCPRuntimeContribution(
        plugin_id="bianbu.mcp.example",
        mcp_id="example",
        runtime_id="bianbu.mcp.example/example",
        display_name="Example",
        transport="stdio",
        command="example",
    )
    contributions = PluginContributions(
        hooks=(hook,),
        tools=(tool,),
        skills=(skill,),
        mcp_runtime=mcp_runtime,
    )

    assert contributions.tools == (tool,)
    assert contributions.skills == (skill,)
    assert contributions.hooks == (hook,)
    assert contributions.mcp_runtime == mcp_runtime
    with pytest.raises(ValueError, match="tool name"):
        ToolSpec(name="Bad Tool", description="bad", input_schema={})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        (42, "42"),
    ],
)
def test_context_patch_normalizes_direct_reply(value: object, expected: str | None) -> None:
    assert ContextPatch(direct_reply=value).direct_reply == expected


def test_run_and_tool_contexts_expose_default_and_explicit_accounts() -> None:
    default_run = RunContext("run", "session", "agent", "request")
    default_call = ToolCallContext("run", "session", "agent")
    system_run = RunContext(
        "run", "session", "agent", "request", account_id="system-user-42"
    )
    system_call = ToolCallContext(
        "run", "session", "agent", account_id="system-user-42"
    )

    assert default_run.account_id == "default"
    assert default_call.account_id == "default"
    assert system_run.account_id == "system-user-42"
    assert system_call.account_id == "system-user-42"

    with pytest.raises(ValueError, match="account_id"):
        RunContext("run", "session", "agent", "request", account_id=" ")
    with pytest.raises(ValueError, match="account_id"):
        ToolCallContext("run", "session", "agent", account_id=" ")


@pytest.mark.asyncio
async def test_handlers_use_only_sdk_owned_context_and_result_types() -> None:
    tool_context = ToolCallContext(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        workspace_ref="workspace:default",
    )
    result = await _tool_handler(tool_context, {"value": "ok"})
    patch = await _hook_handler(
        RunContext(
            run_id="run-1",
            session_id="session-1",
            agent_id="agent-1",
            request="hello",
        )
    )

    assert result.content == "run-1:ok"
    assert patch.context_sections == ("hello",)
