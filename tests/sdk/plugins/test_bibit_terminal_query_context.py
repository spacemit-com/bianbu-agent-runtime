from pathlib import Path
from types import SimpleNamespace

import pytest

from bianbu_agent.hooks import HookLoadContext
from tests.builtin_hook_support import ScriptUnitLoader
from bianbu_agent.plugins import RunContext
from bianbu_agent.runs.manager import _prepare_prompt


HOOK_ROOT = (
    Path(__file__).resolve().parents[3]
    / "resources/plugins/bianbu.bibit.intent/hooks/bibit-intent.before_run"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "解释终端里查询IP的原理",
    "终端查看IP的原理是什么",
    "写一篇关于终端查询IP的教程",
])
async def test_explanations_do_not_receive_terminal_execution_plan(hook, monkeypatch, text):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"handled": False, "route": "cloud", "metadata": {}},
        )

    monkeypatch.setattr("httpx.post", post)
    patch = await hook.handler(RunContext("run", "session", "agent", text))
    assert patch.direct_reply is None
    assert patch.context_sections == ()
    assert patch.metadata == {"route": "cloud", "reason": "open_question"}
    assert not calls


@pytest.fixture
def hook(tmp_path, monkeypatch):
    monkeypatch.setenv("BIBIT_INTENT_MODE", "service")

    def reject_edge_call(*args, **kwargs):
        raise AssertionError("Context-dependent queries must not execute edge tools")

    monkeypatch.setattr("httpx.AsyncClient.post", reject_edge_call)
    return ScriptUnitLoader().load(
        HOOK_ROOT,
        context=HookLoadContext(
            workspace_root=tmp_path,
            data_root=tmp_path / "data",
            owner_plugin_id="bianbu.bibit.intent",
        ),
        plugin_id="bianbu.bibit.intent",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "能帮我打开终端进行 IP 查询吗",
    "打开终端进行查询ip",
    "打开终端然后查询IP地址",
    "打开终端，在这个终端里执行 ip addr",
    "打开终端，另外查询IP并在聊天里显示结果",
    "不要打开终端，只在这里显示IP",
])
async def test_terminal_query_reaches_model_with_both_plans(hook, text):
    patch = await hook.handler(RunContext("run", "session", "agent", text))
    assert patch.direct_reply is None
    assert patch.metadata == {"route": "cloud", "reason": "multi_intent"}
    assert len(patch.context_sections) == 2
    assert "设备操作结果的真实性约束" in patch.context_sections[1]
    prompt = _prepare_prompt(text, patch.context_sections)
    assert "A. 打开终端，在该终端中执行查询命令" in prompt
    assert "B. 打开终端，另外调用只读查询工具" in prompt
    assert "等待回答前不要打开终端或执行查询" in prompt
    assert "不重复询问" in prompt
    assert "不擅自将 A 改成 B" in prompt
    assert prompt.endswith(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["终端里", "在终端里执行", "这里", "在这里显示", "应用里", "聊天中显示结果"])
async def test_short_answer_preserves_history_owned_by_runtime(hook, answer):
    patch = await hook.handler(RunContext("answer-run", "session", "agent", answer))
    assert patch.direct_reply is None
    assert patch.metadata == {"route": "cloud", "reason": "contextual_reply"}
    assert "当前会话确有上述待选任务" in patch.context_sections[0]
    assert "否则询问它指的是哪个任务，不猜测历史" in patch.context_sections[0]
    assert "取消后不要执行旧任务" in patch.context_sections[0]
    assert _prepare_prompt(answer, patch.context_sections).endswith(answer)


@pytest.mark.asyncio
async def test_no_plugin_pending_state_leaks_between_sessions(hook):
    await hook.handler(RunContext("request-run", "first-session", "agent", "打开终端查询ip"))
    patch = await hook.handler(RunContext("answer-run", "other-session", "agent", "这里"))
    assert "打开终端查询ip" not in "".join(patch.context_sections)
    assert patch.direct_reply is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["查询ip", "打开终端", "打开终端查看器"])
async def test_simple_requests_keep_edge_fast_path(hook, monkeypatch, text):
    calls = []

    async def post(self, url, **kwargs):
        calls.append(kwargs["json"])
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"handled": True, "reply": "ok", "route": "rule", "metadata": {}},
        )

    monkeypatch.setattr("httpx.AsyncClient.post", post)
    patch = await hook.handler(RunContext("run", "session", "agent", text))
    assert patch.direct_reply == "ok"
    assert patch.context_sections == ()
    assert calls[0]["prompt"] == text


@pytest.mark.asyncio
async def test_legacy_embedded_setting_does_not_restore_in_process_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("BIBIT_INTENT_MODE", "embedded")

    class Pipeline:
        def handle(self, text, session_id):
            raise AssertionError("Ambiguous requests must not start local actions")

    embedded_hook = ScriptUnitLoader().load(
        HOOK_ROOT,
        context=HookLoadContext(
            workspace_root=tmp_path,
            data_root=tmp_path / "data",
            owner_plugin_id="bianbu.bibit.intent",
            services={"bibit_pipeline": Pipeline()},
        ),
        plugin_id="bianbu.bibit.intent",
    )
    first = await embedded_hook.handler(RunContext("run", "session", "agent", "打开终端查询ip"))
    second = await embedded_hook.handler(RunContext("reply", "session", "agent", "终端里"))
    assert first.metadata["reason"] == "multi_intent"
    assert second.metadata["reason"] == "contextual_reply"
    assert first.direct_reply is None and second.direct_reply is None
    assert first.context_sections[0] == second.context_sections[0]
    assert "设备操作结果的真实性约束" in first.context_sections[1]
