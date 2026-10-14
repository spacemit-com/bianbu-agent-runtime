from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from bianbu_agent import RunControl
from bianbu_agent.engines.langgraph import LangGraphEngine, build_langgraph_agent


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
@pytest.mark.parametrize("content", ["", [{"type": "text", "text": "  "}],
                                      [{"type": "output_text", "text": ""}]])
async def test_reasoning_only_reply_retries_without_false_completion(recover, content):
    empty = AIMessage(content=content, additional_kwargs={"reasoning_content": "planning"})
    model = Model(responses=[empty, AIMessage(content="ready") if recover else empty])
    engine = LangGraphEngine(build_langgraph_agent(
        model=model, system_prompt="Test"))
    async def collect():
        return [e async for e in engine.stream("create chess", session_id="empty",
            control=RunControl(request_id="empty", session_id="empty"))]
    if recover:
        events = await collect()
        assert [
            e.payload["text"] for e in events if e.kind == "token"
        ][-1] == "ready"
    else:
        with pytest.raises(RuntimeError, match="no answer or tool calls"):
            await collect()


def test_sync_reasoning_only_reply_recovers():
    model = Model(responses=[
        AIMessage(content="", additional_kwargs={"reasoning_content": "planning"}),
        AIMessage(content="ready"),
    ])
    graph = build_langgraph_agent(model=model, system_prompt="Test")
    result = graph.invoke({"messages": [{"role": "user", "content": "create chess"}]})
    assert result["messages"][-1].content == "ready"


@pytest.mark.parametrize("model_name,finish", [
    ("deepseek-v4-flash", "length"),
    ("deepseek-flash", "length"),
    ("deepseek-v4-flash", "stop"),
    ("other-model", "length"),
])
def test_retry_preserves_explicit_model_settings(model_name, finish):
    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain_openai import ChatOpenAI
    from bianbu_agent.engines.langgraph.model_output import ModelOutputMiddleware

    request = ModelRequest(model=ChatOpenAI(model=model_name, api_key="test"), messages=[],
                           model_settings={"max_tokens": 8192, "reasoning_effort": "high"})
    seen = []
    def handler(current):
        seen.append(current)
        return ModelResponse(result=[AIMessage(content="" if len(seen) == 1 else "ready",
            additional_kwargs={"reasoning_content": "planning"},
            response_metadata={"finish_reason": finish})])
    ModelOutputMiddleware().wrap_model_call(request, handler)
    assert seen[1].model_settings == {"max_tokens": 8192, "reasoning_effort": "high"}
    assert request.model_settings == {"max_tokens": 8192, "reasoning_effort": "high"}


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.asyncio
async def test_exhausted_reasoning_error_reports_budget_without_disclosing_reasoning(async_mode):
    from langchain.agents.middleware import ModelRequest, ModelResponse
    from bianbu_agent.engines.langgraph.model_output import ModelOutputMiddleware

    empty = AIMessage(content="", additional_kwargs={"reasoning_content": "private planning"},
        response_metadata={"finish_reason": "length"},
        usage_metadata={"input_tokens": 30000, "output_tokens": 8192, "total_tokens": 38192})
    request = ModelRequest(model=Model(responses=[empty]), messages=[])
    def handler(current):
        return ModelResponse(result=[empty])
    async def ahandler(current):
        return handler(current)
    with pytest.raises(RuntimeError) as caught:
        if async_mode:
            await ModelOutputMiddleware().awrap_model_call(request, ahandler)
        else:
            ModelOutputMiddleware().wrap_model_call(request, handler)
    message = str(caught.value)
    assert "finish_reason=length" in message
    assert "output_tokens=8192" in message
    assert "reasoning" in message
    assert "private planning" not in message


@pytest.mark.asyncio
async def test_recovery_strategy_persists_after_tool_progress_and_isolated_between_runs():
    from pydantic import Field
    from langchain_core.tools import tool

    class RecordingModel(Model):
        requests: list = Field(default_factory=list)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.requests.append(list(messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    effects = []

    @tool
    def save_step() -> str:
        """Record one completed implementation step."""
        effects.append('saved')
        return 'step saved'

    model = RecordingModel(responses=[
        AIMessage(content='', additional_kwargs={'reasoning_content': 'planning'}),
        AIMessage(content='', tool_calls=[{'id': 'saved-once', 'name': 'save_step', 'args': {}}]),
        AIMessage(content='done'), AIMessage(content='new run'),
    ])
    engine = LangGraphEngine(build_langgraph_agent(model=model, system_prompt='Original instructions',
        tools=[save_step]))
    events = [e async for e in engine.stream('create app', session_id='sticky',
        control=RunControl(request_id='first', session_id='sticky'))]
    assert effects == ['saved']
    assert model.requests[1][0].content != model.requests[0][0].content
    assert model.requests[2][0].content == model.requests[1][0].content
    assert any(e.kind == 'progress' and e.payload.get('phase') == 'recovering' for e in events)
    [e async for e in engine.stream('hello', session_id='fresh',
        control=RunControl(request_id='second', session_id='fresh'))]
    assert model.requests[3][0].content == model.requests[0][0].content


@pytest.mark.asyncio
async def test_recovery_budget_is_not_reset_by_completed_tool_calls():
    from langchain_core.tools import tool

    effects = []

    @tool
    def save_step() -> str:
        """Save one step without replaying previous effects."""
        effects.append('saved')
        return 'saved'

    empty = AIMessage(content='')
    model = Model(responses=[empty,
        AIMessage(content='', tool_calls=[{'id': 'one', 'name': 'save_step', 'args': {}}]), empty,
        AIMessage(content='', tool_calls=[{'id': 'two', 'name': 'save_step', 'args': {}}]), empty])
    engine = LangGraphEngine(build_langgraph_agent(model=model, system_prompt='Test',
        tools=[save_step]))
    events = []
    with pytest.raises(RuntimeError, match='recovery exhausted'):
        async for event in engine.stream('create app', session_id='bounded',
                control=RunControl(request_id='bounded', session_id='bounded')):
            events.append(event)
    assert effects == ['saved', 'saved']
    assert len([e for e in events if e.kind == 'progress']) == 2
    assert [
        e.payload['call_id'] for e in events if e.kind == 'tool_start'
    ] == ['one', 'two']
