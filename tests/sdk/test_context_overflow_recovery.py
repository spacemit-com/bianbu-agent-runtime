from __future__ import annotations

from types import SimpleNamespace
import asyncio

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from bianbu_agent import RunControl
from bianbu_agent.context import CompressionSettingsProvider
from bianbu_agent.context.middleware import (
    CompressionEventSink,
    ContextCompressionMiddleware,
)
from bianbu_agent.engines.langgraph.engine import (
    LangGraphEngine,
    is_context_overflow_error,
)
from bianbu_agent.sessions.events import SessionEvent


def test_recognizes_provider_context_overflow_without_masking_other_400s() -> None:
    overflow = RuntimeError(
        "Error code: 400 - This model's maximum context length is 1048576 "
        "tokens. However, you requested 1513974 tokens."
    )

    assert is_context_overflow_error(overflow) is True
    assert is_context_overflow_error(RuntimeError("Error code: 400 - bad API key")) is False


class OverflowOnceGraph:
    def __init__(self, messages) -> None:
        self.messages = list(messages)
        self.inputs: list[object] = []
        self.updates: list[tuple[object, object]] = []
        self._bianbu_context_events = CompressionEventSink()
        model = FakeMessagesListChatModel(responses=[AIMessage(content="unused")])
        self._bianbu_context_middleware = ContextCompressionMiddleware(
            model,
            CompressionSettingsProvider(),
            self._bianbu_context_events,
        )

    async def astream(self, agent_input, **_kwargs):
        self.inputs.append(agent_input)
        if len(self.inputs) == 1:
            raise RuntimeError(
                "maximum context length is 1048576 tokens; requested 1513974 tokens"
            )
        yield AIMessage(content="recovered answer"), {}

    async def aget_state(self, _config):
        return SimpleNamespace(values={"messages": list(self.messages)}, interrupts=())

    async def aupdate_state(self, _config, update, *, as_node=None):
        self.updates.append((update, as_node))
        replacements = list(update["messages"])
        assert isinstance(replacements[0], RemoveMessage)
        self.messages = replacements[1:]


class AlwaysErrorGraph(OverflowOnceGraph):
    def __init__(self, messages, error: Exception) -> None:
        super().__init__(messages)
        self.error = error

    async def astream(self, agent_input, **_kwargs):
        self.inputs.append(agent_input)
        raise self.error
        yield  # pragma: no cover - keeps this an async generator


class ConcurrentGraph:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def astream(self, _agent_input, **_kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            yield AIMessage(content="done"), {}
        finally:
            self.active -= 1

    async def aget_state(self, _config):
        return SimpleNamespace(values={"messages": []}, interrupts=())


class RecordingConfigGraph:
    def __init__(self) -> None:
        self.config = None

    async def astream(self, _agent_input, *, config, **_kwargs):
        self.config = config
        yield AIMessage(content="done"), {}

    async def aget_state(self, _config):
        return SimpleNamespace(values={"messages": []}, interrupts=())


def _polluted_transcript(thread_id: str):
    """A folded history whose tool result alone overflows any context budget."""

    payload = "PNG_BASE64_" * 150_000
    events = [
        SessionEvent(
            session_id="session-overflow",
            seq=index,
            type=event_type,
            time=float(index),
            run_id="old-run",
            data=data,
        )
        for index, (event_type, data) in enumerate(
            [
                ("user/message", {"content": "make report", "thread_id": thread_id}),
                (
                    "tool/call.started",
                    {"call_id": "media", "name": "read_media", "arguments": {}},
                ),
                (
                    "tool/call.completed",
                    {
                        "call_id": "media",
                        "name": "read_media",
                        "result": payload,
                        "status": "success",
                    },
                ),
            ]
        )
    ]

    async def provider(_session_id: str):
        return events

    return payload, provider


def _messages_text(messages) -> str:
    return "\n".join(str(getattr(message, "content", "")) for message in messages)


@pytest.mark.asyncio
async def test_engine_places_trusted_account_in_tool_config() -> None:
    graph = RecordingConfigGraph()
    engine = LangGraphEngine(graph)

    events = [
        event
        async for event in engine.stream(
            "hello",
            session_id="thread",
            account_id="system-user-42",
            control=RunControl(request_id="run", session_id="session"),
        )
    ]

    assert [event.payload.get("text") for event in events] == ["done"]
    assert graph.config["configurable"]["account_id"] == "system-user-42"


@pytest.mark.asyncio
async def test_overflow_compacts_the_folded_input_without_duplicate_input() -> None:
    payload, provider = _polluted_transcript("polluted-thread")
    graph = OverflowOnceGraph([])
    engine = LangGraphEngine(graph, transcript_provider=provider)

    events = [
        event
        async for event in engine.stream(
            "continue",
            session_id="polluted-thread",
            control=RunControl(request_id="recovery-run", session_id="session"),
        )
    ]

    assert len(graph.inputs) == 2
    retry = graph.inputs[1]
    assert isinstance(retry, dict)
    assert payload not in _messages_text(retry["messages"])
    assert graph.updates == []
    assert [event.kind for event in events] == ["context_compression", "token"]
    assert events[0].payload["emergency"] is True
    assert events[1].payload["text"] == "recovered answer"


@pytest.mark.asyncio
async def test_non_overflow_errors_are_not_retried_or_compacted() -> None:
    graph = AlwaysErrorGraph(
        [HumanMessage(content="continue", id="bianbu-agent-run:plain-run")],
        RuntimeError("Error code: 400 - bad API key"),
    )
    engine = LangGraphEngine(graph)

    with pytest.raises(RuntimeError, match="bad API key"):
        async for _event in engine.stream(
            "continue",
            session_id="plain-thread",
            control=RunControl(request_id="plain-run", session_id="session"),
        ):
            pass

    assert len(graph.inputs) == 1
    assert graph.updates == []


@pytest.mark.asyncio
async def test_second_context_overflow_is_propagated_without_a_third_attempt() -> None:
    payload, provider = _polluted_transcript("retry-thread")
    error = RuntimeError(
        "maximum context length is 1048576 tokens; requested 1513974 tokens"
    )
    graph = AlwaysErrorGraph([], error)
    engine = LangGraphEngine(graph, transcript_provider=provider)

    events = []
    with pytest.raises(RuntimeError, match="maximum context length"):
        async for event in engine.stream(
            "continue",
            session_id="retry-thread",
            control=RunControl(request_id="retry-run", session_id="session"),
        ):
            events.append(event)

    assert len(graph.inputs) == 2
    retry = graph.inputs[1]
    assert isinstance(retry, dict)
    assert payload not in _messages_text(retry["messages"])
    assert graph.updates == []
    assert [event.kind for event in events] == ["context_compression"]


@pytest.mark.asyncio
async def test_same_thread_graph_execution_is_serialized() -> None:
    graph = ConcurrentGraph()
    engine = LangGraphEngine(graph)

    async def consume(request_id: str) -> None:
        async for _event in engine.stream(
            "continue",
            session_id="shared-thread",
            control=RunControl(request_id=request_id, session_id="session"),
        ):
            pass

    await asyncio.gather(consume("one"), consume("two"))

    assert graph.max_active == 1
