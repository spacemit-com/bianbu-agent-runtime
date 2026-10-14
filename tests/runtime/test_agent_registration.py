from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bianbu_agent import (
    AgentManifest,
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    RuntimeConfig,
)
from bianbu_agent.agents.manager import AgentManager
from bianbu_agent.security.agent_registration import logical_agent_capabilities


class _Client:
    def __init__(self, *, required: bool = False, fail: bool = False) -> None:
        self.required = required
        self.fail = fail
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def _call(self, method: str, *args, **kwargs):
        self.calls.append((method, args))
        if self.fail:
            raise RuntimeError("securityd unavailable")
        if method == "get_capabilities":
            return []
        return {"status": "ok"}


def _runtime(
    tmp_path,
    client: _Client,
    *,
    role: str = "control",
) -> BianbuAgentRuntime:
    default = AgentSpec(id="default", name="Default")
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=object(),
        agent_id="default",
        agent_registry=InMemoryAgentRegistry((default,)),
        role=role,
    )
    runtime._security_runtime_client = client
    runtime.agents = AgentManager(
        store=runtime._agent_store,
        model_registry=runtime._models,
        generation_provider=lambda: SimpleNamespace(tools={}, skills={}, mcps={}, hooks={}),
    )
    return runtime


@pytest.mark.asyncio
async def test_worker_does_not_own_logical_agent_security_registration(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITY_CLIENT_DIR", "/usr/lib/bianbu-securityd/client")
    monkeypatch.setenv("BIANBU_WORKER_MANAGED", "1")
    client = _Client(required=True)
    runtime = _runtime(tmp_path, client, role="worker")

    assert await runtime.register_logical_agent(AgentSpec(id="default", name="Default")) is None
    assert runtime.security_registration_required is False
    assert client.calls == []


@pytest.mark.asyncio
async def test_production_agent_uses_direct_registration_with_runtime_capabilities(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITY_CLIENT_DIR", "/usr/lib/bianbu-securityd/client")
    client = _Client(required=True)
    runtime = _runtime(tmp_path, client, role="control")

    manifest = AgentManifest.from_path(Path(__file__).parents[2] / "resources/default-profile/starter-agent.yaml")

    await runtime.register_logical_agent(manifest.spec)

    # Registration only *adds* missing tool rows; the tool policies, DLP rows
    # and execution boundary belong to the security console.
    methods = [method for method, _ in client.calls]
    assert methods == ["register_agent", "get_capabilities", "register_capabilities"]
    # securityd records the framework at registration so the console can list
    # exactly the tools this Agent's framework provides.
    assert client.calls[0][1][1]["framework"] == "runtime"
    rows = client.calls[2][1][1]
    tools = {row["tool"] for row in rows}
    assert {"terminal", "write_file", "process", "kanban_create"} <= tools
    assert {row["framework"] for row in rows} == {"runtime"}


@pytest.mark.asyncio
async def test_register_logical_agent_syncs_declared_and_selected_capabilities(tmp_path, monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITYD_HOME", "/tmp/securityd")
    client = _Client()
    runtime = _runtime(tmp_path, client)
    spec = AgentSpec(
        id="researcher", name="Researcher", tools=("search",), skills=("review",),
        mcps=("browser",), hooks=("audit",),
        permissions={"capabilities": [{"tool": "read_file", "policy": "allow"}]},
    )

    await runtime.register_logical_agent(spec)

    assert [method for method, _ in client.calls] == [
        "register_agent", "get_capabilities", "register_capabilities"]
    synced = client.calls[2][1][1]
    # The declared policy wins; selected tools and MCP servers default to allow.
    assert {item["tool"]: item["policy"] for item in synced} == {
        "read_file": "allow", "search": "allow", "mcp.browser": "allow"}
    assert runtime.security_pending == {}


@pytest.mark.asyncio
async def test_registration_never_overwrites_an_operators_tightening(tmp_path, monkeypatch):
    """已存在的工具策略由安全控制台持有，注册只补缺失项。"""
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITYD_HOME", "/tmp/securityd")

    class Existing(_Client):
        async def _call(self, method, *args, **kwargs):
            self.calls.append((method, args))
            if method == "get_capabilities":
                return [{"tool": "search", "policy": "deny"},
                        {"tool": "read_file", "policy": "ask"}]
            return {"status": "ok"}

    client = Existing()
    runtime = _runtime(tmp_path, client)
    spec = AgentSpec(
        id="researcher", name="Researcher", tools=("search", "write_file"),
        permissions={"capabilities": [{"tool": "read_file", "policy": "allow"}]},
    )

    await runtime.register_logical_agent(spec)

    added = client.calls[2][1][1]
    assert {item["tool"] for item in added} == {"write_file"}
    assert not any(method == "sync_agent_capabilities" for method, _ in client.calls)


@pytest.mark.asyncio
async def test_registration_is_a_noop_when_every_tool_is_known(tmp_path, monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITYD_HOME", "/tmp/securityd")

    class Existing(_Client):
        async def _call(self, method, *args, **kwargs):
            self.calls.append((method, args))
            if method == "get_capabilities":
                return [{"tool": "search", "policy": "deny"}]
            return {"status": "ok"}

    client = Existing()
    runtime = _runtime(tmp_path, client)
    await runtime.register_logical_agent(AgentSpec(id="a", name="A", tools=("search",)))

    assert [method for method, _ in client.calls] == ["register_agent", "get_capabilities"]


def test_capability_translation_deduplicates_by_tool():
    spec = AgentSpec(
        id="a", name="A", tools=("search",),
        permissions={"capabilities": [{"tool": "search", "policy": "deny"}]},
    )
    rows = logical_agent_capabilities(spec)
    assert rows == [{"tool": "search", "policy": "deny", "framework": "runtime"}]


def test_capability_translation_follows_the_agent_framework():
    runtime_rows = logical_agent_capabilities(
        AgentSpec(id="com.bianbu.agent", name="A", tools=("read_file",)))
    hermes_rows = logical_agent_capabilities(
        AgentSpec(id="com.hermes.agent", name="H", tools=("read_file",)))

    # Same tool name, different framework: the console must show the list that
    # belongs to the Agent it is describing.
    assert runtime_rows == [{"tool": "read_file", "policy": "allow", "framework": "runtime"}]
    assert hermes_rows == [{"tool": "read_file", "policy": "allow", "framework": "hermes"}]


@pytest.mark.asyncio
async def test_optional_registration_failure_is_pending_and_required_is_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITYD_HOME", "/tmp/securityd")
    spec = AgentSpec(id="a", name="A")

    optional = _runtime(tmp_path / "optional", _Client(required=False, fail=True))
    with pytest.raises(RuntimeError):
        await optional.register_logical_agent(spec)
    optional._security_pending[spec.id] = "securityd unavailable"
    assert optional.security_pending["a"] == "securityd unavailable"

    required = _runtime(tmp_path / "required", _Client(required=True, fail=True))
    assert required.security_registration_required is True
    with pytest.raises(RuntimeError):
        await required.register_logical_agent(spec)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', [
    {'mode': 'normal', 'updated_at': '', 'updated_by': ''},
    {'mode': 'normal', 'updated_at': 'chosen', 'updated_by': 'admin'},
    {'mode': 'yolo', 'updated_at': 'chosen', 'updated_by': 'admin'},
    {'mode': 'normal'},
])
async def test_builtin_bibit_defaults_yolo_only_before_any_explicit_mode_choice(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr('bianbu_agent.provisioning.system_starter_id', lambda _: 'default')
    class ModeClient(_Client):
        async def _call(self, method, *args, **kwargs):
            self.calls.append((method, args))
            if method == 'get_agent_security_mode':
                return dict(mode)
            if method == 'set_agent_security_mode':
                mode.update(mode='yolo', updated_at='initialized', updated_by='runtime')
                return dict(mode)
            return {'status': 'ok'}
    client = ModeClient()
    runtime = _runtime(tmp_path, client)
    spec = AgentSpec(id='default', name='Default')
    should_initialize = mode == {'mode': 'normal', 'updated_at': '', 'updated_by': ''}
    await runtime.register_logical_agent(spec)
    await runtime.register_logical_agent(spec)
    changes = [args for method, args in client.calls if method == 'set_agent_security_mode']
    assert changes == ([('default', 'yolo')] if should_initialize else [])
    assert any(method == 'get_agent_security_mode' for method, _ in client.calls)
