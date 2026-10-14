from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    ToolMessage,
)
from langchain_core.tools import tool
from bianbu_agent import RunControl, RunEvent
from bianbu_agent.engines.langgraph import LangGraphEngine, build_langgraph_agent
from bianbu_agent.sessions.events import SessionEvent


class _ChunkGraph:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def astream(
        self,
        _agent_input: object,
        **_kwargs: Any,
    ) -> AsyncIterator[tuple[AIMessageChunk, dict[str, object]]]:
        for index, content in enumerate(self._chunks):
            yield (
                AIMessageChunk(
                    content=content,
                    chunk_position=(
                        "last" if index == len(self._chunks) - 1 else None
                    ),
                ),
                {},
            )


class _StalledChunkGraph:
    def __init__(self, content: str) -> None:
        self._content = content
        self.waiting = asyncio.Event()

    async def astream(
        self,
        _agent_input: object,
        **_kwargs: Any,
    ) -> AsyncIterator[tuple[AIMessageChunk, dict[str, object]]]:
        yield AIMessageChunk(content=self._content), {}
        self.waiting.set()
        await asyncio.Event().wait()


class _MessageGraph:
    def __init__(
        self,
        messages: list[AIMessage | AIMessageChunk | ToolMessage],
    ) -> None:
        self._messages = messages

    async def astream(
        self,
        _agent_input: object,
        **_kwargs: Any,
    ) -> AsyncIterator[
        tuple[AIMessage | AIMessageChunk | ToolMessage, dict[str, object]]
    ]:
        for message in self._messages:
            yield message, {}


async def _engine_events(
    graph: object,
    *,
    provider_id: str = "minimax",
) -> list[RunEvent]:
    engine = LangGraphEngine(graph, provider_id=provider_id)
    return [
        event
        async for event in engine.stream(
            None,
            session_id="thread-minimax-events",
            control=RunControl(
                request_id="run-minimax-events",
                session_id="session-minimax-events",
            ),
        )
    ]


async def _stream(chunks: list[str], *, provider_id: str) -> list[tuple[str, str]]:
    engine = LangGraphEngine(_ChunkGraph(chunks), provider_id=provider_id)
    events = [
        event
        async for event in engine.stream(
            None,
            session_id="thread-minimax-think",
            control=RunControl(
                request_id="run-minimax-think",
                session_id="session-minimax-think",
            ),
        )
    ]
    return [
        (event.kind, str(event.payload.get("text") or ""))
        for event in events
        if event.kind in {"reasoning", "token"}
    ]


@pytest.mark.asyncio
async def test_minimax_think_tags_split_across_chunks_become_reasoning() -> None:
    events = await _stream(
        ["<thi", "nk>plan", " step</th", "ink>\n\nanswer"],
        provider_id="minimax",
    )

    assert events == [
        ("reasoning", "plan"),
        ("reasoning", " step"),
        ("token", "\n\nanswer"),
    ]


@pytest.mark.asyncio
async def test_minimax_plain_text_streams_without_added_latency() -> None:
    events = await _stream(
        ["ordinary ", "answer"],
        provider_id="minimax",
    )

    assert events == [("token", "ordinary "), ("token", "answer")]


@pytest.mark.asyncio
async def test_deepseek_reasoning_content_from_response_metadata_is_streamed() -> None:
    class _ReasoningGraph:
        async def astream(
            self,
            _agent_input: object,
            **_kwargs: Any,
        ) -> AsyncIterator[tuple[AIMessageChunk, dict[str, object]]]:
            yield (
                AIMessageChunk(
                    content="",
                    response_metadata={"reasoning_content": "plan"},
                ),
                {},
            )
            yield (
                AIMessageChunk(
                    content="answer",
                    chunk_position="last",
                ),
                {},
            )

    engine = LangGraphEngine(_ReasoningGraph(), provider_id="deepseek")
    events = [
        event
        async for event in engine.stream(
            None,
            session_id="thread-deepseek-think",
            control=RunControl(
                request_id="run-deepseek-think",
                session_id="session-deepseek-think",
            ),
        )
        if event.kind in {"reasoning", "token"}
    ]

    assert [(event.kind, event.payload.get("text")) for event in events] == [
        ("reasoning", "plan"),
        ("token", "answer"),
    ]


@pytest.mark.asyncio
async def test_deepseek_reasoning_content_block_is_streamed() -> None:
    class _ReasoningBlockGraph:
        async def astream(
            self,
            _agent_input: object,
            **_kwargs: Any,
        ) -> AsyncIterator[tuple[AIMessageChunk, dict[str, object]]]:
            yield (
                AIMessageChunk(
                    content=[{"type": "reasoning", "reasoning": "plan"}],
                ),
                {},
            )
            yield (
                AIMessageChunk(content="answer", chunk_position="last"),
                {},
            )

    engine = LangGraphEngine(_ReasoningBlockGraph(), provider_id="deepseek")
    events = [
        event
        async for event in engine.stream(
            None,
            session_id="thread-deepseek-block-think",
            control=RunControl(
                request_id="run-deepseek-block-think",
                session_id="session-deepseek-block-think",
            ),
        )
        if event.kind in {"reasoning", "token"}
    ]

    assert [(event.kind, event.payload.get("text")) for event in events] == [
        ("reasoning", "plan"),
        ("token", "answer"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "ß<think>secret</think>answer",
            [("token", "ß"), ("reasoning", "secret"), ("token", "answer")],
        ),
        (
            "<think>ßsecret</think>answer",
            [("reasoning", "ßsecret"), ("token", "answer")],
        ),
    ],
)
async def test_unicode_does_not_shift_minimax_tag_boundaries(
    content: str,
    expected: list[tuple[str, str]],
) -> None:
    assert await _stream([content], provider_id="minimax") == expected


@pytest.mark.asyncio
async def test_non_minimax_provider_preserves_literal_think_markup() -> None:
    events = await _stream(
        ["<think>private</think>answer"],
        provider_id="other",
    )

    assert events == [("token", "<think>private</think>answer")]


@pytest.mark.asyncio
async def test_incomplete_minimax_delimiters_never_enter_public_text() -> None:
    assert await _stream(["answer<thi"], provider_id="minimax") == [
        ("token", "answer")
    ]
    assert await _stream(
        ["<think>private plan</thi"],
        provider_id="minimax",
    ) == [("reasoning", "private plan")]


@pytest.mark.asyncio
async def test_cancellation_discards_a_buffered_delimiter_fragment() -> None:
    graph = _StalledChunkGraph("answer<thi")
    engine = LangGraphEngine(graph, provider_id="minimax")
    control = RunControl(
        request_id="run-cancel-think",
        session_id="session-cancel-think",
    )

    async def collect() -> list[tuple[str, str]]:
        return [
            (event.kind, str(event.payload.get("text") or ""))
            async for event in engine.stream(
                None,
                session_id="thread-cancel-think",
                control=control,
            )
            if event.kind in {"reasoning", "token"}
        ]

    task = asyncio.create_task(collect())
    await asyncio.wait_for(graph.waiting.wait(), timeout=1)
    await control.cancel()

    assert await asyncio.wait_for(task, timeout=1) == [("token", "answer")]


@pytest.mark.asyncio
async def test_equivalent_final_message_does_not_repeat_streamed_output() -> None:
    content = "<think>plan</think>answer"
    events = await _engine_events(
        _MessageGraph(
            [
                AIMessageChunk(content=content, chunk_position="last"),
                AIMessage(
                    content=content,
                    usage_metadata={
                        "input_tokens": 5,
                        "output_tokens": 2,
                        "total_tokens": 7,
                    },
                ),
            ]
        )
    )

    assert [
        (event.kind, event.payload.get("text"))
        for event in events
        if event.kind in {"reasoning", "token"}
    ] == [("reasoning", "plan"), ("token", "answer")]
    assert [event for event in events if event.kind == "usage"] == [
        RunEvent("usage", {"prompt_tokens": 5, "completion_tokens": 2})
    ]


@pytest.mark.asyncio
async def test_reasoning_and_tool_start_share_the_same_round() -> None:
    events = await _engine_events(
        _MessageGraph(
            [
                AIMessage(
                    content="<think>choose lookup</think>",
                    tool_calls=[
                        {
                            "name": "lookup",
                            "args": {"query": "value"},
                            "id": "call-round",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )
    )

    reasoning = next(event for event in events if event.kind == "reasoning")
    tool_start = next(event for event in events if event.kind == "tool_start")
    assert reasoning.payload["round"] == 1
    assert tool_start.payload["round"] == 1


@pytest.mark.asyncio
async def test_unbound_model_tool_call_is_rejected_without_execution_events() -> None:
    graph = _MessageGraph(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "terminal",
                        "args": {"command": "curl https://example.test"},
                        "id": "call-unbound",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content=(
                    "Error: terminal is not a valid tool, try one of "
                    "[browser_navigate]."
                ),
                name="terminal",
                status="error",
                tool_call_id="call-unbound",
            ),
        ]
    )
    graph._bianbu_allowed_tool_names = frozenset({"browser_navigate"})

    events = await _engine_events(graph)

    assert [
        event
        for event in events
        if event.kind in {"tool_start", "tool_end"}
    ] == []
    assert [
        event
        for event in events
        if event.kind == "tool_rejected"
    ] == [
        RunEvent(
            "tool_rejected",
            {
                "call_id": "call-unbound",
                "name": "terminal",
                "arguments": {"command": "curl https://example.test"},
                "reason": "tool_not_available",
                "allowed_tools": ["browser_navigate"],
                "round": 1,
            },
        )
    ]


def _transcript(run_id: str, *items: tuple[str, dict[str, Any]]):
    """A provider folding one fixed Session log into the recovery history."""

    events = [
        SessionEvent(
            session_id="session-recover",
            seq=index,
            type=event_type,
            time=float(index),
            run_id=run_id,
            data=data,
        )
        for index, (event_type, data) in enumerate(items)
    ]

    async def provider(_session_id: str):
        return events

    return provider


@pytest.mark.asyncio
async def test_recovered_minimax_answer_keeps_reasoning_separate() -> None:
    graph = build_langgraph_agent(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="unused")]),
        system_prompt="Test agent",
    )
    engine = LangGraphEngine(
        graph,
        provider_id="minimax",
        transcript_provider=_transcript(
            "run-recover-think",
            ("user/message", {"content": "question"}),
            ("assistant/text.delta", {"text": "<think>saved plan</think>saved answer<thi"}),
            ("assistant/message.end", {"has_tool_calls": False}),
        ),
    )

    events = await engine.recover_completed_events(
        "thread-recover-think",
        "run-recover-think",
    )

    assert events == [
        RunEvent("reasoning", {"text": "saved plan", "round": 1}),
        RunEvent("token", {"text": "saved answer"}),
    ]


@pytest.mark.asyncio
async def test_recovered_reasoning_uses_its_tool_round() -> None:
    @tool
    def lookup(query: str) -> str:
        """Look up one test value."""

        return query

    graph = build_langgraph_agent(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="unused")]),
        system_prompt="Test agent",
        tools=[lookup],
    )
    engine = LangGraphEngine(
        graph,
        provider_id="minimax",
        transcript_provider=_transcript(
            "run-recover-tool",
            ("user/message", {"content": "question"}),
            ("assistant/text.delta", {"text": "<think>select tool</think>"}),
            (
                "tool/call.started",
                {
                    "call_id": "call-recover-round",
                    "name": "lookup",
                    "arguments": {"query": "value"},
                },
            ),
            (
                "tool/call.completed",
                {
                    "call_id": "call-recover-round",
                    "name": "lookup",
                    "result": "result",
                    "status": "success",
                },
            ),
            ("assistant/text.delta", {"text": "final answer"}),
            ("assistant/message.end", {"has_tool_calls": False}),
        ),
    )

    events = await engine.recover_completed_events(
        "thread-recover-tool",
        "run-recover-tool",
    )
    assert events is not None
    reasoning = next(event for event in events if event.kind == "reasoning")
    tool_start = next(event for event in events if event.kind == "tool_start")
    assert reasoning.payload["round"] == 1
    assert tool_start.payload["round"] == 1


@pytest.mark.asyncio
async def test_recovery_keeps_unbound_tool_call_rejected() -> None:
    graph = build_langgraph_agent(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="unused")]),
        system_prompt="Test agent",
    )
    engine = LangGraphEngine(
        graph,
        transcript_provider=_transcript(
            "run-recover-rejected",
            ("user/message", {"content": "question"}),
            (
                "tool/call.started",
                {
                    "call_id": "call-recover-rejected",
                    "name": "terminal",
                    "arguments": {"command": "curl https://example.test"},
                },
            ),
            (
                "tool/call.rejected",
                {
                    "call_id": "call-recover-rejected",
                    "name": "terminal",
                    "arguments": {"command": "curl https://example.test"},
                    "status": "error",
                },
            ),
            ("assistant/text.delta", {"text": "final answer"}),
            ("assistant/message.end", {"has_tool_calls": False}),
        ),
    )

    events = await engine.recover_completed_events(
        "thread-recover-rejected",
        "run-recover-rejected",
    )

    assert events is not None
    assert [
        event.kind
        for event in events
        if event.kind in {"tool_start", "tool_end", "tool_rejected"}
    ] == ["tool_rejected"]
    rejected = next(event for event in events if event.kind == "tool_rejected")
    assert rejected.payload == {
        "call_id": "call-recover-rejected",
        "name": "terminal",
        "arguments": {"command": "curl https://example.test"},
        "reason": "tool_not_available",
        "allowed_tools": [],
        "round": 1,
    }


@pytest.mark.asyncio
async def test_tool_argument_activity_does_not_expose_partial_arguments():
    class ArgumentGraph:
        async def astream(self, _input, **kwargs):
            for instant in [0.0, 0.1, 0.2, 0.49, 0.5, 0.6, 0.99, 1.0]:
                yield AIMessageChunk(content="", tool_call_chunks=[{
                    "name": None, "id": None, "index": 0, "args": "private draft",
                }]), {}

    events = await _engine_events(ArgumentGraph(), provider_id="openai")
    progress = [event for event in events if event.kind == "model_progress"]
    assert progress == []
    assert "private draft" not in str(events)
    assert not any(event.kind == "tool_start" for event in events)


@pytest.mark.asyncio
async def test_compression_activity_does_not_leak_summary():
    class CompressionGraph:
        async def astream(self, _input, **kwargs):
            for instant, content in [(0.0, "private"), (0.1, "summary"),
                                     (0.5, "continued"), (1.0, "")]:
                yield AIMessageChunk(content=content), {"lc_source": "context_compression"}
            yield AIMessageChunk(content="Task finished"), {}

    events = await _engine_events(CompressionGraph(), provider_id="openai")
    progress = [event for event in events if event.kind == "model_progress"]
    assert progress == []
    assert "private" not in str(events)
    assert "Task finished" in str(events)
