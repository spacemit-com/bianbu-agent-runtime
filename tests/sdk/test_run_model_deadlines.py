from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from bianbu_agent import RunControl, AgentSpec, ModelRegistry, RunEvent, RunManager
from bianbu_agent.runs.stores.sqlite import SQLiteRunStore


@dataclass
class Model:
    model: str
    request_timeout_seconds: float | None = None


class Agents:
    async def materialize(self, agent_id, generation, **kwargs):
        del generation
        return AgentSpec(id=agent_id or "default", name="Agent", model="fast")


class Plugins:
    @asynccontextmanager
    async def lease(self):
        yield SimpleNamespace(id=1, tools={}, skills={})


class NonResponsiveModelEngine:
    async def stream(self, prompt, **kwargs):
        del prompt, kwargs
        await asyncio.Event().wait()
        if False:
            yield RunEvent("done", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("after_tools", ["complete", "model_hangs", "tool_timeout", "approval"])
async def test_model_deadline_waits_for_all_parallel_tools(tmp_path, after_tools):
    terminal_wait_finished = asyncio.Event()
    class ParallelToolsEngine:
        async def stream(self, prompt, *, control, **kwargs):
            del prompt, kwargs
            yield RunEvent("tool_start", {"call_id": "terminal", "name": "terminal"})
            yield RunEvent("tool_start", {"call_id": "skill", "name": "skill_view"})
            if after_tools == "approval":
                task = asyncio.ensure_future(control.request_approval({
                    "approval_id": "tools",
                    "actions": [{"allowed_decisions": ["approve", "reject"]}],
                }))
                surfaced = await control.wait_approval_request()
                yield RunEvent("approval_required", dict(surfaced))
                assert await task == [{"type": "approve"}]
            yield RunEvent("tool_end", {"call_id": "skill", "name": "skill_view"})
            # Reasoning/progress from another branch must not restart the
            # model clock while the terminal is still executing.
            yield RunEvent("reasoning", {"text": "Inspecting the repository"})
            await asyncio.sleep(0.06)
            terminal_wait_finished.set()
            if after_tools == "tool_timeout":
                raise TimeoutError("terminal command reached its own timeout")
            yield RunEvent("tool_end", {"call_id": "terminal", "name": "terminal"})
            if after_tools == "model_hangs":
                await asyncio.Event().wait()
            yield RunEvent("token", {"text": "Both tools finished"})
            yield RunEvent("done", {})

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(), agent_manager=Agents(),
        model_registry=ModelRegistry(default_model=Model("default", 0.02),
                                     models={"fast": Model("fast", 0.02)}),
        engine_builder=lambda *_args: ParallelToolsEngine(), store=store,
    )
    try:
        handle = await manager.run("parallel-tool-deadline", "Port the repository")
        if after_tools == "approval":
            async with asyncio.timeout(1):
                async for event in handle:
                    if event.kind == "approval_required":
                        await asyncio.sleep(0)
                        assert await handle.approve("tools")
        result = await asyncio.wait_for(handle.wait(), timeout=1)
        assert terminal_wait_finished.is_set()
        if after_tools in {"complete", "approval"}:
            assert result.status == "completed"
            assert result.content == "Both tools finished"
        else:
            assert result.status == "failed"
            if after_tools == "model_hangs":
                # 模型空闲计时到期 = Provider 没有回包，必须是可行动的 provider_timeout，
                # 而不是裸的 run_failed。
                assert result.error.code == "provider_timeout"
                assert "0.02 秒" in result.error.message
            else:
                assert result.error.message == "terminal command reached its own timeout"
    finally:
        await manager.close()
        await store.close()


@pytest.mark.asyncio
async def test_run_manager_fails_when_model_stream_never_produces_an_event(
    tmp_path,
) -> None:
    """HTTP timeout hints alone cannot leave a Run permanently active."""

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default", 0.02),
            models={"fast": Model("fast", 0.02)},
        ),
        engine_builder=lambda *_args: NonResponsiveModelEngine(),
        store=store,
    )

    handle = await manager.run("session-timeout", "hi")
    result = await asyncio.wait_for(handle.wait(), timeout=1)

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "provider_timeout"
    assert "0.02 秒" in result.error.message
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_title_model_waits_for_active_conversation_model(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A low-capacity provider must always give the user response priority."""

    conversation_started = asyncio.Event()
    release_conversation = asyncio.Event()
    title_started = asyncio.Event()

    class BlockingConversationEngine:
        async def stream(self, prompt, **kwargs):
            del prompt, kwargs
            conversation_started.set()
            await release_conversation.wait()
            yield RunEvent("token", {"text": "conversation done"})
            yield RunEvent("done", {})

    async def title_model(*_args, **_kwargs) -> str:
        title_started.set()
        return "generated title"

    monkeypatch.setattr(
        "bianbu_agent.runs.manager.generate_session_title",
        title_model,
    )
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"),
            models={"fast": Model("fast")},
        ),
        engine_builder=lambda *_args: BlockingConversationEngine(),
        store=store,
    )

    conversation = await manager.run("session-priority", "hi")
    await asyncio.wait_for(conversation_started.wait(), timeout=1)
    title = await manager.run(
        "session-priority",
        "hi",
        metadata={"console_operation": "generate_title"},
    )
    await asyncio.sleep(0.02)
    assert not title_started.is_set()

    release_conversation.set()
    assert (await asyncio.wait_for(conversation.wait(), timeout=1)).status == (
        "completed"
    )
    assert (await asyncio.wait_for(title.wait(), timeout=1)).status == "completed"
    assert title_started.is_set()
    await manager.close()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_state", ["complete", "no_final_marker", "stalled", "empty_keepalives"])
async def test_streamed_tool_arguments_refresh_model_idle_deadline(tmp_path, stream_state):
    from langchain_core.messages import AIMessageChunk, ToolMessage
    from bianbu_agent.engines.langgraph import LangGraphEngine

    class ToolArgumentGraph:
        async def aget_state(self, config):
            return SimpleNamespace(values={}, tasks=())

        async def astream(self, _input, **kwargs):
            yield AIMessageChunk(content="", tool_call_chunks=[{
                "id": "write-html", "name": "write_file", "index": 0,
                "args": '{"content":"',
            }]), {}
            if stream_state == "stalled":
                await asyncio.Event().wait()
            for _ in range(12):
                await asyncio.sleep(0.01)
                yield AIMessageChunk(content="", tool_call_chunks=[{
                    "id": None, "name": None, "index": 0,
                    "args": "<div>private draft</div>" if stream_state != "empty_keepalives" else "",
                }]), {}
            yield AIMessageChunk(content="", chunk_position=None if stream_state == "no_final_marker" else "last", tool_call_chunks=[{
                "id": None, "name": None, "index": 0, "args": '"}',
            }]), {}
            yield ToolMessage(content="written", tool_call_id="write-html", name="write_file"), {}
            yield AIMessageChunk(content="Ready", chunk_position="last"), {}

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(), agent_manager=Agents(),
        model_registry=ModelRegistry(default_model=Model("fast", 0.04),
                                     models={"fast": Model("fast", 0.04)}),
        engine_builder=lambda *_args: LangGraphEngine(ToolArgumentGraph()), store=store,
    )
    try:
        handle = await manager.run("stream-tool-arguments", "Write a browser app")
        events = [event async for event in handle]
        result = await asyncio.wait_for(handle.wait(), timeout=2)
        assert all(event.kind != "model_progress" for event in events)
        starts = [event for event in events if event.kind == "tool_start"]
        if stream_state in {"complete", "no_final_marker"}:
            assert result.status == "completed", result.error
            assert result.content == "Ready"
            assert len(starts) == 1
            assert starts[0].payload["arguments"] == {"content": "<div>private draft</div>" * 12}
        else:
            assert result.status == "failed"
            assert result.error.code == "provider_timeout"
            assert "0.04 秒" in result.error.message
            assert not starts
    finally:
        await manager.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_state", ["complete", "stalled", "empty_keepalives", "empty_blocks", "text_blocks", "cancel"])
async def test_compression_stream_refreshes_deadline_and_resumes_agent_loop(tmp_path, summary_state):
    from langchain_core.messages import AIMessageChunk, ToolMessage
    from bianbu_agent.engines.langgraph import LangGraphEngine

    class CompressionGraph:
        async def aget_state(self, config):
            return SimpleNamespace(values={}, tasks=())

        async def astream(self, _input, **kwargs):
            for _ in range(12):
                await asyncio.sleep(0.01)
                if summary_state == "stalled":
                    await asyncio.Event().wait()
                yield AIMessageChunk(
                    content=([{"type": "text", "text": "" if summary_state == "empty_blocks" else "private summary"}]
                             if summary_state in {"empty_blocks", "text_blocks"}
                             else "private summary" if summary_state in {"complete", "cancel"} else "")
                ), {"lc_source": "context_compression"}
            yield AIMessageChunk(content="", chunk_position="last", tool_call_chunks=[{
                "id": "after-summary", "name": "terminal", "index": 0,
                "args": '{"command":"make test"}',
            }]), {}
            yield ToolMessage(content="passed", tool_call_id="after-summary", name="terminal"), {}
            yield AIMessageChunk(content="Verified", chunk_position="last"), {}

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(), agent_manager=Agents(),
        model_registry=ModelRegistry(default_model=Model("fast", 0.04),
                                     models={"fast": Model("fast", 0.04)}),
        engine_builder=lambda *_args: LangGraphEngine(CompressionGraph()), store=store,
    )
    try:
        handle = await manager.run("compression-loop", "Continue the port")
        if summary_state == "cancel":
            await asyncio.sleep(0.025)
            assert await handle.cancel()
        events = [event async for event in handle]
        result = await asyncio.wait_for(handle.wait(), timeout=2)
        assert all(event.kind != "model_progress" for event in events)
        assert "private summary" not in str(events)
        if summary_state in {"complete", "text_blocks"}:
            assert result.status == "completed", result.error
            assert result.content == "Verified"
            assert len([event for event in events if event.kind == "tool_end"]) == 1
        elif summary_state == "cancel":
            assert result.status == "cancelled"
            assert not any(event.kind == "tool_start" for event in events)
        else:
            assert result.status == "failed"
            assert result.error.code == "provider_timeout"
            assert "0.04 秒" in result.error.message
    finally:
        await manager.close()
        await store.close()


@pytest.mark.asyncio
async def test_model_deadline_resumes_after_compression():
    control = RunControl(request_id="deadline", session_id="deadline")
    with pytest.raises(TimeoutError):
        async with control.model_deadline(0.02):
            with control.pause_model_deadline():
                await asyncio.sleep(0.04)
            await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_tool_argument_chunks_keep_model_deadline_alive():
    from langchain_core.messages import AIMessageChunk
    from bianbu_agent.engines.langgraph import LangGraphEngine

    class Graph:
        async def astream(self, *args, **kwargs):
            for i, fragment in enumerate(['{"text":"', 'a', 'b', 'c', 'd', 'e', '"}']):
                await asyncio.sleep(0.01)
                yield AIMessageChunk(content="", tool_call_chunks=[{
                    "index": 0, "name": "write_file" if i == 0 else None,
                    "id": "call" if i == 0 else None, "args": fragment,
                }]), {}
            yield AIMessageChunk(content="", chunk_position="last"), {}

    control = RunControl(request_id="chunks", session_id="chunks")
    iterator = LangGraphEngine(Graph()).stream(None, session_id="chunks", control=control)
    try:
        async with control.model_deadline(0.04):
            events = []
            async for event in iterator:
                events.append(event)
                if event.kind == "tool_start":
                    break
        assert events[-1].payload["arguments"] == {"text": "abcde"}
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["stream", "compression"])
async def test_internal_model_progress_does_not_timeout_the_loop(tmp_path, phase):
    class Engine:
        async def stream(self, prompt, *, control, **kwargs):
            if phase == "compression":
                with control.pause_model_deadline():
                    await asyncio.sleep(0.08)
            else:
                for _ in range(8):
                    await asyncio.sleep(0.01)
                    control.model_progress()
            yield RunEvent("token", {"text": "continued"})
            yield RunEvent("done", {})

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(), agent_manager=Agents(),
        model_registry=ModelRegistry(default_model=Model("fast", 0.04),
                                     models={"fast": Model("fast", 0.04)}),
        engine_builder=lambda *_args: Engine(), store=store,
    )
    try:
        handle = await manager.run("progress", "continue")
        result = await asyncio.wait_for(handle.wait(), 1)
        assert result.status == "completed", result.error
        assert result.content == "continued"
    finally:
        await manager.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("keeps_timing_out", [False, True, "cancel"])
async def test_model_idle_timeout_resumes_checkpoint_without_replaying_completed_tool(tmp_path, keeps_timing_out):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from bianbu_agent.engines.langgraph import LangGraphEngine, build_langgraph_agent

    effects = []
    calls = []

    @tool
    def save_step() -> str:
        """Write a completed step."""
        effects.append('saved')
        return 'saved'

    class RecoveringModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            calls.append(messages)
            if len(calls) == 2 or (keeps_timing_out and len(calls) > 2):
                await asyncio.Event().wait()
            return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = RecoveringModel(responses=[
        AIMessage(content='', tool_calls=[{'id': 'write-once', 'name': 'save_step', 'args': {}}]),
        AIMessage(content='finished after recovery'),
    ])
    store = SQLiteRunStore(tmp_path / 'runs.sqlite3')
    await store.open()
    # The graph keeps no state: the Run transcript is the only history, so the
    # recovery re-entry folds it instead of resuming a checkpoint.
    engine = LangGraphEngine(
        build_langgraph_agent(model=model, system_prompt='Test', tools=[save_step]),
        transcript_provider=lambda session_id: store.read_session_events(session_id),
    )
    manager = RunManager(plugin_manager=Plugins(), agent_manager=Agents(),
        model_registry=ModelRegistry(default_model=Model('fast', .1), models={'fast': Model('fast', .1)}),
        engine_builder=lambda *_: engine, store=store)
    try:
        handle = await manager.run('recover-timeout', 'create app')
        events = []
        async for e in handle:
            events.append(e)
            if keeps_timing_out == 'cancel' and e.kind == 'progress':
                assert await handle.cancel()
        result = await handle.wait()
        assert effects == ['saved']
        assert any(e.kind == 'progress' and e.payload.get('reason') == 'model_timeout' for e in events)
        if keeps_timing_out == 'cancel':
            assert result.status == 'cancelled'
            assert not any(e.kind == 'error' for e in events)
            assert len(calls) <= 3
        elif keeps_timing_out:
            assert result.status == 'failed'
            assert result.error.code == 'model_recovery_exhausted'
            assert result.error.details['resumable'] is True
            assert result.error.details['reason'] == 'model_timeout'
            assert len(calls) == 4
        else:
            assert result.status == 'completed'
            assert result.content == 'finished after recovery'
            assert len(calls) == 3
    finally:
        await manager.close()
        await store.close()


@pytest.mark.asyncio
async def test_recovery_checkpoint_probe_failure_is_conservative():
    from bianbu_agent.runs.manager import _can_resume_model

    class BrokenCheckpoint:
        async def can_resume_model(self, session_id, request_id):
            raise OSError('checkpoint unavailable')

    class CancelledCheckpoint:
        async def can_resume_model(self, session_id, request_id):
            raise asyncio.CancelledError

    assert await _can_resume_model(object(), 's', 'r') is False
    assert await _can_resume_model(BrokenCheckpoint(), 's', 'r') is False
    with pytest.raises(asyncio.CancelledError):
        await _can_resume_model(CancelledCheckpoint(), 's', 'r')


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['partial', 'cancel_probe'])
async def test_recovery_rolls_back_partial_text_and_honors_cancel(tmp_path, scenario):
    from bianbu_agent.runs.recovery import ModelRecoveryExhausted
    probe_started = asyncio.Event()

    class Engine:
        async def can_resume_model(self, *_):
            if scenario == 'cancel_probe':
                probe_started.set()
                await asyncio.Event().wait()
            return True

        async def stream(self, prompt, **kwargs):
            if scenario == 'cancel_probe':
                raise ModelRecoveryExhausted('empty_output', 2, 'empty')
            if prompt is not None:
                yield RunEvent('token', {'text': 'Saved. '})
                yield RunEvent('tool_start', {'call_id': 'saved'})
                yield RunEvent('tool_end', {'call_id': 'saved'})
                yield RunEvent('token', {'text': 'Hello 🌏'})
                await asyncio.Event().wait()
            yield RunEvent('token', {'text': 'Hello 🌏 world'})
            yield RunEvent('done', {})

    store = SQLiteRunStore(tmp_path / 'runs.sqlite3')
    await store.open()
    manager = RunManager(plugin_manager=Plugins(), agent_manager=Agents(),
        model_registry=ModelRegistry(default_model=Model('fast', .05), models={'fast': Model('fast', .05)}),
        engine_builder=lambda *_: Engine(), store=store)
    try:
        handle = await manager.run('recover', 'create app')
        if scenario == 'cancel_probe':
            await asyncio.wait_for(probe_started.wait(), 1)
            assert await handle.cancel()
        result = await asyncio.wait_for(handle.wait(), 1)
        if scenario == 'cancel_probe':
            assert result.status == 'cancelled'
        else:
            assert result.status == 'completed'
            assert result.content == 'Saved. Hello 🌏 world'
            events = [e async for e in handle]
            assert any(e.kind == 'progress' and e.payload.get('discarded_text') == 'Hello 🌏' for e in events)
    finally:
        await manager.close()
        await store.close()
