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
    def __init__(self, *, required: bool = False, fail: bool = False,
                 registered: bool = False) -> None:
        self.required = required
        self.fail = fail
        self.registered = registered
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def _call(self, method: str, *args, **kwargs):
        self.calls.append((method, args))
        if self.fail:
            raise RuntimeError("securityd unavailable")
        if method == "get_agent_info":
            return {"agent_id": str(args[0]), "registered": self.registered}
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

    manifest = AgentManifest.from_path(Path(__file__).parents[2] / "resources/agents/com.bianbu.627d97ad908c/agent.yaml")

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
        mcps=("browser",), hooks=("audit",), plugins=("bianbu.security",),
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
async def test_agent_that_dropped_security_plugin_is_unregistered(tmp_path, monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITYD_HOME", "/tmp/securityd")
    client = _Client()
    runtime = _runtime(tmp_path, client)
    spec = AgentSpec(id="researcher", name="Researcher", tools=("search",))
    from bianbu_agent.security.capability_declarations import DeclarationBaseline

    baseline = DeclarationBaseline.in_data_dir(runtime.config.data_dir)
    baseline.record(spec.id, {})

    assert await runtime.register_logical_agent(spec) is None
    assert [method for method, _ in client.calls] == ["unregister_agent"]
    assert client.calls[0][1][0] == "researcher"
    assert baseline.declared_for(spec.id) is None


@pytest.mark.asyncio
async def test_agent_that_never_selected_security_is_not_unregistered(
    tmp_path, monkeypatch
):
    """A normal-mode Agent has no securityd row, so startup must not call it."""

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITYD_HOME", "/tmp/securityd")
    client = _Client()
    runtime = _runtime(tmp_path, client)
    spec = AgentSpec(id="researcher", name="Researcher", tools=("search",))

    assert await runtime.register_logical_agent(spec) is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_plain_worker_never_registers_another_agents_security_identity(
    tmp_path, monkeypatch
):
    """A host Worker must not depend on securityd for a sibling Expert."""

    monkeypatch.setenv("BIANBU_SECURITY_AGENT_ENABLED", "0")
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    client = _Client(required=True)
    runtime = _runtime(tmp_path, client, role="worker")
    sibling = AgentSpec(
        id="secured-sibling", name="Secured", plugins=("bianbu.security",)
    )

    assert await runtime.register_logical_agent(sibling) is None
    assert client.calls == []


@pytest.mark.asyncio
async def test_registration_replays_the_declaration_over_stale_rows(tmp_path, monkeypatch):
    """A policy an older release wrote must follow the declaration on upgrade."""
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITYD_HOME", "/tmp/securityd")

    class Existing(_Client):
        async def _call(self, method, *args, **kwargs):
            self.calls.append((method, args))
            if method == "get_capabilities":
                return [{"tool": "search", "policy": "ask"},
                        {"tool": "read_file", "policy": "ask"}]
            return {"status": "ok"}

    client = Existing()
    runtime = _runtime(tmp_path, client)
    spec = AgentSpec(
        id="researcher", name="Researcher", tools=("search", "write_file"),
        plugins=("bianbu.security",),
        permissions={"capabilities": [{"tool": "read_file", "policy": "allow"}]},
    )

    await runtime.register_logical_agent(spec)

    replayed = {row["tool"]: row["policy"] for row in client.calls[2][1][1]}
    assert replayed == {"search": "allow", "read_file": "allow", "write_file": "allow"}


@pytest.mark.asyncio
async def test_registration_preserves_an_operators_tightening(tmp_path, monkeypatch):
    """Once the console changes a row, later declarations leave it alone."""
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITYD_HOME", "/tmp/securityd")

    class Console(_Client):
        rows: list[dict] = []

        async def _call(self, method, *args, **kwargs):
            self.calls.append((method, args))
            if method == "get_capabilities":
                return [dict(row) for row in self.rows]
            if method == "register_capabilities":
                self.rows = [dict(row) for row in args[1]]
            return {"status": "ok"}

    client = Console()
    client.rows = [{"tool": "terminal", "policy": "allow"}]
    runtime = _runtime(tmp_path, client)
    spec = AgentSpec(
        id="a", name="A", tools=("terminal",), plugins=("bianbu.security",)
    )
    await runtime.register_logical_agent(spec)
    assert [method for method, _ in client.calls] == ["register_agent", "get_capabilities"]

    # The operator tightens the row in the security console.
    client.rows = [{"tool": "terminal", "policy": "deny"}]
    client.calls.clear()
    await runtime.register_logical_agent(spec)

    assert [method for method, _ in client.calls] == ["register_agent", "get_capabilities"]
    assert client.rows == [{"tool": "terminal", "policy": "deny"}]


@pytest.mark.asyncio
async def test_registration_is_a_noop_when_stored_rows_match_the_declaration(tmp_path, monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_SECURITYD_HOME", "/tmp/securityd")

    class Existing(_Client):
        async def _call(self, method, *args, **kwargs):
            self.calls.append((method, args))
            if method == "get_capabilities":
                return [{"tool": "search", "policy": "allow"}]
            return {"status": "ok"}

    client = Existing()
    runtime = _runtime(tmp_path, client)
    await runtime.register_logical_agent(AgentSpec(
        id="a", name="A", tools=("search",), plugins=("bianbu.security",)))

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
    spec = AgentSpec(id="a", name="A", plugins=("bianbu.security",))

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
    {'mode': 'safe', 'updated_at': '', 'updated_by': ''},
    {'mode': 'safe', 'updated_at': 'chosen', 'updated_by': 'admin'},
    {'mode': 'yolo', 'updated_at': 'chosen', 'updated_by': 'admin'},
    {'mode': 'safe'},
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
    spec = AgentSpec(
        id='default', name='Default', plugins=('bianbu.security',)
    )
    should_initialize = mode == {'mode': 'safe', 'updated_at': '', 'updated_by': ''}
    await runtime.register_logical_agent(spec)
    await runtime.register_logical_agent(spec)
    changes = [args for method, args in client.calls if method == 'set_agent_security_mode']
    assert changes == ([('default', 'yolo')] if should_initialize else [])
    assert any(method == 'get_agent_security_mode' for method, _ in client.calls)


@pytest.mark.asyncio
async def test_first_install_forces_packaged_security_agents_back_to_yolo(
    tmp_path, monkeypatch
):
    """The first install resets the shipped Agent, but not an operator's own."""

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setenv('BIANBU_SECURITYD_HOME', '/tmp/securityd')

    class ModeClient(_Client):
        async def _call(self, method, *args, **kwargs):
            self.calls.append((method, args))
            if method == 'get_agent_security_mode':
                return {
                    'agent_id': args[0], 'mode': 'safe',
                    'updated_at': 'chosen', 'updated_by': 'admin',
                }
            if method == 'set_agent_security_mode':
                return {
                    'agent_id': args[0], 'mode': args[1],
                    'updated_at': 'reset', 'updated_by': 'package-first-install',
                }
            return {'status': 'ok'}

    client = ModeClient()
    runtime = _runtime(tmp_path, client)
    monkeypatch.setattr(
        runtime, '_is_packaged_agent', lambda agent_id: agent_id == 'packaged',
    )
    specs = [
        AgentSpec(
            id='packaged', name='Packaged', plugins=('bianbu.security',)
        ),
        AgentSpec(id='user-made', name='User', plugins=('bianbu.security',)),
        AgentSpec(id='plain', name='Plain'),
    ]

    await runtime._reset_packaged_agent_security_modes(specs)

    assert [args for method, args in client.calls
            if method == 'set_agent_security_mode'] == [('packaged', 'yolo')]
    assert all(method != 'get_agent_security_mode' or args[0] == 'packaged'
               for method, args in client.calls)


def test_package_first_install_marker_is_consumed_once(tmp_path, monkeypatch):
    marker = tmp_path / 'package-first-install'
    marker.write_text('token-1\n', encoding='utf-8')
    monkeypatch.setenv('BIANBU_RUNTIME_FIRST_INSTALL_MARKER', str(marker))
    runtime = _runtime(tmp_path, _Client())

    assert runtime._package_first_install_token() == 'token-1'
    runtime._record_package_first_install('token-1')
    assert not marker.exists()
    assert runtime._package_first_install_state_path().read_text().strip() == 'token-1'

    marker.write_text('token-1\n', encoding='utf-8')
    assert runtime._package_first_install_token() is None


def test_packaged_agent_detection_uses_the_package_root_not_the_user_copy(
    tmp_path, monkeypatch
):
    system = tmp_path / 'system'
    packaged = system / 'agents' / 'com.bianbu.012345abcdef'
    packaged.mkdir(parents=True)
    (packaged / 'agent.yaml').write_text('schema_version: 2\n', encoding='utf-8')
    monkeypatch.setenv('BIANBU_SYSTEM_RESOURCE_ROOT', str(system))
    runtime = _runtime(tmp_path, _Client())

    # A user declaration with the same ID is effective for editing and Worker
    # launches, but the package still owns the shipped first-install default.
    user_copy = tmp_path / '.bianbu-agent' / 'agents' / packaged.name
    user_copy.mkdir(parents=True)
    (user_copy / 'agent.yaml').write_text('schema_version: 2\n', encoding='utf-8')

    assert runtime._is_packaged_agent(packaged.name) is True
    assert runtime._is_packaged_agent('com.bianbu.ffffffffffff') is False


def test_requested_execution_mode_follows_the_security_plugin_selection(tmp_path):
    runtime = _runtime(tmp_path, _Client())

    plain = AgentSpec(id="plain", name="Plain")
    secured = AgentSpec(
        id="secured", name="Secured", plugins=("bianbu.security",)
    )

    assert runtime._requested_execution_mode(plain) == "host"
    assert runtime._requested_execution_mode(secured) == "auto"


@pytest.mark.asyncio
async def test_prompt_replays_a_console_unregistered_declaration(tmp_path, monkeypatch):
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    client = _Client(required=True, registered=False)
    runtime = _runtime(tmp_path, client)
    spec = AgentSpec(
        id="bibit", name="Bibit", tools=("terminal",),
        plugins=("bianbu.security",),
    )
    await runtime._agent_store.create(spec)

    restored = await runtime.ensure_logical_agent_registered("bibit")

    assert restored == {"status": "ok"}
    methods = [method for method, _ in client.calls]
    assert methods[0] == "get_agent_info"
    assert "register_agent" in methods
    assert runtime.security_pending == {}


@pytest.mark.asyncio
async def test_prompt_probe_leaves_a_registered_agent_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    client = _Client(required=True, registered=True)
    runtime = _runtime(tmp_path, client)

    assert await runtime.ensure_logical_agent_registered("bibit") is None
    assert [method for method, _ in client.calls] == ["get_agent_info"]
