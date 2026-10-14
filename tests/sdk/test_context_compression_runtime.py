from __future__ import annotations

import json
import asyncio

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from bianbu_agent import RunControl
from bianbu_agent.context import CompressionSettingsProvider, compression_metadata
from bianbu_agent.context.middleware import (
    CompressionEventSink,
    ContextCompressionMiddleware,
)
from bianbu_agent.engines.langgraph import LangGraphEngine, build_langgraph_agent
from bianbu_agent.engines.langgraph.plugin_tools import build_plugin_tools
from bianbu_agent.plugins import ToolContribution, ToolResult, ToolSpec
from bianbu_agent.sessions.events import SessionEvent


def _transcript(items):
    """One fixed Session log for tests that need prior conversation."""

    return [
        SessionEvent(
            "session-compression", index, event_type, float(index), "run-old", data,
        )
        for index, (event_type, data) in enumerate(items)
    ]


class RecordingCompressionModel(FakeMessagesListChatModel):
    seen_messages: list[list[object]] = []

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen_messages.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stalled_summary_falls_back_or_honors_cancellation(tmp_path, monkeypatch, cancel):
    import bianbu_agent.context.middleware as module

    monkeypatch.setattr(module, "SUMMARY_TIMEOUT_SECONDS", 0.08)
    summary_started = asyncio.Event()

    class StalledSummary(RecordingCompressionModel):
        async def ainvoke(self, input, config=None, **kwargs):
            if (config or {}).get("metadata", {}).get("lc_source") == "context_compression":
                summary_started.set()
                await asyncio.Event().wait()
            return await super().ainvoke(input, config, **kwargs)

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"agent_max_context_tokens": 1000,
                               "context_compression_protect_first_n": 0}))
    model = StalledSummary(profile={"max_input_tokens": 1000}, responses=[AIMessage(content="continued")])
    graph = build_langgraph_agent(model=model, system_prompt="Test",
        context_settings=CompressionSettingsProvider())

    async def provider(_session_id: str):
        return _transcript([
            ("user/message", {"content": "old " + "x" * 5000, "thread_id": "summary-timeout"}),
            ("assistant/text.delta", {"text": "answer"}),
            ("assistant/message.end", {"has_tool_calls": False}),
        ])

    engine = LangGraphEngine(graph, transcript_provider=provider)
    control = RunControl(request_id="summary", session_id="summary-timeout")
    async def collect():
        async with control.model_deadline(0.04):
            return [e async for e in engine.stream("continue", session_id="summary-timeout",
                control=control)]
    task = asyncio.create_task(collect())
    if cancel:
        await asyncio.wait_for(summary_started.wait(), 0.5)
        control.cancelled.set()
        events = await asyncio.wait_for(task, 0.5)
        assert not any(e.kind in {"token", "context_compression"} for e in events)
        return
    events = await asyncio.wait_for(task, 0.5)
    assert any(e.kind == "token" and e.payload["text"] == "continued" for e in events)
    assert any(e.kind == "context_compression" and e.payload["used_fallback"] for e in events)


def test_sync_child_agent_path_compacts_before_model_invocation(tmp_path) -> None:
    settings_path = tmp_path / "config.json"
    settings_path.write_text(
        json.dumps(
            {
                "agent_max_context_tokens": 1_000,
                "context_compression_protect_first_n": 0,
            }
        ),
        encoding="utf-8",
    )
    model = RecordingCompressionModel(
        profile={"max_input_tokens": 1000},
        responses=[AIMessage(content="sync summary")]
    )
    model.seen_messages = []
    middleware = ContextCompressionMiddleware(
        model,
        CompressionSettingsProvider(),
        CompressionEventSink(),
    )

    update = middleware.before_model(
        {
            "messages": [
                HumanMessage(content="old " + ("x" * 5_000), id="old"),
                AIMessage(content="answer", id="answer"),
                HumanMessage(content="continue", id="continue"),
            ]
        },
        None,
    )

    assert update is not None
    assert model.seen_messages == []
    assert any(
        compression_metadata(message) is not None
        for message in update["messages"]
        if isinstance(message, HumanMessage)
    )


@pytest.mark.asyncio
async def test_ineffective_summary_does_not_replace_checkpoint_or_emit_boundary(
    tmp_path,
) -> None:
    settings_path = tmp_path / "config.json"
    settings_path.write_text(
        json.dumps(
            {
                "agent_max_context_tokens": 1_000,
                "context_compression_protect_first_n": 0,
            }
        ),
        encoding="utf-8",
    )
    model = RecordingCompressionModel(
        profile={"max_input_tokens": 1000},
        responses=[AIMessage(content="long summary " * 1_000)]
    )
    events = CompressionEventSink()
    middleware = ContextCompressionMiddleware(
        model,
        CompressionSettingsProvider(),
        events,
        static_tokens=740,
    )
    token = events.activate("run")
    try:
        update = await middleware.abefore_model(
            {
                "messages": [
                    HumanMessage(content="old" * 200, id="old"),
                    AIMessage(content="answer" * 100, id="answer"),
                    HumanMessage(content="continue", id="continue"),
                ]
            },
            None,
        )
    finally:
        events.deactivate(token)

    assert update is None
    assert events.drain("run") == ()


@pytest.mark.asyncio
async def test_pre_model_compression_persists_marker_emits_event_and_continues(
    tmp_path,
) -> None:
    settings_path = tmp_path / "config.json"
    settings_path.write_text(
        json.dumps(
            {
                "agent_max_context_tokens": 1_000,
                "context_compression_threshold": 0.1,
                "context_compression_target_ratio": 0.1,
                "context_compression_protect_first_n": 0,
                "context_compression_protect_last_n": 3,
            }
        ),
        encoding="utf-8",
    )
    model = RecordingCompressionModel(
        profile={"max_input_tokens": 1000},
        responses=[
            AIMessage(content="summary keeps report path"),
            AIMessage(content="continued successfully"),
        ]
    )
    model.seen_messages = []
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        context_settings=CompressionSettingsProvider(),
    )

    async def provider(_session_id: str):
        return _transcript([
            (
                "user/message",
                {"content": "old report " + ("x" * 5_000), "thread_id": "polluted-thread"},
            ),
            ("assistant/text.delta", {"text": "old answer"}),
            ("assistant/message.end", {"has_tool_calls": False}),
        ])

    engine = LangGraphEngine(graph, transcript_provider=provider)

    events = [
        event
        async for event in engine.stream(
            "continue",
            session_id="polluted-thread",
            control=RunControl(request_id="recovery-run", session_id="session"),
        )
    ]

    assert [event.kind for event in events].count("context_compression") == 1
    boundary = next(event for event in events if event.kind == "context_compression")
    assert boundary.payload["compressed"] is True
    assert boundary.payload["before_tokens"] > boundary.payload["after_tokens"]
    assert boundary.payload["compression_count"] == 1
    assert any(
        event.kind == "token" and event.payload["text"] == "continued successfully"
        for event in events
    )
    assert len(model.seen_messages) == 2
    assert "summary keeps report path" in str(model.seen_messages[-1])
    markers = [
        message
        for message in model.seen_messages[-1]
        if compression_metadata(message) is not None
    ]
    assert len(markers) == 1
    assert "x" * 1_000 not in "\n".join(
        str(message.content) for message in model.seen_messages[-1]
    )


@pytest.mark.asyncio
async def test_large_tool_result_is_bounded_then_compressed_inside_same_run(
    tmp_path,
) -> None:
    settings_path = tmp_path / "config.json"
    settings_path.write_text(
        json.dumps(
            {
                "agent_max_context_tokens": 1_000,
                "context_compression_protect_first_n": 0,
            }
        ),
        encoding="utf-8",
    )

    async def render_report(_context, _arguments):
        return ToolResult("REPORT_BEGIN" + ("z" * 30_000) + "REPORT_END")

    tools = build_plugin_tools(
        [
            ToolContribution(
                "report.plugin",
                ToolSpec("render_report", "Render report", {"type": "object"}),
                render_report,
            )
        ]
    )

    model = RecordingCompressionModel(
        profile={"max_input_tokens": 8192},
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "render-call",
                        "name": "render_report",
                        "args": {},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="report tool completed"),
            AIMessage(content="final answer"),
        ]
    )
    model.seen_messages = []
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        tools=tools,
        context_settings=CompressionSettingsProvider(),
    )
    engine = LangGraphEngine(graph)

    events = [
        event
        async for event in engine.stream(
            "make it " + ("p" * 10_000),
            session_id="same-run-thread",
            control=RunControl(request_id="same-run", session_id="session"),
        )
    ]

    tool_end = next(event for event in events if event.kind == "tool_end")
    assert len(tool_end.payload["result"]) <= 16_384
    assert [event.kind for event in events].count("context_compression") == 1
    assert not any(
        event.kind == "token" and event.payload["text"] == "report tool completed"
        for event in events
    )
    assert any(
        event.kind == "token" and event.payload["text"] == "final answer"
        for event in events
    )


@pytest.mark.asyncio
async def test_unknown_capacity_does_not_compact_a_small_conversation():
    model = RecordingCompressionModel(responses=[AIMessage(content="summary")], profile={})
    model.seen_messages = []
    messages = [HumanMessage(content="earlier question"),
                AIMessage(content="tool log " * 2500),
                HumanMessage(content="summarize our conversation")]
    middleware = ContextCompressionMiddleware(model, CompressionSettingsProvider(), CompressionEventSink())
    assert await middleware.abefore_model({"messages": messages}, None) is None
    assert middleware.before_model({"messages": messages}, None) is None
    assert not model.seen_messages


@pytest.mark.asyncio
async def test_unknown_capacity_recovers_after_actual_provider_overflow() -> None:
    class OverflowOnce(RecordingCompressionModel):
        def _generate(self, messages, *args, **kwargs):
            if not self.seen_messages:
                self.seen_messages.append(list(messages))
                raise ValueError("maximum context length exceeded")
            return super()._generate(messages, *args, **kwargs)

    model = OverflowOnce(profile={}, responses=[AIMessage(content="continued")])
    model.seen_messages = []
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test",
        context_settings=CompressionSettingsProvider(),
    )

    async def provider(_session_id: str):
        return [
            SessionEvent(
                "session-overflow", 0, "user/message", 0.0, "run-old",
                {
                    "content": "old " + "x" * 24000,
                    "thread_id": "unknown-overflow",
                },
            ),
            SessionEvent(
                "session-overflow", 1, "assistant/text.delta", 1.0, "run-old",
                {"text": "previous answer"},
            ),
            SessionEvent(
                "session-overflow", 2, "assistant/message.end", 2.0, "run-old",
                {"has_tool_calls": False},
            ),
        ]

    events = [
        event
        async for event in LangGraphEngine(
            graph, transcript_provider=provider
        ).stream(
            "continue",
            session_id="unknown-overflow",
            control=RunControl(request_id="overflow", session_id="test"),
        )
    ]
    boundaries = [event for event in events if event.kind == "context_compression"]
    assert len(boundaries) == 1
    assert boundaries[0].payload["emergency"]
    assert any(
        event.kind == "token" and event.payload["text"] == "continued"
        for event in events
    )
    assert len(model.seen_messages) == 2
