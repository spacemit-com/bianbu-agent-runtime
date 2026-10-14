from __future__ import annotations

import builtins
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from pathlib import Path

import pytest

from bianbu_agent.hooks import HookLoadContext
from tests.builtin_hook_support import ScriptUnitLoader
from bianbu_agent.plugins import RunContext

import sys


PLUGIN_ROOT = (
    Path(__file__).resolve().parents[3]
    / "resources"
    / "plugins"
    / "bianbu.bibit.intent"
)


def test_plugin_ships_readonly_control_admin_mcps():
    mcp_root = PLUGIN_ROOT / "mcps"
    manifest_dirs = sorted(path.name for path in mcp_root.iterdir() if path.is_dir())

    assert manifest_dirs == ["bibit-admin", "bibit-control", "bibit-readonly"]
    for name in manifest_dirs:
        document = json.loads((mcp_root / name / "mcp.json").read_text(encoding="utf-8"))
        assert document["transport"] == "stdio"
        assert document["command"] == "/opt/bibit-intent/.venv/bin/python"
        assert document["args"] == ["-m", "bibit_intent.mcp_server"]
        assert document["env"]["PYTHONPATH"] == "/opt/bibit-intent/src"
        assert document["env"]["BIBIT_MCP_TRANSPORT"] == "stdio"
        assert document["env"]["BIBIT_MCP_GROUPS"] == name.removeprefix("bibit-")
        assert document["env"]["PATH"].split(":")[0] == "/opt/bibit-intent/.venv/bin"


def _load_hook(tmp_path, pipeline):
    return ScriptUnitLoader().load(
        PLUGIN_ROOT / "hooks" / "bibit-intent.before_run",
        context=HookLoadContext(
            workspace_root=tmp_path,
            data_root=tmp_path / "plugin-data",
            owner_plugin_id="bianbu.bibit.intent",
            services={"bibit_pipeline": pipeline},
        ),
        plugin_id="bianbu.bibit.intent",
    )


@pytest.mark.asyncio
async def test_service_hook_runs_without_importing_external_package(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    response = {
        "run_id": "bibit-run",
        "handled": False,
        "reply": "",
        "route": "cloud",
        "reason": "writing_task",
        "metadata": {"latency_ms": 8},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/runs"
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length))
            assert payload == {
                "run_id": payload["run_id"],
                "session_id": "service-session",
                "prompt": "搜索AI资讯并生成可视化网页报告",
                "timeout_ms": 90000,
                "stream": False,
            }
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_import = builtins.__import__

    def reject_external_import(name, *args, **kwargs):
        if name == "bibit_intent" or name.startswith("bibit_intent."):
            raise AssertionError("service mode must not import bibit_intent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_external_import)
    monkeypatch.setenv("BIBIT_INTENT_MODE", "service")
    monkeypatch.setenv(
        "BIBIT_INTENT_BASE_URL",
        f"http://127.0.0.1:{server.server_port}",
    )
    monkeypatch.setenv("BIBIT_INTENT_SRC", str(tmp_path / "missing-bibit-src"))
    for name in tuple(sys.modules):
        if name == "bibit_intent" or name.startswith("bibit_intent."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    try:
        hook = _load_hook(tmp_path, None)
        patch = await hook.handler(
            RunContext(
                "run",
                "service-session",
                "agent",
                "搜索AI资讯并生成可视化网页报告",
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert patch.direct_reply is None
    assert patch.metadata == {
        "route": "cloud",
        "reason": "writing_task",
        "latency_ms": 8,
    }


@pytest.mark.asyncio
async def test_service_hook_falls_back_when_local_service_is_unavailable(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BIBIT_INTENT_MODE", "service")
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("BIBIT_INTENT_SRC", str(tmp_path / "missing-bibit-src"))

    hook = _load_hook(tmp_path, None)
    patch = await hook.handler(
        RunContext("run", "service-down", "agent", "hello")
    )

    assert patch.direct_reply is None
    assert patch.metadata == {
        "route": "cloud",
        "reason": "edge_error",
        "edge_error_type": "ConnectError",
    }


def _serve_once(response: dict) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


@pytest.mark.asyncio
async def test_cloud_fallback_injects_side_effect_reality_guard(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    # 回归：端侧 app_not_found 上云后，模型曾不调用工具却复述"已启动应用"。
    # 云端兜底必须在上下文里注入真实性约束。
    server = _serve_once({
        "run_id": "bibit-run",
        "handled": False,
        "reply": "",
        "route": "cloud",
        "reason": "app_not_found",
        "metadata": {},
    })
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BIBIT_INTENT_MODE", "service")
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("BIBIT_INTENT_SRC", str(tmp_path / "missing-bibit-src"))
    try:
        hook = _load_hook(tmp_path, None)
        patch = await hook.handler(
            RunContext("run", "cloud-guard", "agent", "打开终端和浏览器")
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert patch.direct_reply is None
    assert patch.metadata["route"] == "cloud"
    assert any("已打开" in section for section in patch.context_sections)
    assert any("launch_apps" in section for section in patch.context_sections)


@pytest.mark.asyncio
async def test_cloud_fallback_skips_guard_for_open_questions(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
):
    server = _serve_once({
        "run_id": "bibit-run",
        "handled": False,
        "reply": "",
        "route": "cloud",
        "reason": "open_question",
        "metadata": {},
    })
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BIBIT_INTENT_MODE", "service")
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("BIBIT_INTENT_SRC", str(tmp_path / "missing-bibit-src"))
    try:
        hook = _load_hook(tmp_path, None)
        patch = await hook.handler(
            RunContext("run", "cloud-plain", "agent", "讲讲操作系统的历史")
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert patch.direct_reply is None
    assert patch.context_sections == ()
