import asyncio
import math

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from bianbu_agent import RunControl
from bianbu_agent.engines.langgraph import LangGraphEngine, build_langgraph_agent


class ToolLoopModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_real_agent_exceeds_fifty_tool_steps_and_still_finishes_or_cancels(cancel, monkeypatch):
    monkeypatch.setenv("BIANBU_RUNTIME_SECURITY", "optional")
    monkeypatch.setattr("bianbu_agent.engines.langgraph.engine._security_middleware_enabled", lambda: False)
    control = RunControl(request_id="long-run", session_id="session")
    calls = []

    async def step() -> str:
        """Perform one bounded work item."""
        calls.append(len(calls))
        if cancel and len(calls) == 55:
            await control.cancel()
        return "done"

    responses = [AIMessage(content="", tool_calls=[{"id": f"step-{i}", "name": "step", "args": {}}]) for i in range(60)]
    responses.append(AIMessage(content="finished all work"))
    graph = build_langgraph_agent(system_prompt="Finish work", model=ToolLoopModel(responses=responses), tools=[StructuredTool.from_function(coroutine=step)])
    assert math.isinf(graph.config["recursion_limit"])
    async with asyncio.timeout(15):
        events = [event async for event in LangGraphEngine(graph).stream("do work", session_id="thread", control=control)]
    assert len(calls) >= 55
    if cancel:
        assert control.cancelled.is_set()
        assert len(calls) < 60
    else:
        assert len(calls) == 60
        assert any(event.payload.get("text") == "finished all work" for event in events)


def test_sync_child_runtime_finishes_sixty_tool_calls(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from bianbu_agent.tools.runtime_services import RuntimeServices
    monkeypatch.setattr("bianbu_agent.engines.langgraph.engine._security_middleware_enabled", lambda: False)
    calls = []
    def step() -> str:
        """Perform one bounded child work item."""
        calls.append(len(calls))
        return "done"
    responses = [AIMessage(content="", tool_calls=[{"id": f"child-{i}", "name": "step", "args": {}}]) for i in range(60)]
    responses.append(AIMessage(content="child finished"))
    services = SimpleNamespace(runtime_home=tmp_path, model_provider_id="", capture_callback=None)
    result = RuntimeServices.run_child_with_snapshot(
        services, ToolLoopModel(responses=responses), [StructuredTool.from_function(step)],
        "session", "complete all work", "Work until complete", "child",
    )
    assert result == "child finished"
    assert len(calls) == 60
