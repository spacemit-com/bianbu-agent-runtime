"""H13：appcli 产物的只读独立复核 Hook（bianbu.bibit.intent/bibit-intent.after_appcli）。

Hook 换一条路径重新读盘（只读 `appcli_verify`），把「工具/模型都说成功、盘上没有合格
文件」的情况变成一句纠正，附在 appcli_run 的工具结果里。
"""
from __future__ import annotations

import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

PLUGIN_ROOT = (
    Path(__file__).resolve().parents[3] / "resources" / "plugins" / "bianbu.bibit.intent"
)
SCRIPT = PLUGIN_ROOT / "scripts" / "after_appcli.py"
HOOK_ROOT = PLUGIN_ROOT / "hooks" / "bibit-intent.after_appcli"

FAILED_REPLY = (
    "校验未通过：/ws/build/out.pdf\n"
    "文件：/ws/build/out.pdf\n"
    "（按 libreoffice/export_render 的 2 条规则校验）\n"
    "  exists: 文件存在\n"
    "  pdf_magic: 缺少 %PDF 头（不是 PDF）\n"
)
PASSED_REPLY = (
    "校验通过：/ws/build/out.pdf\n"
    "文件：/ws/build/out.pdf\n"
    "（按 libreoffice/export_render 的 2 条规则校验）\n"
    "  exists: 文件存在\n"
    "  pdf_magic: 7 字节头\n"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("_test_bibit_after_appcli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def after_appcli(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("BIBIT_INTENT_APPCLI_CHECK", raising=False)
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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.asyncio
async def test_failed_verification_attaches_a_correction(after_appcli, monkeypatch):
    requests: list[dict] = []
    server = _serve({"handled": True, "reply": FAILED_REPLY, "route": "rule"}, requests)
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        patch = await after_appcli.after_appcli({
            "tool_input": {
                "appcli": "libreoffice",
                "action": "export_render",
                "output": "build/out.pdf",
            },
            "state": {},
        })
    finally:
        server.shutdown()

    assert requests and requests[0]["path"] == "/v1/runs"
    body = requests[0]["body"]
    assert body["prompt"] == "校验产出物 libreoffice export_render build/out.pdf"
    assert body["session_id"] == "bibit-appcli-verify"
    assert body["stream"] is False
    # 低于 executor 的最小执行预算（40s）会被判 edge_deadline_exceeded
    assert body["timeout_ms"] >= 45_000

    section = patch["context_sections"][0]
    assert section.startswith("独立复核未通过：libreoffice/export_render")
    assert "不要自行重放渲染" in section
    assert "pdf_magic" in section
    assert patch["metadata"]["appcli_artifact"]["status"] == "fail"
    assert patch["state"]["appcli_artifact"]["status"] == "fail"
    assert patch["state"]["appcli_artifact"]["artifact"] == "build/out.pdf"


@pytest.mark.asyncio
async def test_passing_verification_records_state_without_context(after_appcli, monkeypatch):
    server = _serve({"handled": True, "reply": PASSED_REPLY}, [])
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        patch = await after_appcli.after_appcli({
            "tool_input": {
                "appcli": "libreoffice", "action": "export_render", "output": "build/out.pdf",
            },
            "state": {},
        })
    finally:
        server.shutdown()

    assert (patch.get("context_sections") or []) == []
    assert "metadata" not in patch
    assert patch["state"]["appcli_artifact"]["status"] == "pass"


@pytest.mark.asyncio
@pytest.mark.parametrize(("extra",), [
    ({"args": ["project=report.docx", "output=build/out.pdf"]},),
    ({"args": "project=report.docx output=build/out.pdf"},),
])
async def test_output_may_be_declared_in_args(after_appcli, monkeypatch, extra):
    requests: list[dict] = []
    server = _serve({"handled": True, "reply": PASSED_REPLY}, requests)
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        patch = await after_appcli.after_appcli({
            "tool_input": {
                "appcli": "libreoffice", "action": "export_render", **extra,
            },
            "state": {},
        })
    finally:
        server.shutdown()

    assert requests[0]["body"]["prompt"] == "校验产出物 libreoffice export_render build/out.pdf"
    assert patch["state"]["appcli_artifact"]["artifact"] == "build/out.pdf"


@pytest.mark.asyncio
async def test_missing_artifact_is_reported_as_an_unmet_deliverable(after_appcli, monkeypatch):
    reply = "未产出：/ws/build/out.pdf 不存在。该路径在工作区内是合法的，说明命令没有写出文件。"
    server = _serve({"handled": True, "reply": reply}, [])
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        patch = await after_appcli.after_appcli({
            "tool_input": {"appcli": "drawio", "action": "export_render", "output": "x.png"},
            "state": {},
        })
    finally:
        server.shutdown()

    assert patch["context_sections"][0].startswith("独立复核未通过：drawio/export_render")
    assert patch["state"]["appcli_artifact"]["status"] == "fail"


@pytest.mark.asyncio
async def test_non_appcli_tool_call_is_ignored(after_appcli, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("非 appcli 调用不该发起复核")

    monkeypatch.setattr(after_appcli, "_verify", fail)

    assert await after_appcli.after_appcli({
        "tool_input": {"path": "app.py", "content": "x"},
        "state": {},
    }) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool_input",), [
    ({"appcli": "libreoffice"},),
    ({"appcli": "libreoffice", "action": "export_render"},),
    ({"appcli": "", "action": "export_render", "output": "x.pdf"},),
    ({"appcli": "libreoffice", "action": "export_render", "output": "  "},),
])
async def test_incomplete_tool_input_is_ignored(after_appcli, monkeypatch, tool_input):
    def fail(*args, **kwargs):
        raise AssertionError("缺产物参数时不该发起复核")

    monkeypatch.setattr(after_appcli, "_verify", fail)

    assert await after_appcli.after_appcli({"tool_input": tool_input, "state": {}}) is None


@pytest.mark.asyncio
async def test_transport_failure_never_breaks_the_tool_call(after_appcli, monkeypatch):
    async def boom(appcli: str, action: str, artifact: str) -> str:
        raise OSError("connection refused")

    monkeypatch.setattr(after_appcli, "_verify", boom)

    patch = await after_appcli.after_appcli({
        "tool_input": {"appcli": "libreoffice", "action": "export_render", "output": "x.pdf"},
        "state": {},
    })

    assert (patch.get("context_sections") or []) == []
    assert "metadata" not in patch
    assert patch["state"]["appcli_artifact"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_unhandled_service_response_is_silent(after_appcli, monkeypatch):
    requests: list[dict] = []
    server = _serve({"handled": False, "reply": ""}, requests)
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    try:
        patch = await after_appcli.after_appcli({
            "tool_input": {"appcli": "libreoffice", "action": "export_render", "output": "x.pdf"},
            "state": {},
        })
    finally:
        server.shutdown()

    assert requests and (patch.get("context_sections") or []) == []
    assert patch["state"]["appcli_artifact"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_opt_out_environment_variable(after_appcli, monkeypatch):
    monkeypatch.setenv("BIBIT_INTENT_APPCLI_CHECK", "0")

    assert await after_appcli.after_appcli({
        "tool_input": {"appcli": "libreoffice", "action": "export_render", "output": "x.pdf"},
        "state": {},
    }) is None


@pytest.mark.asyncio
async def test_hook_runs_through_the_real_command_loader(tmp_path, monkeypatch):
    """真实 loader + 真实脚本 + 假 bibit 服务：验证 hook.json、子进程协议与 state 回写。"""
    from bianbu_agent.hooks.loader import DirectoryHookLoader
    from bianbu_agent.hooks.models import HookLoadContext
    from bianbu_agent.inventory.models import CapabilityOrigin
    from bianbu_agent.plugins.contributions import RunContext

    requests: list[dict] = []
    server = _serve({"handled": True, "reply": FAILED_REPLY}, requests)
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
    run = RunContext(
        "run", "session", "agent", "appcli_run",
        metadata={
            "tool": "appcli_run",
            "arguments": {
                "appcli": "libreoffice", "action": "export_render", "output": "build/out.pdf",
            },
        },
    )
    other = RunContext(
        "run", "session", "agent", "read_file",
        metadata={"tool": "read_file", "arguments": {"path": "build/out.pdf"}},
    )
    try:
        patch = await hook.handler(run)
    finally:
        server.shutdown()

    assert patch.context_sections[0].startswith("独立复核未通过：")
    assert await hook.handler(other) is None


def test_hook_is_declared_for_the_appcli_tool():
    hook = json.loads((HOOK_ROOT / "hook.json").read_text(encoding="utf-8"))
    groups = hook["hooks"]["PostToolUse"]

    assert {group["matcher"] for group in groups} == {"*appcli_run"}
    assert all(
        "after_appcli.py" in command["command"]
        for group in groups
        for command in group["hooks"]
    )
    from bianbu_agent.plugins.manifest import load_installed_plugin_manifest

    manifest = load_installed_plugin_manifest(PLUGIN_ROOT)
    assert "bibit-intent.after_appcli" in manifest.contributions.hooks
    assert "contributions" not in (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")


def test_mcp_manifests_put_the_bibit_venv_bin_on_path():
    """appcli 的入口命令装在 bibit-intent 的 venv 里，MCP 子进程必须能找到它。

    拆分后的三个 MCP server 都用同一个 venv 解释器，任何一个掉队都会让对应的
    Route 报「装了却说不在 PATH」。

    Runtime 的 stdio MCP 子进程只拿到 manifest 的 env（不继承 os.environ），而
    appcli_run 按注册表的裸命令名 Popen、前置检查用 shutil.which：PATH 里没有
    /opt/bibit-intent/.venv/bin 就会「装了却说不在 PATH」。
    """
    import json

    for name in ("bibit-readonly", "bibit-control", "bibit-admin"):
        manifest = json.loads(
            (PLUGIN_ROOT / "mcps" / name / "mcp.json").read_text(encoding="utf-8")
        )
        path = manifest["env"]["PATH"].split(":")

        assert path[0] == "/opt/bibit-intent/.venv/bin"
        assert "/usr/bin" in path
        assert manifest["command"] == "/opt/bibit-intent/.venv/bin/python"
