"""端侧路由的上下文判定：该问用户时绝不顺手执行端侧动作。

终端/IP 这类请求的执行位置（终端里还是聊天里）属于模型与用户的事，端侧只负责
把话带到；这里钉住「上下文相关请求不许调端侧工具」与「简单请求仍走端侧快路」。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

from bianbu_agent.hooks import HookLoadContext
from tests.builtin_hook_support import ScriptUnitLoader
from bianbu_agent.plugins import RunContext
from bianbu_agent.runs.manager import _prepare_prompt


HOOK_ROOT = (
    Path(__file__).resolve().parents[3]
    / "resources/plugins/bianbu.bibit.intent/hooks/bibit-intent.before_run"
)


def _serve(handler):
    """端侧服务替身：记录收到的请求，handler(method, path, payload) -> (status, body)。"""

    recorded: list[tuple[str, str, object]] = []

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - http.server 的命名约定
            self._handle("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw) if raw else None
            recorded.append((method, self.path, payload))
            status, body = handler(method, self.path, payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    class _Server(ThreadingHTTPServer):
        # 客户端按设计会中途关掉连接（取消、读超时）：别把噪声打进测试输出。
        def handle_error(self, request, client_address) -> None:
            return

    server = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, recorded


def _handled(reply: str) -> tuple[int, bytes]:
    return 200, json.dumps(
        {"handled": True, "reply": reply, "route": "rule", "metadata": {}}
    ).encode()


@pytest.fixture
def edge(monkeypatch):
    """端侧服务替身：任何请求都会被记下来，回一个「已处理」的结果。"""

    server, recorded = _serve(lambda method, path, payload: _handled("ok"))
    monkeypatch.setenv("BIBIT_INTENT_MODE", "service")
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    yield recorded
    server.shutdown()


@pytest.fixture
def hook(tmp_path, monkeypatch):
    monkeypatch.setenv("BIBIT_INTENT_MODE", "service")
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
    "解释终端里查询IP的原理",
    "终端查看IP的原理是什么",
    "写一篇关于终端查询IP的教程",
])
async def test_explanations_do_not_receive_terminal_execution_plan(hook, edge, text):
    patch = await hook.handler(RunContext("run", "session", "agent", text))
    assert patch.direct_reply is None
    assert patch.context_sections == ()
    assert patch.metadata == {"route": "cloud", "reason": "open_question"}
    assert edge == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "能帮我打开终端进行 IP 查询吗",
    "打开终端进行查询ip",
    "打开终端然后查询IP地址",
    "打开终端，在这个终端里执行 ip addr",
    "打开终端，另外查询IP并在聊天里显示结果",
    "不要打开终端，只在这里显示IP",
])
async def test_terminal_query_reaches_model_with_both_plans(hook, edge, text):
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
    # 执行位置没定下来之前不能碰端侧工具。
    assert edge == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["终端里", "在终端里执行", "这里", "在这里显示", "应用里", "聊天中显示结果"])
async def test_short_answer_preserves_history_owned_by_runtime(hook, edge, answer):
    patch = await hook.handler(RunContext("answer-run", "session", "agent", answer))
    assert patch.direct_reply is None
    assert patch.metadata == {"route": "cloud", "reason": "contextual_reply"}
    assert "当前会话确有上述待选任务" in patch.context_sections[0]
    assert "否则询问它指的是哪个任务，不猜测历史" in patch.context_sections[0]
    assert "取消后不要执行旧任务" in patch.context_sections[0]
    assert _prepare_prompt(answer, patch.context_sections).endswith(answer)
    assert edge == []


@pytest.mark.asyncio
async def test_no_plugin_pending_state_leaks_between_sessions(hook, edge):
    await hook.handler(RunContext("request-run", "first-session", "agent", "打开终端查询ip"))
    patch = await hook.handler(RunContext("answer-run", "other-session", "agent", "这里"))
    assert "打开终端查询ip" not in "".join(patch.context_sections)
    assert patch.direct_reply is None
    assert edge == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["查询ip", "打开终端", "打开终端查看器"])
async def test_simple_requests_keep_edge_fast_path(hook, edge, text):
    patch = await hook.handler(RunContext("run", "session", "agent", text))
    assert patch.direct_reply == "ok"
    assert patch.context_sections == ()
    assert edge[0][2]["prompt"] == text


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
