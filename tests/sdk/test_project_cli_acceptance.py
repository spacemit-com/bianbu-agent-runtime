from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
import zipfile

import httpx
import psutil
import yaml

from bianbu_agent.agents.projects import init_agent_project, check_agent_project, build_agent_project
from bianbu_agent.config import load_runtime_config
from bianbu_agent.server.development import (
    development_package_manager, start_development_runtime, development_status,
    uninstall_development_agent, tail_development_operational_logs,
)
from bianbu_agent.models import ProviderConfig, YAMLModelStore
from bianbu_agent.sessions.stores.json import JSONSessionStore
from bianbu_agent.sessions.log_store import SessionLogStore
from bianbu_agent.inventory import CapabilityCatalogBuilder, InventorySnapshot


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_hello_agent_sdk_workflow_completes_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """A packaged Tool must execute before the model can complete the real Run."""
    started_at = time.monotonic()
    repository = Path(__file__).parents[2]
    project = tmp_path / "hello-agent"
    artifact = tmp_path / "hello.agent"
    requests: list[dict] = []
    started: dict = {}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            results = [m for m in body["messages"] if m["role"] == "tool"]
            if results:
                content = ("HELLO_RT24_ACCEPTED" if results[-1]["content"] == "Hello, RT24!"
                           else "TOOL_RESULT_REJECTED")
                delta = {"role": "assistant", "content": content}
                finish = "stop"
            elif body.get("tools"):
                greeting_tools = [tool for tool in body["tools"]
                                  if tool["function"]["name"] == "com__bianbu__hello__greet"]
                assert len(greeting_tools) == 1, body["tools"]
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": "rt24-greet", "type": "function",
                    "function": {"name": greeting_tools[0]["function"]["name"],
                                 "arguments": '{"name":"RT24"}'},
                }]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "NO_GREETING_TOOL"}
                finish = "stop"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for value, reason in ((delta, None), ({}, finish)):
                chunk = {"id": "chatcmpl-rt24", "object": "chat.completion.chunk",
                         "created": 1, "model": "rt24",
                         "choices": [{"index": 0, "delta": value, "finish_reason": reason}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PYTHONPATH", str(repository))
    monkeypatch.setenv("BIANBU_SECURITY", "0")

    async def configure_provider():
        store = YAMLModelStore(project / ".bianbu-agent-data" / "models")
        await store.open()
        try:
            await store.create_provider(ProviderConfig(
                id="rt24", name="Local acceptance provider", protocol="openai-compatible",
                base_url=f"http://127.0.0.1:{provider.server_port}/v1", api_key="local-test-only",
            ))
        finally:
            await store.close()

    try:
        init_agent_project(project, agent_id="com.bianbu.hello", name="HelloAgent", port=_port())
        manifest = yaml.safe_load((project / "agent.yaml").read_text())
        manifest["agent"]["model"] = {"provider": "rt24", "name": "rt24"}
        (project / "agent.yaml").write_text(yaml.safe_dump(manifest))
        asyncio.run(configure_provider())
        assert check_agent_project(project).manifest.spec.id == "com.bianbu.hello"
        build_agent_project(project, output=artifact)
        with zipfile.ZipFile(artifact) as archive:
            assert "tools/greet/tool.py" in archive.namelist()
        package = development_package_manager(project, load_runtime_config(project / "runtime.yaml")).install(artifact)
        installed = {"id": package.id}
        assert installed["id"] == "com.bianbu.hello"
        # Even broken source must not replace or prevent running the installed archive.
        source = project / "tools" / "greet" / "tool.py"
        source.write_text("raise RuntimeError('SOURCE_ONLY must not load')\n")
        started = start_development_runtime(project, timeout=20)
        assert started["running"] is True
        with httpx.Client(base_url=started["url"], timeout=30) as client:
            def api(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                assert response.is_success, response.text
                return response.json()["data"]

            session = api("POST", "/api/v1/sessions", json={"agent_id": installed["id"]})
            session_id = session["id"]
            accepted = api("POST", f"/api/v1/sessions/{session_id}/prompt",
                           json={"message_id": "rt24-message", "content": "Greet RT24 using your tool."})
            run_id = accepted["run_id"]
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                events = api("GET", f"/api/v1/sessions/{session_id}/events")
                terminal = [event for event in events if event["run_id"] == run_id
                            and event["type"] in {"run/completed", "run/failed", "run/cancelled"}]
                if terminal:
                    break
                time.sleep(0.05)
            assert terminal and terminal[0]["data"]["status"] == "completed", events
            assert terminal[0]["data"]["content"] == "HELLO_RT24_ACCEPTED", events
            assert any(m["role"] == "tool" and m["content"] == "Hello, RT24!"
                       for request in requests for m in request["messages"])
            completion = [event for event in events if event["type"] == "tool/call.completed"]
            assert len(completion) == 1, events
            calls = [event for event in events if event["type"] == "tool/call.started"]
            assert len(calls) == 1
            assert calls[0]["data"]["arguments"] == {"name": "RT24"}
            assert calls[0]["data"]["call_id"] == completion[0]["data"]["call_id"]
            assert completion[0]["data"]["name"] == "com.bianbu.hello/greet"
            assert completion[0]["data"]["result"] == "Hello, RT24!"
        pid_path = project / ".bianbu-agent-data" / "runtime" / "bianbu-agent.pid"
        pid_record = json.loads(pid_path.read_text())
        runtime_process = psutil.Process(started["pid"])
        process_evidence = {
            "record": pid_record, "observed_create_time": runtime_process.create_time(),
            "state": runtime_process.status(),
        }
        status = development_status(project)
        assert status["running"] is True, process_evidence
        assert status["agent_id"] == installed["id"]
        correlated = tail_development_operational_logs(project, agent_id=installed["id"], session_id=session_id, run_id=run_id)
        records = [json.loads(line) for line in correlated.splitlines()]
        assert records
        assert all(record["agent_id"] == installed["id"] and record["session_id"] == session_id
                   and record["run_id"] == run_id for record in records)
        removed = uninstall_development_agent(installed["id"], project)
        assert removed == {"stopped": True, "deleted": True, "agent_id": installed["id"]}
        assert not (project / ".bianbu-agent-data" / "agents" / installed["id"]).exists()
        assert development_status(project)["running"] is False

        async def retained_session():
            store = JSONSessionStore(project / ".bianbu-agent-data" / "sessions")
            await store.open()
            try:
                saved = await store.get(session_id)
                assert saved is not None and saved.agent_id == installed["id"]
            finally:
                await store.close()
            logs = SessionLogStore(project / ".bianbu-agent-data" / "sessions")
            await logs.open()
            try:
                saved_run = await logs.get(run_id)
                assert saved_run is not None and saved_run.status == "completed"
                assert saved_run.result.content == "HELLO_RT24_ACCEPTED"
                assert saved_run.request["prompt"] == "Greet RT24 using your tool."
            finally:
                await logs.close()

        asyncio.run(retained_session())
        catalog = CapabilityCatalogBuilder(
            workspace_root=project / ".bianbu-agent-data" / "workspace",
        ).build(InventorySnapshot(revision=0))
        assert "com.bianbu.hello/greet" not in catalog.tools
        sessions_root = project / ".bianbu-agent-data" / "sessions"
        assert not (sessions_root / "checkpoints").exists()
        session_dirs = [path for path in sessions_root.iterdir() if path.is_dir()]
        assert session_dirs
        assert all(
            (path / "session.jsonl.zstd").is_file() for path in session_dirs
        )
        assert tail_development_operational_logs(project, run_id=run_id)
        assert time.monotonic() - started_at < 600
    finally:
        try:
            if (project / "runtime.yaml").exists():
                from bianbu_agent.server.process import stop_runtime
                stop_runtime(load_runtime_config(project / "runtime.yaml"))
            # The status command may remove a stale PID record. Still reap the
            # exact test Runtime we started, after checking its unique config.
            if started.get("pid"):
                try:
                    process = psutil.Process(started["pid"])
                    if str(project / "runtime.yaml") in process.cmdline():
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except psutil.TimeoutExpired:
                            process.kill()
                except psutil.NoSuchProcess:
                    pass
        finally:
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=3)
