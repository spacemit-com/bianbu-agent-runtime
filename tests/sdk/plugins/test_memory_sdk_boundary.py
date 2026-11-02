"""Host delegates memory policy to the SDK and injects its bounded result."""
import importlib.util
from pathlib import Path
import sys
import types

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "resources/plugins/bianbu.memory/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location("memory_boundary_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_sdk(monkeypatch, bridge, calls):
    async def connect(**kwargs):
        calls.append(kwargs)
        return bridge
    memory = types.ModuleType("bianbu_agent_sdk.memory")
    memory.get_memory_bridge = connect
    monkeypatch.setitem(sys.modules, "bianbu_agent_sdk.memory", memory)


def run():
    return {"run_id":"r", "session_id":"s", "account_id":"trusted-account", "agent_id":"trusted-agent",
            "request":"hi", "response":"hello", "status":"completed", "metadata":{},
            "config":{"timeout_seconds":0.1, "socket_path":"/tmp/memory.sock", "recall_top_k":2}}


@pytest.mark.asyncio
async def test_before_run_forwards_identity_and_config_and_returns_sdk_patch(monkeypatch):
    calls = []
    expected = {"context_sections": ("SDK context",), "metadata": {"sdk_policy":True}}
    class Bridge:
        async def prepare_context(self, **kwargs):
            assert kwargs == {"query":"hi", "session_id":"s", "config":run()["config"]}
            return expected
        async def record_run_started(self, **kwargs):
            assert kwargs["session_id"] == "s"
            return True
    fake_sdk(monkeypatch, Bridge(), calls)
    assert await load("before_run").before_run(run()) == expected
    assert all(c == {"socket_path":"/tmp/memory.sock", "timeout":0.1,
                     "account_id":"trusted-account", "agent_name":"trusted-agent"} for c in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError])
async def test_unavailable_sdk_does_not_fail_chat(monkeypatch, failure):
    class Bridge:
        async def prepare_context(self, **kwargs):
            raise failure()
    fake_sdk(monkeypatch, Bridge(), [])
    assert await load("before_run").before_run(run()) is None


@pytest.mark.asyncio
async def test_after_run_only_forwards_lifecycle_metadata(monkeypatch):
    calls = []
    r = run()
    r["metadata"] = {"session_end":True, "context_compressions":[]}
    class Bridge:
        async def record_runtime_result(self, **kwargs):
            assert kwargs["metadata"] is r["metadata"]
            assert "force_summary" not in kwargs
            assert kwargs["user_text"] == "hi"
            return True
    fake_sdk(monkeypatch, Bridge(), calls)
    assert await load("after_run")._record_agent_memory(r) is True


@pytest.mark.asyncio
async def test_role_switch_prevents_all_sdk_calls(monkeypatch):
    calls = []
    fake_sdk(monkeypatch, object(), calls)
    r = run()
    r["metadata"] = {"hook_ids":[]}
    assert await load("before_run").before_run(r) is None
    assert await load("after_run").after_run(r) is None
    assert calls == []


def test_plugins_do_not_own_memory_policy():
    before = (SCRIPTS / "before_run.py").read_text(encoding="utf-8")
    after = (SCRIPTS / "after_run.py").read_text(encoding="utf-8")
    for policy in ("_is_greeting_only", "_safe_greeting_address", "_load_session_summary",
                   "_finalize_recent_session_summaries", "search_headers", "include_explicit"):
        assert policy not in before
    assert "summarize(" not in after and "force_summary" not in after
