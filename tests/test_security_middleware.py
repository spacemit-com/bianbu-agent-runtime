from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from bianbu_agent.security import security_middleware as middleware


def _tool_request(
    *,
    session_id: str = "session-1",
    run_id: str = "run-1",
    tool_name: str = "terminal",
    args: dict | None = None,
    tool_metadata: dict | None = None,
):
    control = SimpleNamespace(session_id=session_id, request_id=run_id)
    runtime = SimpleNamespace(context=SimpleNamespace(control=control))
    return SimpleNamespace(
        tool_call={
            "name": tool_name,
            "args": args if args is not None else {"command": "echo hello"},
        },
        runtime=runtime,
        tool_metadata=tool_metadata or {},
    )


def test_run_hooks_forwards_the_prechecked_exclusion() -> None:
    """Execution must skip the Hook chain the HITL prompt already ran."""

    from bianbu_agent.engines.langgraph.plugin_tools import _run_hooks

    calls = []

    class _Runner:
        async def run_hooks(self, point, context, *, hook_ids=None, exclude=()):
            calls.append((point, hook_ids, exclude))
            return SimpleNamespace()

    import asyncio

    asyncio.run(
        _run_hooks(
            _Runner(), "pre_tool_call", "context", ("hook",),
            exclude=("hook",),
        )
    )
    assert calls == [("pre_tool_call", ("hook",), ("hook",))]


def test_hitl_precheck_uses_the_security_hook_chain(monkeypatch) -> None:
    """The Hook chain is the only judge: HITL must not call CheckPermission."""

    import asyncio

    from langchain_core.messages import AIMessage

    from bianbu_agent.security import security_runtime

    runtime_calls = []

    class _Runner:
        async def run_hooks(self, point, context, *, hook_ids=None, exclude=()):
            runtime_calls.append(
                (point, hook_ids, dict(context.metadata))
            )
            return SimpleNamespace(
                metadata={
                    "bianbu.security": {
                        "securityd": {
                            "decision": "CONFIRM",
                            "requires_approval": True,
                        }
                    }
                },
                tool_arguments=None,
            )

    monkeypatch.setattr(
        security_runtime, "security_client_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HITL must not judge the call a second time")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        middleware, "_runtime_context", lambda _request: ("session-1", "run-1")
    )
    hitl = middleware.build_security_hitl_middleware(
        {"run_shell": {"allowed_decisions": ["approve", "reject"]}},
        tool_metadata={"run_shell": {}},
        hook_runner=_Runner(),
        hook_ids=("bianbu.security/before_tool",),
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_shell",
                        "args": {"command": "id"},
                        "id": "call-1",
                    }
                ],
            )
        ]
    }

    async def _run():
        return await hitl._precheck(state, SimpleNamespace())

    decisions, effective = asyncio.run(_run())
    assert decisions == {"call-1": middleware.APPROVAL_ASK}
    assert effective == {}
    assert runtime_calls and runtime_calls[0][0] == "before_tool"
    assert runtime_calls[0][1] == ("bianbu.security/before_tool",)
    assert runtime_calls[0][2]["tool"] == "run_shell"
    assert runtime_calls[0][2]["arguments"] == {"command": "id"}


def test_plugin_path_uses_managed_worker_data_root(tmp_path, monkeypatch):
    """A bwrap Worker must find hooks through BIANBU_RUNTIME_DATA_DIR."""

    plugin_home = tmp_path / "plugins" / "bianbu.security"
    (plugin_home / "lib").mkdir(parents=True)
    monkeypatch.delenv("BIANBU_SECURITY_PLUGIN_HOME", raising=False)
    monkeypatch.delenv("BIANBU_AGENT_DATA_DIR", raising=False)
    monkeypatch.setenv("BIANBU_RUNTIME_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path.parent)

    assert middleware._plugin_lib_path() == str(plugin_home)


def test_plugin_path_falls_back_to_the_packaged_security_plugin(tmp_path, monkeypatch):
    """An expert without the plugin still gets the global security mapping."""

    system_root = tmp_path / "share"
    plugin_home = system_root / "plugins" / "bianbu.security"
    (plugin_home / "lib").mkdir(parents=True)
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.delenv("BIANBU_SECURITY_PLUGIN_HOME", raising=False)
    monkeypatch.delenv("BIANBU_AGENT_DATA_DIR", raising=False)
    monkeypatch.setenv("BIANBU_RUNTIME_DATA_DIR", str(data_root))
    monkeypatch.setenv("BIANBU_SYSTEM_RESOURCE_ROOT", str(system_root))
    monkeypatch.chdir(tmp_path)

    assert middleware._plugin_lib_path() == str(plugin_home)


def test_securityd_confirm_precheck_cannot_evaluate_policy(monkeypatch):
    """Missing plugin state is UNKNOWN, so the static contract decides."""

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setattr(middleware, "_plugin_lib_path", lambda: None)

    # Not a silent allow and not a blanket prompt: the caller has to fall back
    # to the tool's own requires_approval / destructive contract.
    assert middleware.securityd_approval_decision(_tool_request()) == (
        middleware.APPROVAL_UNKNOWN
    )
    assert middleware.securityd_requires_confirm(_tool_request()) is False


def test_security_disabled_does_not_ask_securityd(monkeypatch):
    """BIANBU_SECURITY=0 leaves static tool contracts in charge."""

    from bianbu_agent.security import security_runtime

    monkeypatch.setenv("BIANBU_SECURITY", "0")

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("securityd must not be consulted")

    monkeypatch.setattr(
        security_runtime, "security_client_call", must_not_run, raising=False
    )

    assert middleware.securityd_approval_decision(_tool_request()) == (
        middleware.APPROVAL_UNKNOWN
    )


def test_securityd_confirm_precheck_without_context_still_blocks_deny(monkeypatch):
    """Missing LangGraph context must not turn an explicit DENY into approval."""
    plugin_home = Path(__file__).parents[1] / "resources" / "plugins" / "bianbu.security"
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(middleware, "_plugin_lib_path", lambda: str(plugin_home))
    fake_client = SimpleNamespace(
        check_permission=lambda agent, capability: 2,
    )
    monkeypatch.setitem(sys.modules, "bianbu_security_client", fake_client)
    request = _tool_request()
    request.runtime = SimpleNamespace(config={"configurable": {}})
    with __import__("pytest").raises(PermissionError, match="bianbu-security"):
        middleware.securityd_requires_confirm(request)


def test_securityd_confirm_precheck_does_not_reuse_stale_decision(
    monkeypatch,
):
    """WebUI policy changes must affect the next Runtime tool call immediately."""

    plugin_home = (
        __import__("pathlib").Path(__file__).parents[1]
        / "resources"
        / "plugins"
        / "bianbu.security"
    )
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    monkeypatch.delenv("BIANBU_WORKER_AGENT_ID", raising=False)
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setenv("BIANBU_SECURITY_PLUGIN_HOME", str(plugin_home))
    monkeypatch.setattr(middleware, "_plugin_lib_path", lambda: str(plugin_home))

    decisions = iter((0, 1))
    contexts = []
    fake_client = SimpleNamespace(
        check_permission_v2=lambda agent, capability, scope="", context=None: (
            contexts.append((agent, capability, scope, context))
            or next(decisions)
        ),
    )
    monkeypatch.setitem(sys.modules, "bianbu_security_client", fake_client)

    assert middleware.securityd_requires_confirm(_tool_request()) is False
    assert middleware.securityd_requires_confirm(_tool_request(run_id="run-2")) is True
    assert contexts == [
        (
            "com.bianbu.agent",
            "terminal",
            "",
            {"session_id": "session-1", "run_id": "run-1"},
        ),
        (
            "com.bianbu.agent",
            "terminal",
            "",
            {"session_id": "session-1", "run_id": "run-2"},
        ),
    ]


def test_yolo_allow_decision_skips_runtime_hitl(monkeypatch):
    """securityd YOLO returns ALLOW, so Runtime must not create an approval."""

    plugin_home = (
        Path(__file__).parents[1]
        / "resources"
        / "plugins"
        / "bianbu.security"
    )
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(middleware, "_plugin_lib_path", lambda: str(plugin_home))
    calls = []
    fake_client = SimpleNamespace(
        get_agent_security_mode=lambda agent: (
            calls.append(("mode", agent)) or {"agent_id": agent, "mode": "yolo"}
        ),
        check_permission_v2=lambda agent, capability, scope="", context=None: (
            calls.append((agent, capability, scope, context)) or 0
        ),
    )
    monkeypatch.setitem(sys.modules, "bianbu_security_client", fake_client)

    assert middleware.securityd_requires_confirm(_tool_request()) is False
    assert calls == [("mode", "com.bianbu.agent")]


def test_yolo_skips_hitl_for_unmapped_skill_view(monkeypatch):
    """YOLO must also bypass the conservative unmapped-tool confirmation."""
    plugin_home = Path(__file__).parents[1] / "resources" / "plugins" / "bianbu.security"
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(middleware, "_plugin_lib_path", lambda: str(plugin_home))
    fake_client = SimpleNamespace(
        get_agent_security_mode=lambda agent: {"agent_id": agent, "mode": "yolo"},
    )
    monkeypatch.setitem(sys.modules, "bianbu_security_client", fake_client)
    request = _tool_request(tool_name="skill_view", args={"name": "vibe-coder"})

    assert middleware.securityd_requires_confirm(request) is False


def test_securityd_deny_precheck_raises_before_tool_execution(monkeypatch):
    """An explicit securityd DENY must not be treated as no approval needed."""

    plugin_home = (
        __import__("pathlib").Path(__file__).parents[1]
        / "resources"
        / "plugins"
        / "bianbu.security"
    )
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    monkeypatch.delenv("BIANBU_WORKER_AGENT_ID", raising=False)
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setenv("BIANBU_SECURITY_PLUGIN_HOME", str(plugin_home))
    monkeypatch.setattr(middleware, "_plugin_lib_path", lambda: str(plugin_home))
    fake_client = SimpleNamespace(
        check_permission_v2=lambda agent, capability, scope="", context=None: 2,
    )
    monkeypatch.setitem(sys.modules, "bianbu_security_client", fake_client)

    with __import__("pytest").raises(PermissionError, match="bianbu-security"):
        middleware.securityd_requires_confirm(_tool_request())


def test_runtime_context_prefers_runnable_config_over_stale_control() -> None:
    """HITL ToolRuntime carries the current run identity in config.

    A persisted/resumed graph may retain another control object in runtime
    context. Session trust must follow the same configurable identity used by
    Tool hooks, otherwise one approval is immediately followed by another.
    """

    request = _tool_request(session_id="stale-session", run_id="stale-run")
    request.runtime.config = {
        "configurable": {
            "session_id": "current-session",
            "run_id": "current-run",
        }
    }

    assert middleware._runtime_context(request) == (
        "current-session",
        "current-run",
    )


def test_directory_plugin_tool_is_not_misclassified_as_mcp(monkeypatch):
    """Qualified ordinary Plugin Tools must use their local capability."""

    plugin_home = (
        Path(__file__).parents[1]
        / "resources"
        / "plugins"
        / "bianbu.security"
    )
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    monkeypatch.delenv("BIANBU_WORKER_AGENT_ID", raising=False)
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(middleware, "_plugin_lib_path", lambda: str(plugin_home))
    calls = []
    fake_client = SimpleNamespace(
        check_permission_v2=lambda agent, capability, scope="", context=None: (
            calls.append((agent, capability, scope, context)) or 0
        ),
    )
    monkeypatch.setitem(sys.modules, "bianbu_security_client", fake_client)

    request = _tool_request(
        tool_name="bianbu.memory/memory_retrieve",
        args={"query": "preferences"},
        tool_metadata={"origin": "plugin", "plugin_id": "bianbu.memory"},
    )

    assert middleware.securityd_requires_confirm(request) is False
    assert calls == [
        (
            "com.bianbu.agent",
            "memory_retrieve",
            "",
            {"session_id": "session-1", "run_id": "run-1"},
        )
    ]


def test_dynamic_hitl_passes_tool_origin_to_security_precheck(monkeypatch):
    """The after-model precheck must receive the same Tool origin metadata."""

    seen = []
    monkeypatch.setattr(
        middleware,
        "securityd_approval_decision",
        lambda request: (
            seen.append(dict(request.tool_metadata))
            or middleware.APPROVAL_ALLOW
        ),
    )
    hitl = middleware.build_security_hitl_middleware(
        {"encoded-memory": {"allowed_decisions": ["approve", "reject"]}},
        tool_metadata={
            "encoded-memory": {
                "origin": "plugin",
                "plugin_id": "bianbu.memory",
            }
        },
    )

    assert hitl._needs_interrupt(
        {"name": "encoded-memory", "args": {}, "id": "call-1"}, None
    ) is False
    assert seen == [{"origin": "plugin", "plugin_id": "bianbu.memory"}]


def test_dynamic_hitl_asks_for_a_memory_remove_and_skips_a_memory_add(monkeypatch):
    """记忆工具的破坏性只在参数里，注解表达不了，所以必须走谓词。"""

    from bianbu_agent.engines.langgraph.engine import (
        _memory_tool_call_requires_approval,
    )

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(
        middleware, "_plugin_lib_path", lambda: None
    )
    monkeypatch.setattr(
        middleware, "securityd_requires_confirm", lambda _request: False
    )
    hitl = middleware.build_security_hitl_middleware(
        {"encoded-memory": {"allowed_decisions": ["approve", "reject"]}},
        memory_names={"encoded-memory"},
        memory_when=_memory_tool_call_requires_approval,
    )

    assert hitl._needs_interrupt(
        {
            "name": "encoded-memory",
            "args": {"target": "memory", "action": "remove", "memory_id": "m-1"},
            "id": "call-remove",
        },
        None,
    ) is True
    assert hitl._needs_interrupt(
        {
            "name": "encoded-memory",
            "args": {"target": "memory", "action": "add", "content": "tea"},
            "id": "call-add",
        },
        None,
    ) is False


def test_dynamic_hitl_deny_precheck_overrides_static_approval(monkeypatch):
    """A statically protected tool must not show approval when securityd denies it."""

    def deny(_request):
        raise PermissionError("权限被拒绝")

    monkeypatch.setattr(middleware, "securityd_approval_decision", deny)
    hitl = middleware.build_security_hitl_middleware(
        {"terminal": {"allowed_decisions": ["approve", "reject"]}},
        protected_names={"terminal"},
    )

    with __import__("pytest").raises(PermissionError, match="权限被拒绝"):
        hitl._needs_interrupt({"name": "terminal", "args": {}, "id": "call-1"}, None)


def test_dynamic_hitl_yolo_skips_static_protected_and_terminal_rules(monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(
        middleware, "securityd_requires_confirm",
        lambda _request: (_ for _ in ()).throw(AssertionError("must short-circuit")),
    )
    fake_client = SimpleNamespace(
        get_agent_security_mode=lambda agent: {"agent_id": agent, "mode": "yolo"},
    )
    monkeypatch.setitem(sys.modules, "bianbu_security_client", fake_client)
    hitl = middleware.build_security_hitl_middleware(
        {"terminal": {"allowed_decisions": ["approve", "reject"]}},
        protected_names={"terminal"},
        terminal_names={"terminal"},
        terminal_when=lambda _request: True,
    )
    assert hitl._needs_interrupt({"name": "terminal", "args": {}, "id": "call-1"}, None) is False


def test_dynamic_hitl_allowed_tool_skips_static_protected_rule(monkeypatch):
    """An allow policy must win over requires_approval, not be re-prompted."""

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(
        middleware, "securityd_approval_decision",
        lambda _request: middleware.APPROVAL_ALLOW,
    )
    hitl = middleware.build_security_hitl_middleware(
        {"appcli_run": {"allowed_decisions": ["approve", "reject"]}},
        protected_names={"appcli_run"},
    )

    assert hitl._needs_interrupt(
        {"name": "appcli_run", "args": {}, "id": "call-1"}, None
    ) is False


def test_dynamic_hitl_ask_still_prompts_for_a_static_tool(monkeypatch):
    """ASK from securityd keeps the approval prompt even without a static flag."""

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(
        middleware, "securityd_approval_decision",
        lambda _request: middleware.APPROVAL_ASK,
    )
    hitl = middleware.build_security_hitl_middleware(
        {"terminal": {"allowed_decisions": ["approve", "reject"]}},
    )

    assert hitl._needs_interrupt(
        {"name": "terminal", "args": {"command": "echo hi"}, "id": "call-1"}, None
    ) is True


def test_dynamic_hitl_unknown_decision_falls_back_to_static_contract(monkeypatch):
    """When policy is unavailable, requires_approval and rm predicates still ask."""

    from bianbu_agent.engines.langgraph.engine import (
        _terminal_tool_call_requires_approval,
    )

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(
        middleware, "securityd_approval_decision",
        lambda _request: middleware.APPROVAL_UNKNOWN,
    )
    hitl = middleware.build_security_hitl_middleware(
        {
            "appcli_run": {"allowed_decisions": ["approve", "reject"]},
            "terminal": {"allowed_decisions": ["approve", "reject"]},
        },
        protected_names={"appcli_run"},
        terminal_names={"terminal"},
        terminal_when=_terminal_tool_call_requires_approval,
    )

    assert hitl._needs_interrupt(
        {"name": "appcli_run", "args": {}, "id": "call-1"}, None
    ) is True
    assert hitl._needs_interrupt(
        {"name": "terminal", "args": {"command": "rm -rf /tmp/x"}, "id": "call-2"},
        None,
    ) is True
    assert hitl._needs_interrupt(
        {"name": "terminal", "args": {"command": "echo hi"}, "id": "call-3"}, None
    ) is False


def test_securityd_record_approval_writes_audit_without_session_trust(monkeypatch):
    plugin_home = (
        __import__("pathlib").Path(__file__).parents[1]
        / "resources"
        / "plugins"
        / "bianbu.security"
    )
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    monkeypatch.delenv("BIANBU_WORKER_AGENT_ID", raising=False)
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setenv("BIANBU_SECURITY_PLUGIN_HOME", str(plugin_home))
    monkeypatch.setattr(middleware, "_plugin_lib_path", lambda: str(plugin_home))
    calls = []
    trusts = []

    def audit_log(agent, op_type, target, result):
        calls.append((agent, op_type, target, result))
        return 42

    def grant_session_trust_v2(*args, **kwargs):
        trusts.append((args, kwargs))
        return True

    fake_client = SimpleNamespace(
        audit_log=audit_log,
        grant_session_trust_v2=grant_session_trust_v2,
    )
    monkeypatch.setitem(sys.modules, "bianbu_security_client", fake_client)

    assert middleware.securityd_record_approval(
        tool_name="terminal",
        args={"command": "echo hello"},
        session_id="session-1",
        run_id="run-1",
    ) is True
    assert calls == [
        ("com.bianbu.agent", "ask", "terminal:", "approved=true")
    ]
    assert trusts == []


def test_securityd_record_approval_uses_directory_plugin_origin(monkeypatch):
    plugin_home = (
        Path(__file__).parents[1]
        / "resources"
        / "plugins"
        / "bianbu.security"
    )
    monkeypatch.delenv("COM_BIANBU_AGENT_ID", raising=False)
    monkeypatch.delenv("BIANBU_WORKER_AGENT_ID", raising=False)
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(middleware, "_plugin_lib_path", lambda: str(plugin_home))
    calls = []

    def audit_log(agent, op_type, target, result):
        calls.append((agent, op_type, target, result))
        return 7

    fake_client = SimpleNamespace(audit_log=audit_log)
    monkeypatch.setitem(sys.modules, "bianbu_security_client", fake_client)

    assert middleware.securityd_record_approval(
        tool_name="bianbu.memory/memory_retrieve",
        args={"query": "preferences"},
        session_id="session-1",
        run_id="run-1",
        tool_metadata={"origin": "plugin", "plugin_id": "bianbu.memory"},
    ) is True
    assert calls == [
        ("com.bianbu.agent", "ask", "memory_retrieve:", "approved=true")
    ]
