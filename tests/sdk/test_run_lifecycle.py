from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from bianbu_agent import (
    AgentManager,
    AgentModel,
    AgentSpec,
    InMemoryAgentRegistry,
    ModelRegistry,
    RunError,
    RunEvent,
    RunManager,
    RunResult,
    ToolContribution,
    ToolResult,
    ToolSpec,
)
from bianbu_agent.plugins import (
    ContextPatch,
    HookContribution,
    HookPoint,
    PluginContributions,
    PluginGeneration,
    PluginRuntime,
    SkillContribution,
    SkillSpec,
)
from bianbu_agent.engines.langgraph.plugin_tools import build_plugin_tools
from bianbu_agent.models import OpenAICompatibleModel
from bianbu_agent.runs.stores.sqlite import SQLiteRunStore
from bianbu_agent.sessions.log_store import SessionLogStore
from bianbu_agent.sessions import SessionInboxMessage
from bianbu_agent.tools.runtime import ToolExecutionScope, ToolRuntime


@dataclass
class Model:
    model: str
    provider_id: str = ""


class Agents:
    def __init__(self, log=None):
        self.log = log

    async def get(self, agent_id):
        return AgentSpec(id=agent_id or "default", name="Agent", model="fast")

    async def materialize(self, agent_id, generation, **kwargs):
        if self.log is not None:
            self.log.append(f"agent.materialize.{generation.id}")
        return AgentSpec(id=agent_id or "default", name="Agent", model="fast")


class StructuredAgents:
    async def materialize(self, agent_id, generation, **kwargs):
        del generation
        return AgentSpec(
            id=agent_id,
            name=agent_id,
            model=AgentModel(provider="shared", name=f"model-{agent_id}"),
        )


class HookSelectedAgents:
    async def materialize(self, agent_id, generation, **kwargs):
        del generation
        return AgentSpec(
            id=agent_id or "default",
            name="Agent",
            model="fast",
            hooks=("greeting.direct-reply",),
        )


class StructuredResolver:
    def resolve_agent_model(self, selected):
        return Model(selected.name)


class Plugins:
    def __init__(self, generation, log): self.generation = generation; self.log = log
    @asynccontextmanager
    async def lease(self):
        self.log.append("lease.enter")
        try: yield self.generation
        finally: self.log.append("lease.exit")


class Engine:
    async def stream(self, prompt, **kwargs):
        yield RunEvent("token", {"text": "hello"})
        yield RunEvent(
            "usage", {"prompt_tokens": 10, "completion_tokens": 3}
        )
        yield RunEvent("done", {})


class ConnectionFailingEngine:
    async def stream(self, prompt, **kwargs):
        del prompt, kwargs
        raise RuntimeError("Connection error.")
        yield  # pragma: no cover - makes this an async generator


class ProviderTimeoutEngine:
    """Provider 收下了请求却没回包 —— 真实客户端抛 openai.APITimeoutError。"""

    async def stream(self, prompt, **kwargs):
        import httpx
        import openai

        del prompt, kwargs
        raise openai.APITimeoutError(
            request=httpx.Request("POST", "https://api.rvcompute.com/v1/chat/completions")
        )
        yield  # pragma: no cover - makes this an async generator


def _global_skill_generation(*, unavailable_reason: str = "") -> PluginGeneration:
    skill_id = "global.skills/review"
    return PluginGeneration(
        id=21,
        plugins={
            "global.skills": PluginRuntime(
                plugin_id="global.skills",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    skills=(
                        SkillContribution(
                            "global.skills",
                            SkillSpec(
                                name="review",
                                description="Review changes",
                                metadata={
                                    "qualified_id": skill_id,
                                    **(
                                        {"unavailable_reason": unavailable_reason}
                                        if unavailable_reason
                                        else {}
                                    ),
                                },
                            ),
                        ),
                    ),
                ),
            ),
        },
        statuses={},
    )


async def _selected_skill_manager(tmp_path: Path, built_agents: list[AgentSpec]):
    generation = _global_skill_generation()
    agents = InMemoryAgentRegistry(
        (AgentSpec(id="default", name="Default", model="fast", skills=()),)
    )
    models = ModelRegistry(
        default_model=Model("default"), models={"fast": Model("fast")}
    )
    agent_manager = AgentManager(
        store=agents,
        model_registry=models,
        generation_provider=lambda: generation,
    )
    store = SQLiteRunStore(tmp_path / "selected-skills.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=agent_manager,
        model_registry=models,
        engine_builder=lambda agent, *_args: built_agents.append(agent) or Engine(),
        store=store,
    )
    return manager, store


@pytest.mark.asyncio
async def test_selected_global_skill_is_ephemeral_per_run(tmp_path: Path) -> None:
    """Persisting a temporary global Skill into the next Run is a bug."""

    built_agents: list[AgentSpec] = []
    manager, store = await _selected_skill_manager(tmp_path, built_agents)
    selected = await manager.run(
        "session",
        (
            SessionInboxMessage(
                "message-selected", "review this", skill_ids=("global.skills/review",)
            ),
        ),
        agent_id="default",
        request_id="run-selected",
    )
    ordinary = await manager.run(
        "session",
        (SessionInboxMessage("message-ordinary", "continue"),),
        agent_id="default",
        request_id="run-ordinary",
    )

    assert (await selected.wait()).status == "completed"
    assert (await ordinary.wait()).status == "completed"
    assert [agent.skills for agent in built_agents] == [("global.skills/review",), ()]
    # Prompt building distinguishes the attached Skill from an ambient one.
    assert [agent.requested_skills for agent in built_agents] == [
        ("global.skills/review",),
        (),
    ]
    selected_record = await store.get("run-selected")
    assert selected_record is not None
    assert selected_record.request["skill_ids"] == ["global.skills/review"]
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_unavailable_selected_skill_fails_durable_run(tmp_path: Path) -> None:
    """Silently dropping a removed global Skill would execute under false premises."""

    built_agents: list[AgentSpec] = []
    manager, store = await _selected_skill_manager(tmp_path, built_agents)
    handle = await manager.run(
        "session",
        (
            SessionInboxMessage(
                "message-missing", "review this", skill_ids=("global/missing",)
            ),
        ),
        agent_id="default",
        request_id="run-missing",
    )

    result = await handle.wait()
    assert result.status == "failed"
    assert result.error == RunError(
        code="skill_unavailable",
        message="Selected Skills are unavailable: global/missing",
        details={"skill_ids": ["global/missing"]},
        retryable=False,
    )
    assert built_agents == []
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_disabled_selected_skill_fails_before_engine_build(tmp_path: Path) -> None:
    """Direct API callers cannot invoke a Skill hidden as unavailable."""

    built_agents: list[AgentSpec] = []
    generation = _global_skill_generation(unavailable_reason="dependency missing")
    agents = InMemoryAgentRegistry(
        (AgentSpec(id="default", name="Default", model="fast", skills=()),)
    )
    models = ModelRegistry(
        default_model=Model("default"), models={"fast": Model("fast")}
    )
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=AgentManager(
            store=agents,
            model_registry=models,
            generation_provider=lambda: generation,
        ),
        model_registry=models,
        engine_builder=lambda agent, *_args: built_agents.append(agent) or Engine(),
        store=(store := SQLiteRunStore(tmp_path / "disabled-skill.sqlite3")),
    )
    await store.open()
    handle = await manager.run(
        "session",
        (
            SessionInboxMessage(
                "message-disabled",
                "review this",
                skill_ids=("global.skills/review",),
            ),
        ),
        agent_id="default",
        request_id="run-disabled",
    )

    result = await handle.wait()
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "skill_unavailable"
    assert built_agents == []
    await manager.close()
    await store.close()


class CheckpointEngine:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def stream(self, prompt, **kwargs):
        del prompt, kwargs
        self.order.append("engine.stream")
        yield RunEvent("token", {"text": "ok"})


class ObservableEngine(Engine):
    def __init__(self, system_prompt: str) -> None:
        self.system_prompt = system_prompt


class RecordingCheckpointStore(SessionLogStore):
    def __init__(self, path: Path, order: list[str]) -> None:
        super().__init__(path, write_batch_max_delay=60)
        self.order = order

    async def flush_session(self, session_id: str) -> None:
        self.order.append("store.flush")
        await super().flush_session(session_id)


class ConcurrentEngine:
    def __init__(self, state) -> None:
        self.state = state

    async def stream(self, prompt, **kwargs):
        del prompt, kwargs
        self.state["active"] += 1
        self.state["max_active"] = max(
            self.state["max_active"],
            self.state["active"],
        )
        try:
            await asyncio.sleep(0.02)
            yield RunEvent("token", {"text": "done"})
        finally:
            self.state["active"] -= 1


class ToolCheckpointEngine:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def stream(self, _prompt, **_kwargs):
        yield RunEvent(
            "tool_start", {"call_id": "tool-1", "name": "read", "arguments": {}},
        )
        self.order.append("tool.effect")
        yield RunEvent(
            "tool_end", {"call_id": "tool-1", "result": "ok", "status": "success"},
        )
        self.order.append("next.model")
        yield RunEvent("token", {"text": "done"})


@pytest.mark.asyncio
async def test_run_manager_flushes_request_before_model_stream(
    tmp_path: Path,
) -> None:
    """Constructing the model stream before its request is durable is unsafe."""

    order: list[str] = []
    store = RecordingCheckpointStore(tmp_path / "sessions", order)
    await store.open()
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *_args: CheckpointEngine(order),
        store=store,
    )

    handle = await manager.run("session-a", "hello")
    assert (await handle.wait()).status == "completed"

    assert order[:2] == ["store.flush", "engine.stream"]
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_logs_only_changed_model_request_context(
    tmp_path: Path,
) -> None:
    store = SessionLogStore(tmp_path / "sessions")
    await store.open()
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    prompts = iter(("system one", "system one", "system two"))
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("local", "bibit"),
            models={"fast": Model("local", "bibit")},
        ),
        engine_builder=lambda *_args: ObservableEngine(next(prompts)),
        store=store,
    )

    for index in range(3):
        handle = await manager.run(
            "session-a",
            f"message {index}",
            request_id=f"run-{index}",
        )
        assert (await handle.wait()).status == "completed"

    contexts = [
        event
        for event in await store.read_session_events("session-a")
        if event.type == "request/context"
    ]
    assert [event.run_id for event in contexts] == ["run-0", "run-2"]
    assert [event.data["reason"] for event in contexts] == ["initial", "change"]
    assert contexts[0].data["system_prompt"] == "system one"
    assert contexts[0].data["provider"] == "bibit"
    assert contexts[0].data["model"] == "local"
    assert contexts[1].data["system_prompt"] == "system two"

    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_flushes_tool_call_and_result_before_advancing(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    store = RecordingCheckpointStore(tmp_path / "sessions", order)
    await store.open()
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *_args: ToolCheckpointEngine(order),
        store=store,
    )

    handle = await manager.run("session-a", "hello")
    assert (await handle.wait()).status == "completed"

    assert order.index("store.flush", 1) < order.index("tool.effect")
    tool_effect = order.index("tool.effect")
    assert order.index("store.flush", tool_effect + 1) < order.index("next.model")
    await manager.close()
    await store.close()


class FinishFailOnceStore(SQLiteRunStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.fail_request_id = "first"

    async def finish(self, request_id, result) -> None:
        if request_id == self.fail_request_id:
            self.fail_request_id = ""
            raise RuntimeError("synthetic finish failure")
        await super().finish(request_id, result)


async def _park(control, payload: dict[str, object]):
    """Surface one approval request on the live Run and wait for its decision."""

    task = asyncio.ensure_future(control.request_approval(payload))
    surfaced = await control.wait_approval_request()
    return task, surfaced


class ApprovalEngine:
    async def stream(self, prompt, *, control, **_kwargs):
        if prompt is None:
            raise AssertionError("approval resumes the same Run, not a new one")
        task, approval = await _park(
            control,
            {
                "approval_id": "approval-1",
                "actions": [{"allowed_decisions": ["approve", "reject"]}],
            },
        )
        yield RunEvent("approval_required", dict(approval))
        assert await task == [{"type": "approve"}]
        yield RunEvent("token", {"text": "approved"})
        yield RunEvent("done", {})


class WaitingEngine:
    async def stream(self, prompt, *, control, **_kwargs):
        del prompt
        task, approval = await _park(
            control,
            {"approval_id": "hold", "actions": []},
        )
        yield RunEvent("approval_required", dict(approval))
        await task


class DirectReplyEngine:
    def __init__(self) -> None:
        self.stream_calls = 0

    async def stream(self, *_args, **_kwargs):
        self.stream_calls += 1
        raise AssertionError("direct reply must not stream the model")
        yield


async def _wait_for_run_event(
    store: SQLiteRunStore,
    run_id: str,
    kind: str,
):
    for _ in range(100):
        record = await store.get(run_id)
        event = next(
            (item for item in record.events if item.kind == kind),
            None,
        )
        if event is not None:
            return event
        await asyncio.sleep(0.01)
    raise AssertionError(f"Run did not persist {kind}")


@pytest.mark.asyncio
async def test_run_manager_serializes_distinct_engines_on_same_thread(
    tmp_path: Path,
) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    state = {"active": 0, "max_active": 0}
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *_args: ConcurrentEngine(state),
        store=store,
    )

    first = await manager.run("session", "first", thread_id="shared-thread")
    second = await manager.run("session", "second", thread_id="shared-thread")
    await asyncio.gather(first.wait(), second.wait())

    assert state["max_active"] == 1
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_enforces_global_concurrent_run_limit(tmp_path: Path) -> None:
    """Different threads cannot bypass configured Worker admission capacity."""

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    release = asyncio.Event()
    two_started = asyncio.Event()
    state = {"active": 0, "started": 0, "max_active": 0}

    class BlockingEngine:
        async def stream(self, prompt, **kwargs):
            del prompt, kwargs
            state["started"] += 1
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            if state["started"] == 2:
                two_started.set()
            try:
                await release.wait()
                yield RunEvent("token", {"text": "done"})
            finally:
                state["active"] -= 1

    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *_args: BlockingEngine(),
        store=store,
        max_concurrent_runs=2,
    )

    handles = [
        await manager.run(
            f"session-{index}",
            f"run-{index}",
            thread_id=f"thread-{index}",
        )
        for index in range(3)
    ]
    await asyncio.wait_for(two_started.wait(), timeout=1)
    await asyncio.sleep(0.05)

    assert state["started"] == 2
    assert state["max_active"] == 2

    release.set()
    await asyncio.gather(*(handle.wait() for handle in handles))
    assert state["started"] == 3
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_admits_twenty_independent_sessions_without_default_cap(
    tmp_path: Path,
) -> None:
    """The default Worker must not park independent conversations behind slot 4."""

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    release = asyncio.Event()
    started = asyncio.Event()
    state = {"started": 0, "active": 0, "max_active": 0}

    class BlockingEngine:
        async def stream(self, prompt, **kwargs):
            del prompt, kwargs
            state["started"] += 1
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            if state["started"] == 20:
                started.set()
            try:
                await release.wait()
                yield RunEvent("token", {"text": "done"})
            finally:
                state["active"] -= 1

    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *_args: BlockingEngine(),
        store=store,
    )

    handles = [
        await manager.run(
            f"session-{index}",
            f"run-{index}",
            thread_id=f"thread-{index}",
        )
        for index in range(20)
    ]
    await asyncio.wait_for(started.wait(), timeout=2)
    assert state["started"] == 20
    assert state["max_active"] == 20

    release.set()
    await asyncio.gather(*(handle.wait() for handle in handles))
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_queued_run_cancels_without_waiting_for_capacity(tmp_path: Path) -> None:
    """Cancellation completes while another Run still owns the only slot."""

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    started = asyncio.Event()
    release = asyncio.Event()
    engine_builds = 0

    class BlockingEngine:
        async def stream(self, prompt, **kwargs):
            del prompt, kwargs
            started.set()
            await release.wait()
            yield RunEvent("token", {"text": "done"})

    def build_engine(*_args):
        nonlocal engine_builds
        engine_builds += 1
        return BlockingEngine()

    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=build_engine,
        store=store,
        max_concurrent_runs=1,
    )
    first = await manager.run("first", "hold", thread_id="first")
    await asyncio.wait_for(started.wait(), timeout=1)
    queued = await manager.run("second", "cancel", thread_id="second")

    assert await queued.cancel() is True
    result = await asyncio.wait_for(queued.wait(), timeout=1)
    assert result.status == "cancelled"
    assert engine_builds == 1

    release.set()
    await first.wait()
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_thread_lock_releases_when_run_cleanup_fails(tmp_path: Path) -> None:
    store = FinishFailOnceStore(tmp_path / "runs.sqlite3")
    await store.open()
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *_args: Engine(),
        store=store,
    )

    await manager.run(
        "session",
        "first",
        request_id="first",
        thread_id="shared-thread",
    )
    first_task = manager._tasks["first"]
    with pytest.raises(RuntimeError, match="synthetic finish failure"):
        await first_task

    second = await manager.run(
        "session",
        "second",
        request_id="second",
        thread_id="shared-thread",
    )
    result = await asyncio.wait_for(second.wait(), timeout=1)

    assert result.status == "completed"
    manager._active.pop("first", None)
    manager._run_controls.pop("first", None)
    manager._tasks.pop("first", None)
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_resolves_each_agents_remote_model(tmp_path: Path) -> None:
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    selected_models: list[str] = []

    def build(agent, selected_generation, model):
        del agent, selected_generation
        selected_models.append(model.model)
        return Engine()

    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=StructuredAgents(),
        model_registry=ModelRegistry(
            default_model=None,
            resolver=StructuredResolver(),
        ),
        engine_builder=build,
        store=store,
    )

    first = await manager.run("session-a", "hello", agent_id="first")
    second = await manager.run("session-b", "hello", agent_id="second")
    await first.wait()
    await second.wait()

    assert selected_models == ["model-first", "model-second"]
    stored = await store.get(first.request_id)
    assert stored is not None
    assert stored.request["model"] == {
        "provider": "shared",
        "name": "model-first",
    }
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_explains_provider_connection_blocked_by_agent_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping the resolved Provider URL or Agent policy regresses to generic failure."""
    from bianbu_agent.security import security_runtime

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    monkeypatch.setattr(
        security_runtime,
        "security_client_module",
        lambda: SimpleNamespace(
            get_network_policy=lambda agent_id: {
                "agent_id": agent_id,
                "mode": "whitelist",
                "domains": ["api.minimaxi.com"],
            },
        ),
    )
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=None,
            models={
                "fast": OpenAICompatibleModel(
                    model="deepseek-v4-flash",
                    base_url="https://api.rvcompute.com/v1",
                ),
            },
        ),
        engine_builder=lambda *_args: ConnectionFailingEngine(),
        store=store,
    )

    handle = await manager.run("session", "hello", agent_id="agent.demo")
    result = await handle.wait()

    assert result.error == RunError(
        code="network_policy_blocked",
        message=(
            "无法连接到模型服务：当前 Agent 只允许访问指定网站，但模型服务 "
            "api.rvcompute.com 不在白名单中。下一步：打开“安全控制中心 → 配置中心 → "
            "网络策略”，将该域名加入白名单；保存后重启此 Agent 再重试。"
        ),
        details={
            "agent_id": "agent.demo",
            "provider_host": "api.rvcompute.com",
            "network_mode": "whitelist",
            "domains": ["api.minimaxi.com"],
            "action": "add_provider_host_to_whitelist_and_restart_agent",
        },
        # 策略拦下的连接：不改配置直接重试结果一样。
        retryable=False,
    )
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_exposes_provider_timeout_without_policy_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider 无响应时必须给出 provider_timeout，而不是裸的 run_failed。

    这条路径没有网络策略证据（开发态 / Worker 没拿到策略快照），以前只剩
    「run_failed + Request timed out.」，用户看不出是模型服务的问题。
    """
    monkeypatch.setenv("BIANBU_SECURITY", "0")
    generation = type("Generation", (), {"id": 5, "tools": {}, "skills": {}})()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=None,
            models={
                "fast": OpenAICompatibleModel(
                    model="deepseek-v4-flash",
                    base_url="https://api.rvcompute.com/v1",
                ),
            },
        ),
        engine_builder=lambda *_args: ProviderTimeoutEngine(),
        store=store,
    )

    handle = await manager.run("session", "hello", agent_id="agent.demo")
    result = await handle.wait()

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "provider_timeout"
    assert result.error.details["provider_host"] == "api.rvcompute.com"
    assert "api.rvcompute.com" in result.error.message
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_owns_one_generation_and_automatic_lifecycle(tmp_path: Path) -> None:
    log = []
    generation = type("Generation", (), {})()
    generation.id = 7
    generation.tools = {}
    generation.skills = {}
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()

    async def build(agent, selected_generation, model):
        log.append(f"engine.{agent.id}.{selected_generation.id}.{model.model}")
        return Engine()

    manager = RunManager(
        plugin_manager=Plugins(generation, log),
        agent_manager=Agents(log),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=build,
        store=store,
    )
    handle = await manager.run("session-1", "hello", agent_id="reviewer")
    result = await handle.wait()

    assert result.status == "completed"
    assert result.content == "hello"
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 3
    assert result.usage.total_tokens == 13
    assert log == [
        "lease.enter",
        "agent.materialize.7",
        "engine.reviewer.7.fast",
        "lease.exit",
    ]
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_rejects_request_model_override(tmp_path: Path) -> None:
    generation = type("Generation", (), {})()
    generation.id = 7
    generation.tools = generation.skills = {}
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *args: Engine(),
        store=store,
    )

    with pytest.raises(ValueError, match="model override"):
        await manager.run("session", "hello", model="other")

    await store.close()


@pytest.mark.asyncio
async def test_run_handle_resumes_same_generation_after_approval(
    tmp_path: Path,
) -> None:
    log = []
    generation = type("Generation", (), {})()
    generation.id = 9
    generation.tools = generation.skills = {}
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, log),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *args: ApprovalEngine(),
        store=store,
    )

    handle = await manager.run("session", "needs approval")
    kinds = []
    async for event in handle:
        kinds.append(event.kind)
        if event.kind == "approval_required":
            await asyncio.sleep(0)
            assert manager.waiting_count == 1
            assert await handle.approve("approval-1") is True

    result = await handle.wait()
    assert result.status == "completed"
    assert result.content == "approved"
    assert kinds == [
        "approval_required",
        "approval_resolved",
        "token",
        "done",
    ]
    assert log == ["lease.enter", "lease.exit"]
    assert manager.waiting_count == 0
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_invokes_hooks_and_restores_completed_handle(
    tmp_path: Path,
) -> None:
    log = []
    prompts = []
    stream_accounts = []
    hook_contexts = []
    generation = type("Generation", (), {})()
    generation.id = 11
    generation.tools = generation.skills = {}

    async def run_hooks(point, context):
        log.append(point)
        hook_contexts.append((point, context))
        return ContextPatch(
            context_sections=("hook context",)
            if point is HookPoint.AFTER_CONTEXT
            else (),
            metadata=(
                {"route": "cloud", "reason": "open_question"}
                if point is HookPoint.BEFORE_RUN
                else {}
            ),
        )

    generation.run_hooks = run_hooks

    class RecordingEngine:
        async def stream(self, prompt, **kwargs):
            prompts.append(prompt)
            stream_accounts.append(kwargs.get("account_id"))
            yield RunEvent("token", {"text": "ok"})
            yield RunEvent("done", {})

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *args: RecordingEngine(),
        store=store,
    )
    handle = await manager.run(
        "session", "hello", account_id="system-user-42"
    )
    result = await handle.wait()
    assert result.content == "ok"
    assert result.metadata == {"route": "cloud", "reason": "open_question"}
    await manager.close()

    restored = await manager.get(handle.id)
    assert restored is not None
    assert restored.id == handle.id
    restored_result = await restored.wait()
    assert restored_result.content == "ok"
    assert restored_result.metadata == {
        "route": "cloud",
        "reason": "open_question",
    }
    assert [event.kind async for event in restored] == ["token", "done"]
    assert "hook context" in prompts[0]
    assert stream_accounts == ["system-user-42"]
    assert {context.account_id for _, context in hook_contexts} == {
        "system-user-42"
    }
    assert log == [
        HookPoint.BEFORE_RUN,
        HookPoint.AFTER_CONTEXT,
        HookPoint.BEFORE_MODEL,
        HookPoint.AFTER_MODEL,
        HookPoint.AFTER_RUN,
    ]
    after_run = hook_contexts[-1][1]
    assert after_run.request == "hello"
    assert after_run.response == "ok"
    assert after_run.status == "completed"
    await store.close()


@pytest.mark.asyncio
async def test_before_run_hook_and_engine_build_overlap(tmp_path: Path) -> None:
    """The engine build must not queue behind the BEFORE_RUN gate Hooks."""

    order: list[str] = []
    hook_started = asyncio.Event()
    release_hook = asyncio.Event()
    build_started = asyncio.Event()
    generation = type("Generation", (), {})()
    generation.id = 12
    generation.tools = generation.skills = {}

    async def run_hooks(point, context):
        if point is HookPoint.BEFORE_RUN:
            order.append("hook")
            hook_started.set()
            await release_hook.wait()
        return ContextPatch()

    generation.run_hooks = run_hooks

    def build_engine(*_args):
        order.append("engine")
        build_started.set()
        return Engine()

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=build_engine,
        store=store,
    )
    handle = await manager.run("session", "hello")
    await asyncio.wait_for(hook_started.wait(), timeout=1)
    # The Hook is deliberately still blocked: a serial manager would only
    # reach the builder after ``release_hook``.
    await asyncio.wait_for(build_started.wait(), timeout=1)
    release_hook.set()
    result = await asyncio.wait_for(handle.wait(), timeout=5)

    assert result.status == "completed"
    assert result.content == "hello"
    assert order == ["hook", "engine"]
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_synchronous_engine_builder_runs_off_the_event_loop(tmp_path: Path) -> None:
    """A blocking builder must not starve the Hook tasks scheduled beside it.

    ``build_default_engine`` is synchronous (skill DLP over the securityd UDS,
    tool adaptation, graph construction).  Building it inline on the loop
    thread left the BEFORE_RUN Hook tasks unable to make progress until it
    returned, so the "build overlaps the Hooks" promise only held for a
    builder that never blocked.
    """

    order: list[str] = []
    generation = type("Generation", (), {})()
    generation.id = 21
    generation.tools = generation.skills = {}

    async def run_hooks(point, context):
        if point is HookPoint.BEFORE_RUN:
            await asyncio.sleep(0.05)
            order.append("hook")
        return ContextPatch()

    generation.run_hooks = run_hooks

    def build_engine(*_args):
        # Synchronous and slow: an inline build owns the loop thread for the
        # whole sleep, which is what this test forbids.
        time.sleep(0.4)
        order.append("engine")
        return Engine()

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=build_engine,
        store=store,
    )
    handle = await manager.run("session", "hello")
    result = await asyncio.wait_for(handle.wait(), timeout=5)

    assert result.status == "completed"
    # The Hook (50ms) finishes while the builder (400ms) is still sleeping.
    assert order == ["hook", "engine"]
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_denied_before_run_hook_discards_in_flight_engine_build(
    tmp_path: Path,
) -> None:
    """A fail-closed gate still stops the Run and cancels the racing build."""

    build_started = asyncio.Event()
    build_cancelled = asyncio.Event()
    generation = type("Generation", (), {})()
    generation.id = 13
    generation.tools = generation.skills = {}

    async def run_hooks(point, context):
        if point is HookPoint.BEFORE_RUN:
            await build_started.wait()
            raise PermissionError("denied by guard")
        return ContextPatch()

    generation.run_hooks = run_hooks

    async def delayed_engine():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            build_cancelled.set()
            raise
        return Engine()

    def build_engine(*_args):
        build_started.set()
        return delayed_engine()

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=build_engine,
        store=store,
    )
    handle = await manager.run("session", "hello")
    result = await asyncio.wait_for(handle.wait(), timeout=5)

    assert result.status == "failed"
    assert result.error is not None
    assert "denied by guard" in result.error.message
    assert build_cancelled.is_set()
    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_passes_agent_hook_selection_to_every_hook_point(
    tmp_path: Path,
) -> None:
    selections: list[tuple[str, ...]] = []
    generation = type("Generation", (), {"id": 15, "tools": {}, "skills": {}})()

    async def run_hooks(point, context, *, hook_ids):
        del point, context
        selections.append(tuple(hook_ids))
        return ContextPatch()

    generation.run_hooks = run_hooks
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=HookSelectedAgents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *args: Engine(),
        store=store,
    )

    assert (await (await manager.run("session", "hello")).wait()).status == "completed"
    assert selections == [("greeting.direct-reply",)] * 5
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_persists_effects_for_plugin_hook_selection(
    tmp_path: Path,
) -> None:
    def hook(plugin_id: str, qualified_id: str, section: str) -> HookContribution:
        async def run(_context):
            return ContextPatch(context_sections=(section,))

        return HookContribution(
            plugin_id,
            HookPoint.AFTER_CONTEXT,
            run,
            id=qualified_id.rsplit("/", 1)[-1],
            metadata={"qualified_id": qualified_id},
        )

    memory_id = "bianbu.memory/memory.after-context"
    security_id = "bianbu.security/security.after-context"
    generation = PluginGeneration(
        id=16,
        plugins={
            "bianbu.memory": PluginRuntime(
                plugin_id="bianbu.memory",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(hook("bianbu.memory", memory_id, "memory context"),),
                ),
            ),
            "bianbu.security": PluginRuntime(
                plugin_id="bianbu.security",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(hook("bianbu.security", security_id, "security context"),),
                ),
            ),
        },
        statuses={},
    )
    agents = InMemoryAgentRegistry()
    for spec in (
        AgentSpec(id="none", name="None", model="fast", plugins=()),
        AgentSpec(
            id="memory", name="Memory", model="fast",
            plugins=("bianbu.memory",),
        ),
        AgentSpec(id="all", name="All", model="fast", plugins=("*",)),
    ):
        await agents.create(spec)
    agent_manager = AgentManager(
        store=agents,
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        generation_provider=lambda: generation,
    )
    assert (await agent_manager.materialize("all", generation)).hooks == (
        memory_id,
        security_id,
    )

    class PromptEchoEngine:
        async def stream(self, prompt, **kwargs):
            del kwargs
            yield RunEvent("token", {"text": str(prompt)})
            yield RunEvent("done", {})

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=agent_manager,
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *args: PromptEchoEngine(),
        store=store,
    )

    expected = {
        "none": "## 当前请求\nhello",
        "memory": "memory context\n\n## 当前请求\nhello",
        "all": "memory context\n\nsecurity context\n\n## 当前请求\nhello",
    }
    for agent_id, content in expected.items():
        handle = await manager.run(
            "session",
            "hello",
            agent_id=agent_id,
            request_id=f"hooks-{agent_id}",
        )
        assert (await handle.wait()).content == content
        stored = await store.get(handle.request_id)
        assert stored is not None
        assert stored.result.content == content

    await manager.close()
    await store.close()


@pytest.mark.asyncio
async def test_paused_run_nested_tool_hooks_keep_initial_agent_snapshot_after_patch(
    tmp_path: Path,
) -> None:
    """A PATCH while paused cannot change a nested Tool call already in flight."""

    calls: list[str] = []

    def hook(
        local_id: str, point: HookPoint, plugin_id: str = "vendor.hooks"
    ) -> HookContribution:
        async def handler(_context):
            calls.append(local_id)
            return ContextPatch()

        return HookContribution(
            plugin_id,
            point,
            handler,
            id=local_id,
            metadata={"qualified_id": f"{plugin_id}/{local_id}"},
        )

    old_before = "old.before_tool"
    old_after = "old.after_tool"
    generation = PluginGeneration(
        id=17,
        plugins={
            "vendor.hooks": PluginRuntime(
                plugin_id="vendor.hooks",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(
                        hook(old_before, HookPoint.BEFORE_TOOL),
                        hook(old_after, HookPoint.AFTER_TOOL),
                    )
                ),
            ),
            "vendor.new": PluginRuntime(
                plugin_id="vendor.new",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(
                        hook("new.before_tool", HookPoint.BEFORE_TOOL, "vendor.new"),
                        hook("new.after_tool", HookPoint.AFTER_TOOL, "vendor.new"),
                    )
                ),
            ),
        },
        statuses={},
    )
    old_hooks = (f"vendor.hooks/{old_before}", f"vendor.hooks/{old_after}")
    agents = InMemoryAgentRegistry(
        (
            AgentSpec(
                id="agent", name="Agent", model="fast", plugins=("vendor.hooks",),
            ),
        )
    )
    agent_manager = AgentManager(
        store=agents,
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        generation_provider=lambda: generation,
    )

    async def dynamically_resolved_hooks(agent_id: str) -> tuple[str, ...]:
        return (await agent_manager.materialize(agent_id, generation)).hooks

    async def nested_handler(_context, _arguments):
        return ToolResult("nested")

    shared_tool = build_plugin_tools(
        [
            ToolContribution(
                "vendor.tool",
                ToolSpec("nested", "Nested", {"type": "object"}),
                nested_handler,
            )
        ],
        hook_runner=generation,
        hook_ids_resolver=dynamically_resolved_hooks,
    )[0]

    class NestedTool:
        name = "nested"

        async def ainvoke(self, arguments, config):
            return await shared_tool.ainvoke(arguments, config=config)

    class PausingEngine:
        def __init__(self, hook_ids: tuple[str, ...]) -> None:
            self._hook_ids = hook_ids

        async def stream(self, prompt, *, control, **_kwargs):
            if prompt is not None:
                task, approval = await _park(
                    control,
                    {"approval_id": "pause-1", "actions": []},
                )
                yield RunEvent("approval_required", dict(approval))
                assert await task == [{"type": "approve"}]
            runtime = ToolRuntime(
                scope=ToolExecutionScope(
                    tool_name="parent",
                        run_id="paused-run",
                        session_id="session",
                        agent_id="agent",
                    workspace=tmp_path,
                    enabled_tools=("nested",),
                    allowed_skills=None,
                    permissions=frozenset(),
                    hook_ids=self._hook_ids,
                ),
                state_root=tmp_path / "state",
                services=SimpleNamespace(available_tools=[NestedTool()]),
            )
            assert await asyncio.to_thread(runtime.tools.invoke_sync, "nested", {}) == "nested"
            yield RunEvent("done", {})

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=agent_manager,
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda agent, *_args: PausingEngine(agent.hooks),
        store=store,
    )
    try:
        handle = await manager.run(
            "session", "hello", agent_id="agent", request_id="paused-run"
        )
        await _wait_for_run_event(store, handle.request_id, "approval_required")
        await agent_manager.update(
            AgentSpec(id="agent", name="Agent", model="fast", plugins=("vendor.new",))
        )
        assert await handle.approve("pause-1") is True
        result = await handle.wait()
        assert result.status == "completed", result.error
    finally:
        await manager.close()
        await store.close()

    assert calls == [old_before, old_after]


@pytest.mark.asyncio
async def test_real_generation_runs_selected_after_run_hook_for_direct_reply(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def hook(
        local_id: str,
        point: HookPoint,
        *,
        direct: bool = False,
        plugin_id: str = "vendor.hooks",
    ) -> HookContribution:
        async def handler(_context):
            calls.append(local_id)
            return (
                ContextPatch(
                    direct_reply="handled",
                    metadata={"route": "local", "tool": "sysinfo"},
                )
                if direct
                else ContextPatch()
            )

        return HookContribution(
            plugin_id,
            point,
            handler,
            id=local_id,
            metadata={"qualified_id": f"{plugin_id}/{local_id}"},
        )

    generation = PluginGeneration(
        id=18,
        plugins={
            "vendor.hooks": PluginRuntime(
                plugin_id="vendor.hooks",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(
                        hook("direct.before_run", HookPoint.BEFORE_RUN, direct=True),
                        hook("selected.after_run", HookPoint.AFTER_RUN),
                    )
                ),
            ),
            "vendor.other": PluginRuntime(
                plugin_id="vendor.other",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(
                        hook(
                            "other.after_run", HookPoint.AFTER_RUN,
                            plugin_id="vendor.other",
                        ),
                    )
                ),
            ),
        },
        statuses={},
    )
    agents = InMemoryAgentRegistry(
        (
            AgentSpec(
                id="agent",
                name="Agent",
                model="fast",
                plugins=("vendor.hooks",),
            ),
        )
    )
    agent_manager = AgentManager(
        store=agents,
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        generation_provider=lambda: generation,
    )
    engine = DirectReplyEngine()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=agent_manager,
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *_args: engine,
        store=store,
    )
    try:
        result = await (await manager.run("session", "hello", agent_id="agent")).wait()
    finally:
        await manager.close()
        await store.close()

    assert result.content == "handled"
    assert result.metadata == {
        "vendor.hooks": {"route": "local", "tool": "sysinfo"}
    }
    assert engine.stream_calls == 0
    assert calls == ["direct.before_run", "selected.after_run"]


@pytest.mark.asyncio
async def test_real_generation_runs_only_selected_on_error_hook(tmp_path: Path) -> None:
    calls: list[str] = []

    def hook(
        local_id: str, plugin_id: str = "vendor.hooks"
    ) -> HookContribution:
        async def handler(_context):
            calls.append(local_id)
            return ContextPatch()

        return HookContribution(
            plugin_id,
            HookPoint.ON_ERROR,
            handler,
            id=local_id,
            metadata={"qualified_id": f"{plugin_id}/{local_id}"},
        )

    generation = PluginGeneration(
        id=19,
        plugins={
            "vendor.hooks": PluginRuntime(
                plugin_id="vendor.hooks",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(hook("selected.on_error"),)
                ),
            ),
            "vendor.other": PluginRuntime(
                plugin_id="vendor.other",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(hook("other.on_error", "vendor.other"),)
                ),
            ),
        },
        statuses={},
    )
    agents = InMemoryAgentRegistry(
        (
            AgentSpec(
                id="agent",
                name="Agent",
                model="fast",
                plugins=("vendor.hooks",),
            ),
        )
    )
    agent_manager = AgentManager(
        store=agents,
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        generation_provider=lambda: generation,
    )

    class FailingEngine:
        async def stream(self, _prompt, **_kwargs):
            raise RuntimeError("engine failure")
            yield

    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=agent_manager,
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *_args: FailingEngine(),
        store=store,
    )
    try:
        result = await (await manager.run("session", "hello", agent_id="agent")).wait()
    finally:
        await manager.close()
        await store.close()

    assert result.status == "failed"
    assert calls == ["selected.on_error"]


@pytest.mark.asyncio
async def test_direct_reply_persists_and_short_circuits_model_hooks(
    tmp_path: Path,
) -> None:
    hook_points: list[HookPoint] = []
    hook_contexts = []
    generation = type("Generation", (), {"id": 12, "tools": {}, "skills": {}})()

    async def run_hooks(point, context):
        hook_points.append(point)
        hook_contexts.append(context)
        return ContextPatch(
            direct_reply="hello" if point is HookPoint.BEFORE_RUN else None
        )

    generation.run_hooks = run_hooks
    engine = DirectReplyEngine()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *args: engine,
        store=store,
    )

    handle = await manager.run(
        "product-session",
        "  Hi  ",
        request_id="direct-run",
        thread_id="internal-thread",
    )
    events = [event async for event in handle]
    result = await handle.wait()

    assert events == [RunEvent("token", {"text": "hello"})]
    assert result == RunResult(
        request_id="direct-run",
        status="completed",
        content="hello",
    )
    assert result.usage is None
    assert engine.stream_calls == 0
    assert hook_points == [HookPoint.BEFORE_RUN, HookPoint.AFTER_RUN]
    assert hook_contexts[-1].request == "  Hi  "
    assert hook_contexts[-1].response == "hello"
    assert hook_contexts[-1].status == "completed"
    await store.close()


@pytest.mark.asyncio
async def test_run_manager_marks_orphaned_persisted_run_interrupted(
    tmp_path: Path,
) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    await store.start("orphan", "session", {"agent_id": "default"})
    manager = RunManager(
        plugin_manager=object(),
        agent_manager=Agents(),
        model_registry=ModelRegistry(default_model=Model("default")),
        engine_builder=lambda *args: Engine(),
        store=store,
    )
    restored = await manager.get("orphan")
    assert restored is not None
    result = await restored.wait()
    assert result.status == "failed"
    assert result.error.code == "run_interrupted"
    await store.close()


@pytest.mark.asyncio
async def test_run_passes_internal_thread_and_product_session_to_engine(
    tmp_path: Path,
) -> None:
    calls = []
    generation = type("Generation", (), {})()
    generation.id = 1
    generation.tools = generation.skills = {}
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()

    class RecordingEngine:
        async def stream(self, prompt, **kwargs):
            calls.append(kwargs)
            yield RunEvent("token", {"text": "ok"})

    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *args: RecordingEngine(),
        store=store,
    )
    await (
        await manager.run(
            "product-session",
            "hello",
            thread_id="internal-thread",
        )
    ).wait()

    assert calls[0]["session_id"] == "internal-thread"
    assert calls[0]["product_session_id"] == "product-session"
    await store.close()


@pytest.mark.asyncio
async def test_delete_many_preflights_all_runs_before_deleting_any(tmp_path: Path) -> None:
    """An active later Run prevents an atomic deletion group from mutating storage."""

    generation = type("Generation", (), {"id": 1, "tools": {}, "skills": {}})()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    await store.start("completed", "session", {"prompt": "completed"})
    await store.finish(
        "completed",
        RunResult(request_id="completed", status="completed"),
    )
    manager = RunManager(
        plugin_manager=Plugins(generation, []),
        agent_manager=Agents(),
        model_registry=ModelRegistry(
            default_model=Model("default"), models={"fast": Model("fast")}
        ),
        engine_builder=lambda *args: WaitingEngine(),
        store=store,
    )
    active = await manager.run("session", "active", request_id="active")

    assert await manager.delete_many(["completed", active.id]) is False
    assert await store.get("completed") is not None
    assert await store.get(active.id) is not None

    assert await manager.delete_many(["completed"]) is True
    assert await store.get("completed") is None

    await manager.close()
    await store.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('structured', [False, True])
async def test_dlp_mask_reaches_engine_and_preserves_message_identity(tmp_path, structured):
    generation = SimpleNamespace(id=90, tools={}, skills={})
    async def run_hooks(point, context):
        if point is HookPoint.BEFORE_MODEL:
            return ContextPatch(metadata={'bianbu.security': {'securityd': {
                'decision': 'MODIFY', 'dlp_masked_prompt': context.request.replace('13824681357', '[PHONE]')
            }}})
        return ContextPatch()
    generation.run_hooks = run_hooks
    received = []
    class CaptureEngine:
        async def stream(self, prompt, **kwargs):
            received.append(prompt)
            yield RunEvent('done', {})
    store = SQLiteRunStore(tmp_path / 'dlp.sqlite')
    await store.open()
    manager = RunManager(plugin_manager=Plugins(generation, []), agent_manager=Agents(),
                         model_registry=ModelRegistry(default_model=Model('default'), models={'fast': Model('fast')}),
                         engine_builder=lambda *args: CaptureEngine(), store=store)
    inputs = (SessionInboxMessage('message-a', 'phone 13824681357', created_at=12),
              SessionInboxMessage('message-b', 'another 13824681357', created_at=13))
    try:
        result = await (await manager.run('session', inputs if structured else 'phone 13824681357')).wait()
        assert result.status == 'completed'
        if structured:
            assert [(m.message_id, m.content, m.created_at) for m in received[0]] == [
                ('message-a', 'phone [PHONE]', 12), ('message-b', 'another [PHONE]', 13)]
        else:
            assert received == ['## 当前请求\nphone [PHONE]']
    finally:
        await manager.close()
        await store.close()
