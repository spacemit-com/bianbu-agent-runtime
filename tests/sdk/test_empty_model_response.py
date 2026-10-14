from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from bianbu_agent.engines.langgraph import build_langgraph_agent


class Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.asyncio
async def test_empty_reasoning_response_retries_then_continues_tool_loop():
    calls = []

    @tool
    def compile_project() -> str:
        """Compile the project."""
        calls.append('compiled')
        return 'passed'

    model = Model(responses=[
        AIMessage(content='', additional_kwargs={'reasoning_content': 'unfinished plan'}),
        AIMessage(content='', tool_calls=[{'name': 'compile_project', 'args': {}, 'id': 'compile'}]),
        AIMessage(content='Verified'),
    ])
    graph = build_langgraph_agent(model=model, system_prompt='', tools=[compile_project])
    result = await graph.ainvoke({'messages': [HumanMessage(content='Build it')]})
    assert calls == ['compiled']
    assert result['messages'][-1].content == 'Verified'
    assert len([m for m in result['messages'] if isinstance(m, HumanMessage)]) == 1
    assert not any(m.additional_kwargs.get('reasoning_content') == 'unfinished plan' for m in result['messages'])


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.asyncio
async def test_persistently_empty_response_is_failure_not_success(asynchronous):
    model = Model(responses=[AIMessage(content='')])
    graph = build_langgraph_agent(model=model, system_prompt='')
    with pytest.raises(RuntimeError, match='model returned no answer or tool calls'):
        if asynchronous:
            await graph.ainvoke({'messages': [HumanMessage(content='Build it')]})
        else:
            graph.invoke({'messages': [HumanMessage(content='Build it')]})


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [False, True])
async def test_non_actionable_response_retries_before_graph_ends(invalid):
    first = (AIMessage(content='', invalid_tool_calls=[{'name': 'write_file', 'args': '{', 'id': 'bad', 'error': 'invalid json'}])
             if invalid else AIMessage(content='<think>unfinished plan</think>'))
    model = Model(responses=[first, AIMessage(content='Retry worked')])
    graph = build_langgraph_agent(model=model, system_prompt='')
    result = await graph.ainvoke({'messages': [HumanMessage(content='Build it')]},
                                config={'metadata': {'provider_id': 'minimax'}})
    assert result['messages'][-1].content == 'Retry worked'
