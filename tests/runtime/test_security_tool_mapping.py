from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from bianbu_agent.plugins import RunContext
from tests.builtin_hook_support import adapt


PROJECT_ROOT = Path(__file__).parents[2]
PLUGIN_ROOT = PROJECT_ROOT / "resources/plugins/bianbu.security"


def _module(name: str):
    sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        for loaded in tuple(sys.modules):
            if loaded == "lib" or loaded.startswith("lib."):
                sys.modules.pop(loaded, None)
        return importlib.import_module(f"lib.{name}")
    finally:
        sys.path.remove(str(PLUGIN_ROOT))


def test_memory_directory_tools_use_local_tool_name() -> None:
    mapping = _module("mapping")

    for tool_name, expected in (
        ("bianbu.memory/memory", "memory"),
        ("bianbu.memory/memory_record", "memory_record"),
        ("bianbu.memory/memory_retrieve", "memory_retrieve"),
    ):
        assert mapping.resolve_capability(
            tool_name,
            {},
            tool_metadata={"origin": "plugin", "plugin_id": "bianbu.memory"},
        ) == (expected, "")


def test_real_mcp_tool_keeps_server_capability_boundary() -> None:
    mapping = _module("mapping")

    assert mapping.resolve_capability(
        "acme.fs/read_config",
        {},
        tool_metadata={"origin": "mcp", "mcp_id": "acme.fs"},
    ) == ("mcp.acme.fs", "read_config")

    # MCP server IDs may collide with ordinary Plugin IDs. Origin metadata is
    # authoritative, so the real MCP remains governed by its server boundary.
    assert mapping.resolve_capability(
        "bianbu.memory/memory_retrieve",
        {},
        tool_metadata={"origin": "mcp", "mcp_id": "bianbu.memory"},
    ) == ("mcp.bianbu.memory", "memory_retrieve")


def test_qualified_tool_without_origin_uses_fail_safe_mcp_fallback() -> None:
    mapping = _module("mapping")

    # Legacy callers may omit Tool metadata. Even if the local name collides
    # with an ordinary Plugin mapping, the qualified name must stay inside the
    # MCP server boundary until its origin is proven to be a directory Plugin.
    assert mapping.resolve_capability(
        "bianbu.memory/memory_retrieve", {},
    ) == ("mcp.bianbu.memory", "memory_retrieve")


def test_unknown_directory_plugin_is_not_treated_as_mcp() -> None:
    mapping = _module("mapping")

    assert mapping.resolve_capability(
        "vendor.notes/read_config",
        {},
        tool_metadata={"origin": "plugin", "plugin_id": "vendor.notes"},
    ) != ("mcp.vendor.notes", "read_config")


def test_directory_plugin_is_not_marked_as_remote_egress() -> None:
    transport = _module("mcp_transport")

    assert transport.egress_info(
        "bianbu.memory/memory_retrieve",
        {"origin": "plugin", "plugin_id": "bianbu.memory"},
    ) is None


@pytest.mark.asyncio
async def test_before_tool_hook_forwards_run_identity_to_securityd() -> None:
    """Execution-time checks must use the same run identity as HITL precheck."""

    hook_path = (
        PLUGIN_ROOT / "scripts/before_tool.py"
    )
    spec = importlib.util.spec_from_file_location(
        "bianbu_security_before_tool_test", hook_path
    )
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = hook
    spec.loader.exec_module(hook)

    calls: list[dict[str, str]] = []

    class Bridge:
        available = True

        @staticmethod
        def client():
            return object()

        @staticmethod
        def hook_point(name):
            return name

        async def invoke(self, _point, _agent, _payload, **context):
            calls.append(context)
            return SimpleNamespace(
                decision=SimpleNamespace(value="ALLOW"),
                metadata={},
                message="",
                modified_data=None,
            )

        @staticmethod
        def audit(*_args):
            pass

    bridge = Bridge()
    hook._bootstrap = lambda: (
        "unknown.tool",
        PermissionError,
        lambda: bridge,
        lambda value, **_kwargs: value,
        lambda *_args, **_kwargs: ("data.memory", ""),
        lambda: 1,
        lambda: "confirm",
        lambda *_args, **_kwargs: None,
    )
    contribution = adapt(hook.before_tool)

    await contribution.handler(
        RunContext(
            run_id="run-execution-42",
            session_id="session-7",
            agent_id="com.bianbu.agent",
            request="bianbu.memory/memory_retrieve",
            metadata={
                "tool": "bianbu.memory/memory_retrieve",
                "arguments": {"query": "preferences"},
            },
        )
    )

    assert calls == [{"session_id": "session-7", "run_id": "run-execution-42"}]


@pytest.mark.parametrize('agent_id', ['com.bianbu.d0d1fabac947', 'sdk-agent'])
def test_security_identity_uses_run_subject_without_policy_fallback(monkeypatch, agent_id):
    identity = _module('agent_id')
    monkeypatch.setenv('BIANBU_AGENT_ID', 'com.bianbu.runtime')
    def unavailable():
        raise AssertionError('Identity must not depend on registry enumeration')
    assert identity.resolve_agent_id(agent_id, client_provider=unavailable) == agent_id


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,force_block', [('yolo', False), ('normal', False), ('yolo', True)])
async def test_mcp_hook_checks_logical_expert_yolo_and_normal_block(monkeypatch, mode, force_block):
    identity = _module('agent_id')
    lib = sys.modules['lib']
    monkeypatch.setenv('BIANBU_AGENT_ID', 'com.bianbu.runtime')
    expert = 'com.bianbu.d0d1fabac947'
    calls = []
    class Client:
        def list_agents(self):
            return [{'agent_id': expert}, {'agent_id': 'com.bianbu.runtime'}]
        def get_agent_security_mode(self, aid):
            calls.append(('mode', aid))
            return {'mode': mode if aid == expert else 'normal'}
    class Bridge:
        available = True
        def client(self):
            return Client()
        def hook_point(self, name):
            return name
        async def invoke(self, point, aid, payload, **context):
            calls.append(('check', aid))
            assert payload['capability'] == 'mcp.mcp.filesystem'
            decision = 'ALLOW' if aid == expert and mode == 'yolo' and not force_block else 'BLOCK'
            return SimpleNamespace(decision=SimpleNamespace(value=decision), metadata={'reason': 'denied'}, message='denied', modified_data=None)
    monkeypatch.setattr(lib, 'get_bridge', lambda: Bridge())
    spec = importlib.util.spec_from_file_location('logical_mcp_hook', PLUGIN_ROOT / 'scripts/before_tool.py')
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    contribution = adapt(hook.before_tool)
    run = RunContext(run_id='run', session_id='session', agent_id=expert,
        request='mcp.filesystem/list_allowed_directories', metadata={
            'tool': 'mcp.filesystem/list_allowed_directories',
            'arguments': {'agent_id': 'com.bianbu.runtime'},
            'tool_metadata': {'origin': 'mcp', 'mcp_id': 'mcp.filesystem'},
        })
    if mode == 'yolo' and not force_block:
        result = await contribution.handler(run)
        assert result.metadata['securityd']['decision'] == 'ALLOW'
    else:
        with pytest.raises(lib.SecurityDenied) as error:
            await contribution.handler(run)
        assert error.value.should_terminate_run
    assert calls == [('mode', expert), ('check', expert)]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["handler_error", "handler_timeout"])
async def test_before_tool_hook_treats_handler_failure_block_as_hard_denial(reason) -> None:
    """R-1/R-2：干预点框架因 handler 异常/超时返回的 fail-closed BLOCK 必须硬拒绝，
    不能落入"securityd 不可达"的低危降级放行路径。"""

    hook_path = PLUGIN_ROOT / "scripts/before_tool.py"
    spec = importlib.util.spec_from_file_location(f"bianbu_security_before_tool_{reason}", hook_path)
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = hook
    spec.loader.exec_module(hook)

    degraded: list[tuple] = []

    class Denied(PermissionError):
        def __init__(self, **kwargs):
            super().__init__(kwargs.get("reason", "denied"))
            self.kwargs = kwargs

    class Bridge:
        available = True
        reason = ""

        @staticmethod
        def client():
            return object()

        @staticmethod
        def hook_point(name):
            return name

        async def invoke(self, _point, _agent, _payload, **context):
            return SimpleNamespace(
                decision=SimpleNamespace(value="BLOCK"),
                metadata={"reason": reason, "failed_handler": "dlp_pre_tool"},
                message=f"[bianbu-security] 安全 handler dlp_pre_tool {reason}",
                modified_data=None,
            )

        @staticmethod
        def degrade(*args):
            degraded.append(args)

        @staticmethod
        def audit(*_args):
            pass

    bridge = Bridge()
    hook._bootstrap = lambda: (
        "unknown.tool", Denied, lambda: bridge,
        lambda value, **_kwargs: value,
        lambda *_args, **_kwargs: ("file.read", "/etc/hostname"),
        lambda: 1, lambda: "ask", lambda *_args, **_kwargs: None,
    )
    import types
    fake_lib = types.ModuleType("lib")
    fake_lib.agent_security_mode_is_yolo = lambda *a, **k: False
    sys.modules["lib"] = fake_lib
    try:
        contribution = adapt(hook.before_tool)
        with pytest.raises(Denied) as caught:
            await contribution.handler(RunContext(
                run_id="run-1", session_id="s-1", agent_id="com.bianbu.agent",
                request="read_file",
                metadata={"tool": "read_file", "arguments": {"path": "/etc/hostname"}},
            ))
    finally:
        sys.modules.pop("lib", None)
    assert caught.value.kwargs["capability"] == "file.read"
    assert degraded == [], "handler 故障不得走不可达降级（低危会被放行）"
