"""M3-1：写完代码后的只读依赖提醒 Hook（bianbu.bibit.intent/bibit-intent.after_tool）。"""
from __future__ import annotations

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

from bianbu_agent.plugins.manifest import load_installed_plugin_manifest

PLUGIN_ROOT = (
    Path(__file__).resolve().parents[3] / "resources" / "plugins" / "bianbu.bibit.intent"
)
SCRIPT = PLUGIN_ROOT / "scripts" / "after_tool.py"
HOOK_ROOT = PLUGIN_ROOT / "hooks" / "bibit-intent.after_tool"


def _load_module():
    spec = importlib.util.spec_from_file_location("_test_bibit_after_tool", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def after_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("BIBIT_INTENT_DEP_NOTICE", raising=False)
    return _load_module()


def _serve(payload: dict, requests: list[dict]):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            requests.append({"path": self.path, "body": json.loads(self.rfile.read(length))})
            body = json.dumps(payload).encode()
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
    return server


@pytest.mark.asyncio
async def test_code_write_attaches_missing_dependency_notice(
    after_tool, tmp_path, monkeypatch,
):
    requests: list[dict] = []
    server = _serve(
        {
            "handled": True,
            "reply": (
                f"依赖提醒【未安装】：{tmp_path} 还缺 1 项依赖——requests（pip）；"
                "仅提示，未安装。\n"
                f'{{"root": "{tmp_path}", "notice": "missing"}}'
            ),
            "route": "rule",
        },
        requests,
    )
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        patch = await after_tool.after_tool({
            "tool_input": {"path": "app.py", "content": "import requests\n"},
            "state": {},
        })
    finally:
        server.shutdown()

    assert requests and requests[0]["path"] == "/v1/runs"
    body = requests[0]["body"]
    assert body["prompt"] == f"依赖提醒 {tmp_path}"
    assert body["session_id"] == "bibit-dep-notice"
    assert body["stream"] is False
    # 低于 executor 的最小执行预算（40s）会被判 edge_deadline_exceeded
    assert body["timeout_ms"] >= 45_000
    section = patch["context_sections"][0]
    assert section.startswith("依赖提醒【未安装】：")
    assert "不要自行安装" in section
    assert "{" not in section.splitlines()[0], "detail JSON 不该进上下文"
    assert patch["metadata"]["dep_notice"]["status"] == "missing"
    assert patch["state"]["dep_notice"]["root"] == str(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_input",), [
    ({"path": "README.md", "content": "docs"},),
    ({"path": "notes.txt", "content": "x"},),  # 纯文本不算代码
    ({"path": "../outside/app.py", "content": "x"},),
])
async def test_non_code_or_outside_targets_are_ignored(after_tool, tool_input, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("不该为这类写入发起依赖扫描")

    monkeypatch.setattr(after_tool, "_notice", fail)

    assert await after_tool.after_tool({"tool_input": tool_input, "state": {}}) is None


@pytest.mark.asyncio
async def test_v4a_patch_body_paths_trigger_the_scan(after_tool, monkeypatch):
    calls: list[Path] = []

    async def fake_notice(root: Path) -> str:
        calls.append(root)
        return "依赖提醒【已满足】：x"

    monkeypatch.setattr(after_tool, "_notice", fake_notice)
    patch = await after_tool.after_tool({
        "tool_input": {"mode": "patch", "patch": "*** Update File: src/app.py\n+import requests\n"},
        "state": {},
    })

    assert calls and (patch.get("context_sections") or []) == []

    monkeypatch.setattr(after_tool, "_notice", fail_never_called)
    assert await after_tool.after_tool({
        "tool_input": {"mode": "patch", "patch": "*** Update File: docs/guide.md\n+x\n"},
        "state": {},
    }) is None


async def fail_never_called(root: Path) -> str:
    raise AssertionError("非代码补丁不该触发扫描")


@pytest.mark.asyncio
async def test_same_workspace_is_scanned_once_per_window(after_tool, monkeypatch, tmp_path):
    calls: list[Path] = []

    async def fake_notice(root: Path) -> str:
        calls.append(root)
        return "依赖提醒【未安装】：x 还缺 1 项依赖——requests（pip）；仅提示，未安装。"

    monkeypatch.setattr(after_tool, "_notice", fake_notice)

    first = await after_tool.after_tool({"tool_input": {"path": "a.py"}, "state": {}})
    second = await after_tool.after_tool({"tool_input": {"path": "b.py"}, "state": first["state"]})

    assert second is None
    assert len(calls) == 1

    stale = {"dep_notice": {"root": str(tmp_path), "at": 0}}
    third = await after_tool.after_tool({"tool_input": {"path": "c.py"}, "state": stale})

    assert len(calls) == 2 and third is not None


@pytest.mark.asyncio
async def test_satisfied_reply_records_state_without_context(after_tool, monkeypatch, tmp_path):
    async def fake_notice(root: Path) -> str:
        return f"依赖提醒【已满足】：{root} 的 2 项依赖都已满足。"

    monkeypatch.setattr(after_tool, "_notice", fake_notice)

    patch = await after_tool.after_tool({"tool_input": {"path": "app.py"}, "state": {}})

    assert (patch.get("context_sections") or []) == []
    assert "metadata" not in patch
    assert patch["state"]["dep_notice"]["root"] == str(tmp_path)


@pytest.mark.asyncio
async def test_transport_failure_never_breaks_the_tool_call(after_tool, monkeypatch):
    async def boom(root: Path) -> str:
        raise OSError("connection refused")

    monkeypatch.setattr(after_tool, "_notice", boom)

    patch = await after_tool.after_tool({"tool_input": {"path": "app.py"}, "state": {}})

    assert (patch.get("context_sections") or []) == []
    assert patch["state"]["dep_notice"]["at"] > 0


@pytest.mark.asyncio
async def test_unhandled_service_response_is_silent(after_tool, monkeypatch):
    requests: list[dict] = []
    server = _serve({"handled": False, "reply": ""}, requests)
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        patch = await after_tool.after_tool({"tool_input": {"path": "app.py"}, "state": {}})
    finally:
        server.shutdown()

    assert requests and (patch.get("context_sections") or []) == []


@pytest.mark.asyncio
async def test_opt_out_environment_variable(after_tool, monkeypatch):
    monkeypatch.setenv("BIBIT_INTENT_DEP_NOTICE", "0")

    assert await after_tool.after_tool({"tool_input": {"path": "app.py"}, "state": {}}) is None


@pytest.mark.asyncio
async def test_hook_runs_through_the_real_command_loader(tmp_path, monkeypatch):
    """真实 loader + 真实脚本 + 假 bibit 服务：验证 hook.json、子进程协议与 state 去抖。"""
    from bianbu_agent.hooks.loader import DirectoryHookLoader
    from bianbu_agent.hooks.models import HookLoadContext
    from bianbu_agent.inventory.models import CapabilityOrigin
    from bianbu_agent.plugins.contributions import RunContext

    requests: list[dict] = []
    server = _serve(
        {
            "handled": True,
            "reply": f"依赖提醒【未安装】：{tmp_path} 还缺 1 项依赖——requests（pip）；仅提示，未安装。",
        },
        requests,
    )
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    hook = DirectoryHookLoader().load(
        HOOK_ROOT,
        origin=CapabilityOrigin.PLUGIN,
        plugin_id="bianbu.bibit.intent",
        context=HookLoadContext(
            workspace_root=tmp_path,
            data_root=tmp_path / "data",
        ),
    )
    write = RunContext(
        "run", "session", "agent", "write_file",
        metadata={"tool": "write_file", "arguments": {"path": "app.py"}},
    )
    try:
        patch = await hook.handler(write)
        again = await hook.handler(write)
    finally:
        server.shutdown()

    assert patch.context_sections[0].startswith("依赖提醒【未安装】：")
    # loader 无论如何都回一个 patch，去抖的效果是「没有第二次扫描」
    assert again.context_sections == () and len(requests) == 1
    assert await hook.handler(RunContext(
        "run", "session", "agent", "read_file",
        metadata={"tool": "read_file", "arguments": {"path": "app.py"}},
    )) is None


def test_hook_is_discovered_for_file_writing_tools():
    hook = json.loads((HOOK_ROOT / "hook.json").read_text(encoding="utf-8"))
    groups = hook["hooks"]["PostToolUse"]

    assert {group["matcher"] for group in groups} == {"write_file", "patch"}
    assert all(
        "after_tool.py" in command["command"]
        for group in groups
        for command in group["hooks"]
    )
    manifest = load_installed_plugin_manifest(PLUGIN_ROOT)
    assert "bibit-intent.after_tool" in manifest.contributions.hooks
    assert "contributions" not in (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
