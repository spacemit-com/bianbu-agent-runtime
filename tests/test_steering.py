from __future__ import annotations

import asyncio
from typing import Any

import pytest

from langchain_core.language_models.fake_chat_models import (
    FakeListChatModel,
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import Field

from bianbu_agent import RunControl
from bianbu_agent.engines.langgraph import LangGraphEngine, build_langgraph_agent
from bianbu_agent.sessions import SessionInboxMessage
from bianbu_agent.sessions.events import SessionEvent


class ToolBindingFakeChatModel(FakeMessagesListChatModel):
    seen_messages: list[list[Any]] = Field(default_factory=list)

    def _generate(self, messages, *args, **kwargs):
        self.seen_messages.append(list(messages))
        return super()._generate(messages, *args, **kwargs)

    def bind_tools(
        self,
        tools: list[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> ToolBindingFakeChatModel:
        return self


class StreamingToolBindingFakeChatModel(FakeListChatModel):
    seen_messages: list[list[Any]] = Field(default_factory=list)

    def bind_tools(
        self,
        tools: list[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> StreamingToolBindingFakeChatModel:
        return self

    async def _astream(self, messages, *args, **kwargs):
        self.seen_messages.append(list(messages))
        async for chunk in super()._astream(messages, *args, **kwargs):
            yield chunk



class _SessionLog:
    """The folded Session log one steering test writes, like the RunManager.

    Steering no longer seeds graph state: the Run stream publishes
    ``input_applied`` and the host writes that user turn into the Session log,
    which the next model step folds back in.  This helper mirrors that write so
    the test exercises the same path.
    """

    def __init__(self, session_id: str, thread_id: str, request_id: str) -> None:
        self.session_id = session_id
        self.thread_id = thread_id
        self.request_id = request_id
        self._events: list[SessionEvent] = []

    async def read(self, _session_id: str):
        return tuple(self._events)

    def start(self, prompt: str) -> None:
        self._append({"content": prompt})

    def apply(self, item: SessionInboxMessage) -> None:
        self._append({"content": item.content, "message_id": item.message_id})

    def _append(self, data: dict[str, Any]) -> None:
        index = len(self._events)
        self._events.append(SessionEvent(
            session_id=self.session_id,
            seq=index,
            type="user/message",
            time=float(index),
            run_id=self.request_id,
            data={**data, "thread_id": self.thread_id},
        ))


class ControlledCheckpointGraph:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.updates: list[dict[str, Any]] = []

    async def aupdate_state(self, _config, update, **_kwargs) -> None:
        self.updates.append(update)
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error


async def test_checkpoint_failure_keeps_staged_step_input_pending() -> None:
    """A failed checkpoint must not acknowledge input the model never received."""

    graph = ControlledCheckpointGraph(error=OSError("checkpoint failed"))
    engine = LangGraphEngine(graph)
    control = RunControl(request_id="run-fail", session_id="session-fail")
    message = SessionInboxMessage("message-1", "try another path")
    control.bind_step_input_loader(lambda: asyncio.sleep(0, result=(message,)))
    await control.notify_step_input()
    graph.release.set()

    with pytest.raises(OSError, match="checkpoint failed"):
        await engine.claim_step_input({}, control)

    assert await control.load_step_input() == (message,)


async def test_step_ack_leaves_input_arriving_during_checkpoint_for_later() -> None:
    """A concurrent arrival must remain pending after the earlier batch commits."""

    graph = ControlledCheckpointGraph()
    engine = LangGraphEngine(graph)
    control = RunControl(request_id="run-race", session_id="session-race")
    first = SessionInboxMessage("message-1", "first")
    second = SessionInboxMessage("message-2", "second")
    pending = [first]

    async def load_pending() -> tuple[SessionInboxMessage, ...]:
        return tuple(pending)

    control.bind_step_input_loader(load_pending)
    await control.notify_step_input()

    applying = asyncio.create_task(engine.claim_step_input({}, control))
    await graph.started.wait()
    pending.append(second)
    await control.notify_step_input()
    graph.release.set()

    assert await applying == (first,)
    assert control.step_input_pending()


async def test_same_boundary_steering_is_checkpointed_as_one_fifo_batch() -> None:
    graph = ControlledCheckpointGraph()
    engine = LangGraphEngine(graph)
    control = RunControl(request_id="run-batch", session_id="session-batch")
    first = SessionInboxMessage("message-1", "first")
    second = SessionInboxMessage("message-2", "second")
    control.bind_step_input_loader(
        lambda: asyncio.sleep(0, result=(first, second))
    )
    await control.notify_step_input()
    graph.release.set()

    assert await engine.claim_step_input({}, control) == (first, second)
    assert len(graph.updates) == 1
    assert [message.id for message in graph.updates[0]["messages"]] == [
        "message-1",
        "message-2",
    ]


async def test_steering_is_claimed_as_fifo_next_step_messages_in_same_run() -> None:
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    @tool
    async def slow_lookup(query: str) -> str:
        """Wait until steering has been queued."""

        tool_started.set()
        await release_tool.wait()
        return f"result for {query}"

    model = ToolBindingFakeChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "slow_lookup",
                        "args": {"query": "weather"},
                        "id": "call-steer",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="changed direction"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        tools=[slow_lookup],
        system_prompt="You are a test agent.",
        hitl_tools=(),
    )
    log = _SessionLog("session-steer", "thread-steer", "run-steer")
    log.start("look up weather")
    engine = LangGraphEngine(graph, transcript_provider=log.read)
    control = RunControl(request_id="run-steer", session_id="session-steer")
    pending: list[SessionInboxMessage] = []
    control.bind_step_input_loader(
        lambda: asyncio.sleep(0, result=tuple(pending))
    )
    run_task = asyncio.create_task(
        _collect(engine, "look up weather", "thread-steer", control, pending, log)
    )
    await asyncio.wait_for(tool_started.wait(), timeout=2)
    pending.extend((
        SessionInboxMessage("steer-1", "first change"),
        SessionInboxMessage("steer-2", "second change"),
    ))
    await control.notify_step_input()
    release_tool.set()

    events = await asyncio.wait_for(run_task, timeout=2)

    assert [
        event.payload["message_id"]
        for event in events
        if event.kind == "input_applied"
    ] == ["steer-1", "steer-2"]
    assert len(model.seen_messages) == 2
    assert [message.content for message in model.seen_messages[1] if isinstance(message, HumanMessage)][-2:] == [
        "first change",
        "second change",
    ]


async def test_steering_during_final_generation_opens_a_next_step() -> None:
    first_token = asyncio.Event()
    model = StreamingToolBindingFakeChatModel(
        responses=["first answer", "steered answer"],
        sleep=0.02,
    )
    graph = build_langgraph_agent(
        model=model,
        tools=[],
        system_prompt="You are a test agent.",
        hitl_tools=(),
    )
    log = _SessionLog("session-steer", "thread-final-steer", "run-final-steer")
    log.start("start")
    engine = LangGraphEngine(graph, transcript_provider=log.read)
    control = RunControl(request_id="run-final-steer", session_id="session-steer")
    pending: list[SessionInboxMessage] = []
    control.bind_step_input_loader(
        lambda: asyncio.sleep(0, result=tuple(pending))
    )

    async def collect_events() -> list[Any]:
        events = []
        async for event in engine.stream(
            "start", session_id="thread-final-steer", control=control
        ):
            events.append(event)
            if event.kind == "input_applied":
                assert pending[0].message_id == event.payload["message_id"]
                log.apply(pending.pop(0))
            if event.kind == "token":
                first_token.set()
        return events

    run_task = asyncio.create_task(collect_events())
    await asyncio.wait_for(first_token.wait(), timeout=2)
    pending.append(SessionInboxMessage("steer-final", "change now"))
    await control.notify_step_input()

    events = await asyncio.wait_for(run_task, timeout=3)

    assert [
        event.payload["message_id"]
        for event in events
        if event.kind == "input_applied"
    ] == ["steer-final"]
    assert len(model.seen_messages) == 2
    assert any(
        isinstance(message, HumanMessage) and message.content == "change now"
        for message in model.seen_messages[1]
    )


async def test_steering_after_a_claim_opens_another_model_step() -> None:
    model = StreamingToolBindingFakeChatModel(
        responses=["first answer", "first steer answer", "second steer answer"],
        sleep=0.02,
    )
    graph = build_langgraph_agent(
        model=model,
        tools=[],
        system_prompt="You are a test agent.",
        hitl_tools=(),
    )
    log = _SessionLog("session-steer", "thread-two-steps", "run-two-steps")
    log.start("start")
    engine = LangGraphEngine(graph, transcript_provider=log.read)
    control = RunControl(request_id="run-two-steps", session_id="session-steer")
    pending: list[SessionInboxMessage] = []
    control.bind_step_input_loader(
        lambda: asyncio.sleep(0, result=tuple(pending))
    )

    run_task = asyncio.create_task(
        _collect(engine, "start", "thread-two-steps", control, pending, log)
    )
    while len(model.seen_messages) < 1:
        await asyncio.sleep(0.005)
    pending.append(SessionInboxMessage("steer-first", "first change"))
    await control.notify_step_input()
    while len(model.seen_messages) < 2:
        await asyncio.sleep(0.005)
    pending.append(SessionInboxMessage("steer-second", "second change"))
    await control.notify_step_input()

    events = await asyncio.wait_for(run_task, timeout=5)

    assert [
        event.payload["message_id"]
        for event in events
        if event.kind == "input_applied"
    ] == ["steer-first", "steer-second"]
    assert len(model.seen_messages) == 3
    assert "first change" in str(model.seen_messages[1])
    assert "second change" in str(model.seen_messages[2])


async def _collect(
    engine: LangGraphEngine,
    prompt: str,
    session_id: str,
    control: RunControl,
    pending: list[SessionInboxMessage],
    log: _SessionLog | None = None,
) -> list[Any]:
    events = []
    async for event in engine.stream(
        prompt,
        session_id=session_id,
        control=control,
    ):
        events.append(event)
        if event.kind == "input_applied":
            assert pending[0].message_id == event.payload["message_id"]
            applied = pending.pop(0)
            if log is not None:
                log.apply(applied)
    return events
