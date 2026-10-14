from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import types
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from bianbu_agent import (
    AgentManager,
    AgentSpec,
    InMemoryAgentRegistry,
    ModelRegistry,
    RunEvent,
    RunManager,
)
from bianbu_agent.hooks import HookLoadContext
from tests.builtin_hook_support import ScriptUnitLoader
from bianbu_agent.inventory import CapabilityOrigin
from bianbu_agent.plugins import (
    PluginContributions,
    PluginGeneration,
    PluginRuntime,
)
from bianbu_agent.runs.stores.sqlite import SQLiteRunStore


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MEMORY_HOOK_ID = "bianbu.memory/memory.before_run"


@dataclass
class _Model:
    model: str = "test"


class _PluginManager:
    def __init__(self, generation: PluginGeneration) -> None:
        self._generation = generation

    @asynccontextmanager
    async def lease(self):
        yield self._generation


class _PromptCapturingEngine:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def stream(self, prompt, **_kwargs):
        self.prompts.append(str(prompt))
        yield RunEvent("done", {})


def _memory_generation(tmp_path: Path) -> PluginGeneration:
    hooks_root = tmp_path / "plugins" / "bianbu.memory" / "hooks"
    hook_root = hooks_root / "memory.before_run"
    shutil.copytree(
        PROJECT_ROOT
        / "resources"
        / "plugins"
        / "bianbu.memory"
        / "hooks"
        / "memory.before_run",
        hook_root,
    )
    after_run_root = hooks_root / "memory.after_run"
    shutil.copytree(
        PROJECT_ROOT
        / "resources"
        / "plugins"
        / "bianbu.memory"
        / "hooks"
        / "memory.after_run",
        after_run_root,
    )
    data_root = tmp_path / "workspace" / "plugin-data" / "bianbu.memory" / "data"
    data_root.mkdir(parents=True)
    (data_root / "bianbu-memory.json").write_text(
        json.dumps({"user": ["calls me Ada"], "memory": ["prefers tea"]}),
        encoding="utf-8",
    )
    (data_root / "memory.jsonl").write_text(
        json.dumps({"content": "finished onboarding"}) + "\n",
        encoding="utf-8",
    )
    contribution = ScriptUnitLoader().load(
        hook_root,
        context=HookLoadContext(
            workspace_root=tmp_path / "workspace",
            data_root=data_root,
            owner_plugin_id="bianbu.memory",
        ),
        origin=CapabilityOrigin.PLUGIN,
        plugin_id="bianbu.memory",
    )
    after_run = ScriptUnitLoader().load(
        after_run_root,
        context=HookLoadContext(
            workspace_root=tmp_path / "workspace",
            data_root=data_root,
            owner_plugin_id="bianbu.memory",
        ),
        origin=CapabilityOrigin.PLUGIN,
        plugin_id="bianbu.memory",
    )
    return PluginGeneration(
        id=1,
        plugins={
            "bianbu.memory": PluginRuntime(
                plugin_id="bianbu.memory",
                instance=object(),
                config={},
                contributions=PluginContributions(hooks=(contribution, after_run)),
            )
        },
        statuses={},
    )


@pytest.mark.asyncio
async def test_memory_hook_injects_context_only_for_agents_that_select_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping Hook selection must keep durable Plugin data out of the model prompt."""

    # This test exercises Runtime's built-in JSON/JSONL backend. The AgentMemory
    # integration is enabled by default and has separate adapter coverage.
    monkeypatch.setenv("BIANBU_AGENT_MEMORY_ENABLED", "false")

    generation = _memory_generation(tmp_path)
    agents = InMemoryAgentRegistry(
        (
            AgentSpec(id="none", name="None", model="test"),
            AgentSpec(
                id="memory",
                name="Memory",
                model="test",
                plugins=("bianbu.memory",),
            ),
            AgentSpec(id="all", name="All", model="test", plugins=("*",)),
        )
    )
    manager = AgentManager(
        store=agents,
        model_registry=ModelRegistry(
            default_model=_Model(),
            models={"test": _Model()},
        ),
        generation_provider=lambda: generation,
    )
    engine = _PromptCapturingEngine()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    runs = RunManager(
        plugin_manager=_PluginManager(generation),
        agent_manager=manager,
        model_registry=ModelRegistry(
            default_model=_Model(),
            models={"test": _Model()},
        ),
        engine_builder=lambda *_args: engine,
        store=store,
    )

    try:
        selected_memory = await manager.materialize("memory", generation)
        selected_all = await manager.materialize("all", generation)
        prompts: dict[str, str] = {}
        for agent_id in ("none", "memory", "all"):
            handle = await runs.run(
                "session-1",
                "What do you remember?",
                agent_id=agent_id,
                request_id=f"memory-{agent_id}",
            )
            assert (await handle.wait()).status == "completed"
            prompts[agent_id] = engine.prompts[-1]
    finally:
        await runs.close()
        await store.close()

    assert selected_memory.hooks == (
        "bianbu.memory/memory.after_run",
        MEMORY_HOOK_ID,
    )
    assert selected_all.hooks == (
        "bianbu.memory/memory.after_run",
        MEMORY_HOOK_ID,
    )
    assert prompts["none"] == "## 当前请求\nWhat do you remember?"
    for durable_text in ("calls me Ada", "prefers tea", "finished onboarding"):
        assert durable_text not in prompts["none"]
        assert durable_text in prompts["memory"]
        assert durable_text in prompts["all"]


@pytest.mark.asyncio
async def test_memory_hook_agent_memory_branch_injects_recall_and_writes_run_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentMemory 默认只注入 L3 召回，并写 run-start 水位。

    其它会话的状态与标题不再进 prompt；近轮转录与历史会话内容都由
    session_search 按需拉取。run-start 水位让执行中的首轮 run 对其它会话可见。
    """

    class _FakeBridge:
        def __init__(self) -> None:
            self.run_started: list[dict] = []

        async def settings(self) -> dict:
            return {"memory_enabled": True}

        async def recall(self, query, *, top_k=5, max_chars=4000):
            return (
                "[preference slot=preference.terminal] 终端颜色偏好为浅色",
                "[fact rev=1 source=agent score=0.015] RT0190_MEMORY_E2E_deadbeef",
            )

        async def record_run_started(self, **kwargs):
            self.run_started.append(kwargs)
            return True

        async def record_turn(self, **kwargs):
            return True

        async def record_interrupted_turn(self, **kwargs):
            return True

    bridge = _FakeBridge()

    async def _fake_get_memory_bridge(**_kwargs):
        return bridge

    fake_sdk = types.ModuleType("bianbu_agent_sdk")
    fake_memory = types.ModuleType("bianbu_agent_sdk.memory")
    fake_memory.get_memory_bridge = _fake_get_memory_bridge
    fake_sdk.memory = fake_memory
    monkeypatch.setitem(sys.modules, "bianbu_agent_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "bianbu_agent_sdk.memory", fake_memory)
    monkeypatch.delenv("BIANBU_AGENT_MEMORY_ENABLED", raising=False)

    generation = _memory_generation(tmp_path)
    agents = InMemoryAgentRegistry(
        (
            AgentSpec(
                id="memory",
                name="Memory",
                model="test",
                plugins=("bianbu.memory",),
            ),
        )
    )
    manager = AgentManager(
        store=agents,
        model_registry=ModelRegistry(
            default_model=_Model(),
            models={"test": _Model()},
        ),
        generation_provider=lambda: generation,
    )
    engine = _PromptCapturingEngine()
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    runs = RunManager(
        plugin_manager=_PluginManager(generation),
        agent_manager=manager,
        model_registry=ModelRegistry(
            default_model=_Model(),
            models={"test": _Model()},
        ),
        engine_builder=lambda *_args: engine,
        store=store,
    )

    try:
        handle = await runs.run(
            "session-1",
            "帮我看看当前进度",
            agent_id="memory",
            request_id="agent-memory-run-1",
        )
        assert (await handle.wait()).status == "completed"

        history_handle = await runs.run(
            "session-1",
            "上一个会话我们聊了什么",
            agent_id="memory",
            request_id="agent-memory-run-2",
        )
        assert (await history_handle.wait()).status == "completed"
    finally:
        await runs.close()
        await store.close()

    prompt = engine.prompts[-2]
    assert "## Memory application policy" in prompt
    assert "not the current conversation" in prompt
    assert "do not recite" in prompt
    assert "execution state" in prompt
    assert "## Recent session status" not in prompt
    assert "帮我开发一个象棋游戏" not in prompt
    assert "session-chess" not in prompt
    assert "## Recent persisted conversation context" not in prompt
    assert "## AgentMemory durable memories" in prompt
    assert "终端颜色偏好为浅色" in prompt
    # 有界窗口：注入段必须自述"这不是全库"，并把完整清单指回 memory_retrieve。
    assert "not the whole store" in prompt
    assert "memory_retrieve" in prompt
    # B4 消费侧：kind=fact 的自由笔记（测试/崩溃残留）不进上下文。
    assert "RT0190_MEMORY_E2E_deadbeef" not in prompt

    history_prompt = engine.prompts[-1]
    assert "## Session history request" in history_prompt
    assert "session_search" in history_prompt
    assert "## Recent session status" not in history_prompt

    for _ in range(200):
        if bridge.run_started:
            break
        await asyncio.sleep(0.01)
    assert bridge.run_started, "run-start watermark was not written"
    watermark = bridge.run_started[0]
    assert watermark["run_id"] == "agent-memory-run-1"
    assert watermark["session_id"] == "session-1"
    assert watermark["user_text"] == "帮我看看当前进度"
# The script-unit harness used by the other tests in this file downgrades a
# raising command Hook to a warning, so a harness-driven test keeps passing while
# the production command (``exec "$RUNTIME_PYTHON" "$PLUGIN_ROOT/scripts/before_run.py"``)
# exits 1 and the host fails the whole Run.  Exercise the plugin script directly.
PLUGIN_SCRIPT = (
    PROJECT_ROOT / "resources" / "plugins" / "bianbu.memory" / "scripts" / "before_run.py"
)


def _load_plugin_script():
    spec = importlib.util.spec_from_file_location("bianbu_memory_before_run", PLUGIN_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _SwitchedOffBridge:
    def __init__(self, settings: dict) -> None:
        self._settings = settings

    async def settings(self) -> dict:
        return dict(self._settings)

    async def recall(self, *_args, **_kwargs):
        raise AssertionError("recall must not run while the backend switch is off")

    async def record_run_started(self, **_kwargs):
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "user_text", "case"),
    (
        ({"memory_enabled": False}, "你好，请只回复：收到", "backend switch off"),
        (
            {
                "memory_enabled": True,
                "preference_auto_apply_enabled": False,
                "development_habit_auto_apply_enabled": False,
            },
            "你好，请只回复：收到",
            "both auto-apply switches off",
        ),
        ({"memory_enabled": True}, "", "empty query"),
    ),
)
async def test_memory_hook_keeps_the_run_alive_when_the_backend_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
    settings: dict,
    user_text: str,
    case: str,
) -> None:
    """关掉记忆只应停掉记忆，不能让整轮 Run 失败。

    0.1.129 的 ``_load_agent_memory`` 在下面三条提前返回路径上写成 ``return ()``
    （空元组），调用方 ``recalled, withheld = ...`` 于是抛出
    ``ValueError: not enough values to unpack (expected 2, got 0)``；Hook 进程因此
    exit 1，宿主把命令 Hook 判为致命，整个 Run 变 ``run_failed``——用户在设置页
    关掉记忆后连"你好"都收不到回复。这里直接调用脚本，三条路径都要能降级为
    "不注入记忆"，而不是抛异常。
    """

    bridge = _SwitchedOffBridge(settings)

    async def _fake_get_memory_bridge(**_kwargs):
        return bridge

    fake_sdk = types.ModuleType("bianbu_agent_sdk")
    fake_memory = types.ModuleType("bianbu_agent_sdk.memory")
    fake_memory.get_memory_bridge = _fake_get_memory_bridge
    fake_sdk.memory = fake_memory
    monkeypatch.setitem(sys.modules, "bianbu_agent_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "bianbu_agent_sdk.memory", fake_memory)
    monkeypatch.delenv("BIANBU_AGENT_MEMORY_ENABLED", raising=False)

    plugin = _load_plugin_script()
    patch = await plugin.before_run(
        {
            "run_id": "memory-off-1",
            "session_id": "session-1",
            "request": user_text,
            "account_id": "account-1",
            "agent_id": "agent-1",
            "metadata": {},
            "config": {},
        }
    )

    # Nothing is injected, and the Hook does not fail the Run.
    assert patch is None, case

