from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
import threading
from typing import Any

import pytest
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from bianbu_agent import (
    AgentNotFoundError,
    AgentModel,
    AgentRuntime,
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    OpenAICompatibleModel,
    RunControl,
    RunEvent,
    RuntimeConfig,
)
from bianbu_agent.context import CompressionSettingsProvider
from tests.sdk._approval import stream_answering_approvals
from bianbu_agent.capture.llm import ProviderCaptureStore
from bianbu_agent.engines.langgraph import (
    LangGraphEngine,
    build_langgraph_agent,
)
from bianbu_agent.sessions.events import SessionEvent
from bianbu_agent.engines.langgraph.engine import (
    _memory_tool_call_requires_approval,
    _terminal_command_requires_approval,
    _terminal_tool_call_requires_approval,
)
from bianbu_agent.security import security_middleware


class RecordingEngine:
    def __init__(self, label: str) -> None:
        self.label = label
        self.prompts: list[str | None] = []
        self.exchanges: list[tuple[str, str, str, str]] = []

    async def stream(self, prompt: str | None, **_kwargs: Any):
        self.prompts.append(prompt)
        yield RunEvent(kind="token", payload={"text": self.label})

    async def has_pending_approval(self, thread_id: str) -> bool:
        return thread_id == f"approval:{self.label}"

    async def seed_thread(self, thread_id: str, messages: list[object]) -> None:
        del thread_id, messages

    async def record_direct_reply(
        self,
        thread_id: str,
        request_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        self.exchanges.append(
            (thread_id, request_id, user_content, assistant_content)
        )

    async def has_thread_input(self, thread_id: str, request_id: str) -> bool:
        return (thread_id, request_id) == (self.label, "input")

    async def recover_completed_events(
        self,
        thread_id: str,
        request_id: str,
    ) -> list[RunEvent] | None:
        if (thread_id, request_id) != (self.label, "complete"):
            return None
        return [RunEvent(kind="token", payload={"text": self.label})]


class CountingFakeMessagesListChatModel(FakeMessagesListChatModel):
    model_calls: int = 0

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def _generate(self, messages, *args, **kwargs):
        self.model_calls += 1
        return super()._generate(messages, *args, **kwargs)


class RejectsDanglingToolCallsModel(CountingFakeMessagesListChatModel):
    def _generate(self, messages, *args, **kwargs):
        for index, message in enumerate(messages):
            if not isinstance(message, AIMessage) or not message.tool_calls:
                continue
            expected = {str(call["id"]) for call in message.tool_calls}
            actual: set[str] = set()
            for candidate in messages[index + 1 :]:
                if not isinstance(candidate, ToolMessage):
                    break
                actual.add(str(candidate.tool_call_id))
            if actual != expected:
                raise ValueError("tool calls must have immediate tool results")
        return super()._generate(messages, *args, **kwargs)


class RecordingCaptureCallback(BaseCallbackHandler):
    def __init__(self) -> None:
        self.metadata: list[dict[str, object]] = []

    def on_chat_model_start(
        self,
        _serialized,
        _messages,
        *,
        metadata=None,
        **_kwargs,
    ):
        self.metadata.append(dict(metadata or {}))


@pytest.mark.asyncio
async def test_main_and_compression_calls_share_capture_callback_and_correlation(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "config.json"
    settings_path.write_text(
        json.dumps(
            {
                "agent_max_context_tokens": 1_000,
                "context_compression_protect_first_n": 0,
            }
        ),
        encoding="utf-8",
    )
    model = CountingFakeMessagesListChatModel(
        profile={"max_input_tokens": 1000},
        responses=[
            AIMessage(content="short summary"),
            AIMessage(content="captured answer"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        context_settings=CompressionSettingsProvider(),
    )

    async def provider(_session_id: str):
        return [
            SessionEvent(
                "session-main", 0, "user/message", 0.0, "run-old",
                {"content": "old " + ("x" * 5_000), "thread_id": "thread-main"},
            ),
            SessionEvent(
                "session-main", 1, "assistant/text.delta", 1.0, "run-old",
                {"text": "old answer"},
            ),
            SessionEvent(
                "session-main", 2, "assistant/message.end", 2.0, "run-old",
                {"has_tool_calls": False},
            ),
        ]

    callback = RecordingCaptureCallback()
    engine = LangGraphEngine(
        graph,
        agent_id="agent-main",
        provider_id="custom:main",
        capture_callback=callback,
        transcript_provider=provider,
    )

    events = [
        event
        async for event in engine.stream(
            "continue",
            session_id="thread-main",
            product_session_id="session-main",
            control=RunControl(request_id="run-main", session_id="session-main"),
        )
    ]

    assert any(event.payload.get("text") == "captured answer" for event in events)
    assert len(callback.metadata) == 2
    assert {
        (
            item["provider_id"],
            item["agent_id"],
            item["session_id"],
            item["run_id"],
        )
        for item in callback.metadata
    } == {("custom:main", "agent-main", "session-main", "run-main")}
    assert {item.get("lc_source") for item in callback.metadata} == {
        None,
        "context_compression",
    }


@pytest.mark.asyncio
async def test_runtime_owns_capture_callback_and_passes_it_to_default_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def build_engine(_agent, _generation, _model, **kwargs):
        captured.update(kwargs)
        return RecordingEngine("ok")

    monkeypatch.setattr(
        "bianbu_agent.server.runtime.build_default_engine",
        build_engine,
    )
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=tmp_path / ".bianbu-agent"),
        default_model=OpenAICompatibleModel(
            model="qwen",
            provider_id="custom:main",
            api_key="secret",
            base_url="https://models.example.test/v1",
        ),
        agent_id="default",
        agent_registry=InMemoryAgentRegistry(
            (
                AgentSpec(
                    id="default",
                    name="Default",
                    model=AgentModel("custom:main", "qwen"),
                ),
            )
        ),
        model_resolver=type(
            "Resolver",
            (),
            {
                "resolve_agent_model": lambda self, selected: OpenAICompatibleModel(
                    model=selected.name,
                    provider_id=selected.provider,
                    api_key="secret",
                    base_url="https://models.example.test/v1",
                )
            },
        )(),
    )

    async with sdk:
        session = await sdk.sessions.create("capture-runtime", agent_id="default")
        await (await session.run("hello")).wait()
        assert captured["capture_callback"] is sdk.provider_capture_callback
        assert sdk.provider_capture_callback.store is sdk.provider_capture_store
        assert sdk.provider_capture_store.root == (
            tmp_path / ".bianbu-agent" / "logs" / "provider-capture"
        )


@pytest.mark.asyncio
async def test_runtime_prunes_provider_capture_on_start_and_periodically_while_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention is physical even when no capture API or callback is active."""

    import bianbu_agent.capture.llm as capture_module
    import bianbu_agent.server.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "PROVIDER_CAPTURE_MAINTENANCE_SECONDS", 0.01)
    data_dir = tmp_path / ".bianbu-agent"
    capture_root = data_dir / "logs" / "provider-capture"
    persisted = ProviderCaptureStore(capture_root)
    persisted.select("custom:persisted")
    startup_expired_id = persisted.write(
        {"provider_id": "custom:persisted", "input": "startup-expired"}
    )
    startup_expired_path = capture_root / "records" / f"{startup_expired_id}.json.gz"
    expired_at = capture_module.time.time() - capture_module.RETENTION_SECONDS - 1
    os.utime(startup_expired_path, (expired_at, expired_at))
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=data_dir),
        default_model=OpenAICompatibleModel(
            model="qwen",
            provider_id="custom:main",
            api_key="secret",
            base_url="https://models.example.test/v1",
        ),
        agent_id="default",
        agent_registry=InMemoryAgentRegistry(
            (AgentSpec(id="default", name="Default"),)
        ),
    )

    async with sdk:
        assert not startup_expired_path.exists()
        idle_expired_id = sdk.provider_capture_store.write(
            {"provider_id": "custom:persisted", "input": "idle-expired"}
        )
        idle_expired_path = capture_root / "records" / f"{idle_expired_id}.json.gz"
        os.utime(idle_expired_path, (expired_at, expired_at))

        async def wait_until_pruned() -> None:
            while idle_expired_path.exists():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_until_pruned(), timeout=1)
        assert sdk.provider_capture_store.target() == "custom:persisted"

    assert sdk._provider_capture_maintenance_task is None


@pytest.mark.asyncio
async def test_runtime_close_joins_blocked_capture_maintenance_before_clear_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close cannot return while a cancelled to-thread worker still mutates files."""

    import bianbu_agent.server.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "PROVIDER_CAPTURE_MAINTENANCE_SECONDS", 0.01)
    data_dir = tmp_path / ".bianbu-agent"

    def runtime() -> BianbuAgentRuntime:
        return BianbuAgentRuntime(
            config=RuntimeConfig(data_dir=data_dir),
            default_model=OpenAICompatibleModel(
                model="qwen",
                provider_id="custom:main",
                api_key="secret",
                base_url="https://models.example.test/v1",
            ),
            agent_id="default",
            agent_registry=InMemoryAgentRegistry(
                (AgentSpec(id="default", name="Default"),)
            ),
        )

    sdk = runtime()
    original_maintain = sdk.provider_capture_store.maintain
    worker_started = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked_second_maintenance() -> None:
        nonlocal calls
        with calls_lock:
            calls += 1
            selected_call = calls
        if selected_call == 2:
            worker_started.set()
            if not release_worker.wait(timeout=5):
                raise TimeoutError("test did not release capture maintenance")
            try:
                original_maintain()
            finally:
                worker_finished.set()
            return
        original_maintain()

    monkeypatch.setattr(
        sdk.provider_capture_store,
        "maintain",
        blocked_second_maintenance,
    )
    await sdk.start()

    async def wait_for_worker() -> None:
        while not worker_started.is_set():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait_for_worker(), timeout=1)
    close_task = asyncio.create_task(sdk.close())
    try:
        await asyncio.sleep(0.05)
        assert not close_task.done()
        assert not worker_finished.is_set()
    finally:
        release_worker.set()
        await asyncio.wait_for(close_task, timeout=2)

    assert worker_finished.is_set()
    assert sdk._provider_capture_maintenance_task is None
    sdk.provider_capture_store.clear()

    restarted = runtime()
    await restarted.start()
    try:
        assert restarted._provider_capture_maintenance_task is not None
    finally:
        await restarted.close()
    assert restarted._provider_capture_maintenance_task is None


@pytest.mark.parametrize(
    "command",
    [
        "rm file.txt",
        "sudo rm -rf build",
        "sudo --user root rm private.txt",
        "true && /bin/rm cache.db",
        "printf done\nrm next.txt",
        "env LANG=C command rm -- old.log",
        "printf '%s\\n' stale | xargs rm",
        "printf '%s\\n' stale | xargs -n 1 rm",
        "sh -c 'rm generated.txt'",
        "bash -lc 'rm generated.txt'",
        "sh -ec 'rm generated.txt'",
        "{ rm grouped.txt; }",
        "echo \"$(rm substituted.txt)\"",
        ">/tmp/rm-output rm redirected.txt",
        "2>/dev/null rm victim",
        "3>&1 rm victim",
        "sudo -nu root rm victim",
        "env -S 'rm victim'",
        "printf x | xargs -tn 1 rm",
        "printf x | xargs sh -c 'rm \"$@\"' _",
        "sh -c -- 'rm victim'",
        "find . -exec sh -c 'rm \"$1\"' _ {} \\;",
        "busybox sh -c 'rm victim'",
        "cat <<EOF\n$(rm victim)\nEOF\n",
        "cat <<EOF\n`rm victim`\nEOF\n",
        "echo '<<EOF'\nrm victim",
        "R=rm; $R victim",
        "$(printf rm) victim",
        "`printf rm` victim",
        "printf 'rm victim\\n' | sh",
        "bash <<'EOF'\nrm victim\nEOF\n",
        "sh <<< 'rm victim'",
        "bash -c 'cat <(rm victim)'",
        "bash --norc -c 'rm victim'",
        "env -a harmless rm victim",
        "env -Srm victim",
        "env -Srm; printf safe",
        "env -Srm&&printf safe",
        "exec -a harmless rm victim",
        "/usr/bin/time -f '%E' rm victim",
        "nice -n 5 rm victim",
        "timeout 10 rm victim",
        "busybox ash -c 'rm victim'",
        "sudo 2>/dev/null rm victim",
        "find . -exec echo {} \\; -exec rm {} \\;",
        "bash -c 'alias zap=rm; zap victim'",
        "r''m victim",
        r"r\m victim",
        "r\\\nm victim",
        "r$''m victim",
        'r$""m victim',
        "echo rm",
        "command -v rm",
        "rm() { printf '%s\\n' harmless; }",
        "cat <<'EOF'\nrm file.txt\nEOF\n",
    ],
)
def test_terminal_rm_commands_require_approval(command: str) -> None:
    assert _terminal_command_requires_approval({"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "rmdir empty-directory",
        "echo remove",
        "printf '%s' harmless",
        "python -c 'print(\"safe\")'",
        "ärm file",
        "rmé file",
    ],
)
def test_terminal_non_rm_commands_do_not_require_approval(command: str) -> None:
    assert _terminal_command_requires_approval({"command": command}) is False


@pytest.mark.asyncio
async def test_terminal_rm_tool_call_emits_approval_before_execution() -> None:
    executions: list[str] = []

    @tool
    def terminal(command: str) -> str:
        """Execute a terminal command."""
        executions.append(command)
        return "executed"

    model = CountingFakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "terminal",
                    "args": {"command": "rm important.txt"},
                    "id": "call-rm",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="deleted"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        tools=[terminal],
    )
    engine = LangGraphEngine(graph)

    control = RunControl(request_id="run-rm", session_id="session-rm")
    executions_when_asked: list[list[str]] = []

    def answer(payload):
        assert payload["actions"][0]["name"] == "terminal"
        assert payload["actions"][0]["arguments"] == {
            "command": "rm important.txt"
        }
        # Approval must arrive before the tool has had any chance to run.
        executions_when_asked.append(list(executions))
        return [{"type": "approve"}]

    events = await stream_answering_approvals(
        engine,
        "delete it",
        control=control,
        session_id="thread-rm",
        answer=answer,
    )

    assert executions_when_asked == [[]]
    assert executions == ["rm important.txt"]
    assert any(
        event.kind == "token" and event.payload["text"] == "deleted"
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expects_approval"),
    [("echo harmless", False), ("rm important.txt", True)],
)
async def test_terminal_approval_follows_when_predicate_without_security_feature(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    expects_approval: bool,
) -> None:
    """BIANBU_SECURITY=0 时 terminal 仍要按谓词判定，而不是无条件弹窗。

    官方 ``HumanInTheLoopMiddleware`` 不读 ``InterruptOnConfig.when``；以前
    非 securityd 分支直接用它，`_terminal_command_requires_approval` 就成了死
    代码，每条命令都弹窗（K3 源部署实测）。
    """
    monkeypatch.setenv("BIANBU_SECURITY", "0")
    executions: list[str] = []

    @tool
    def terminal(command: str) -> str:
        """Execute a terminal command."""
        executions.append(command)
        return "executed"

    suffix = "safe" if not expects_approval else "rm"
    model = CountingFakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "terminal",
                    "args": {"command": command},
                    "id": f"call-{suffix}",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        tools=[terminal],
    )

    events = await stream_answering_approvals(
        LangGraphEngine(graph),
        "run it",
        control=RunControl(
            request_id=f"run-when-{suffix}",
            session_id=f"session-when-{suffix}",
        ),
        session_id=f"thread-when-{suffix}",
        answer=lambda payload: [{"type": "approve"}],
    )

    # The predicate decides whether approval is asked at all; once approved the
    # command runs either way.
    interrupted = any(event.kind == "approval_required" for event in events)
    assert interrupted is expects_approval
    assert executions == [command]


@pytest.mark.asyncio
async def test_compatible_hitl_middleware_is_installed_without_security_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 securityd 分支也必须挂兼容中间件，不能依赖 LangChain 的原生 `when`。

    设备上跑的是 LangChain 1.2.18，它的 ``HumanInTheLoopMiddleware`` 根本不读
    ``when``；本地 venv 是 1.3.x，读了。只做行为断言会因版本而异，所以这里直接
    固定「装的是哪个中间件」：谓词必须由兼容层求值。
    """
    monkeypatch.setenv("BIANBU_SECURITY", "0")
    recorded: list[dict[str, object]] = []

    def fake_builder(interrupt_on, **kwargs):
        recorded.append({"interrupt_on": dict(interrupt_on), **kwargs})
        return HumanInTheLoopMiddleware(
            interrupt_on={
                name: {"allowed_decisions": ["approve", "reject"]}
                for name in interrupt_on
            },
            description_prefix="工具执行需要人工批准",
        )

    @tool
    def terminal(command: str) -> str:
        """Execute a terminal command."""
        return "executed"

    monkeypatch.setattr(
        "bianbu_agent.engines.langgraph.engine._dynamic_hitl_builder",
        lambda: fake_builder,
    )
    model = CountingFakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "terminal",
                    "args": {"command": "echo harmless"},
                    "id": "call-structure",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="done"),
        ]
    )
    build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        tools=[terminal],
    )

    assert len(recorded) == 1
    assert recorded[0]["terminal_names"] == {"terminal"}
    assert recorded[0]["terminal_when"] is _terminal_tool_call_requires_approval
    assert isinstance(recorded[0]["tool_metadata"], dict)
    assert recorded[0]["protected_names"] == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("security_enabled", ["1", "0"])
async def test_mcp_memory_tool_keeps_the_destructive_remove_predicate(
    monkeypatch: pytest.MonkeyPatch, security_enabled: str
) -> None:
    """tools → MCP 后 memory 只换了限定名，remove 仍要走人工审批。

    引擎靠 ``sdk_qualified_name`` 的末段认出记忆工具；MCP 改名成
    ``<mcp_id>/<local_name>`` 后如果按模型侧编码名硬匹配，remove 就会在
    没有审批的情况下真删。securityd 开与关都必须挂上同一条谓词。
    """

    from langchain_core.tools import StructuredTool

    from bianbu_agent.engines.names import encode_tool_name

    monkeypatch.setenv("BIANBU_SECURITY", security_enabled)
    recorded: list[dict[str, object]] = []

    def fake_builder(interrupt_on, **kwargs):
        recorded.append({"interrupt_on": dict(interrupt_on), **kwargs})
        return HumanInTheLoopMiddleware(
            interrupt_on={
                name: {"allowed_decisions": ["approve", "reject"]}
                for name in interrupt_on
            },
            description_prefix="工具执行需要人工批准",
        )

    def remember(target: str, action: str, memory_id: str) -> str:
        """AgentMemory."""
        del target, action, memory_id
        return "ok"

    monkeypatch.setattr(
        "bianbu_agent.engines.langgraph.engine._dynamic_hitl_builder",
        lambda: fake_builder,
    )
    qualified_id = "bianbu.memory/memory/memory"
    encoded = encode_tool_name(qualified_id)
    memory_tool = StructuredTool.from_function(
        func=remember,
        name=encoded,
        description="AgentMemory",
        metadata={
            "qualified_id": qualified_id,
            "sdk_qualified_name": qualified_id,
        },
    )
    build_langgraph_agent(
        model=FakeMessagesListChatModel(responses=[AIMessage(content="done")]),
        system_prompt="Test agent",
        tools=[memory_tool],
    )

    assert len(recorded) == 1
    assert recorded[0]["memory_names"] == {encoded}
    assert recorded[0]["memory_when"] is _memory_tool_call_requires_approval
    config = recorded[0]["interrupt_on"][encoded]
    assert config["allowed_decisions"] == ["approve", "reject"]
    if security_enabled == "0":
        # securityd 不在场时静态谓词必须自己挂在 interrupt_on 上，否则
        # 「需要确认」会静默降级成「直接执行」。
        assert config["when"] is _memory_tool_call_requires_approval
    else:
        assert "when" not in config


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("write_file", {"path": "poem.txt", "content": "hello"}),
        ("read_file", {"path": "poem.txt"}),
    ],
)
@pytest.mark.usefixtures("allow_model_dlp")
async def test_security_confirm_for_file_tools_interrupts_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, str],
) -> None:
    """动态 securityd CONFIRM 必须在工具执行前进入 Runtime 审批流。"""

    executions: list[dict[str, str]] = []

    if tool_name == "write_file":
        @tool
        async def write_file(path: str, content: str) -> str:
            """Write a test file."""
            executions.append({"path": path, "content": content})
            return "file result"

        built_tool = write_file
    else:
        @tool
        async def read_file(path: str) -> str:
            """Read a test file."""
            executions.append({"path": path})
            return "file result"

        built_tool = read_file

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setattr(
        security_middleware,
        "securityd_requires_confirm",
        lambda request: request.tool_call.get("name") == tool_name,
    )

    model = CountingFakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{
                    "name": tool_name,
                    "args": arguments,
                    "id": f"call-{tool_name}",
                    "type": "tool_call",
                }],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        tools=[built_tool],
    )
    engine = LangGraphEngine(graph)

    executions_when_asked: list[list[object]] = []

    def answer(payload):
        assert payload["actions"][0]["name"] == tool_name
        # Approval must arrive before the tool has had any chance to run.
        executions_when_asked.append(list(executions))
        return [{"type": "approve"}]

    events = await stream_answering_approvals(
        engine,
        "use file tool",
        control=RunControl(
            request_id=f"run-{tool_name}",
            session_id=f"session-{tool_name}",
        ),
        session_id=f"session-{tool_name}",
        answer=answer,
    )

    assert executions_when_asked == [[]]
    assert executions == [arguments]
    assert any(
        event.kind == "token" and event.payload["text"] == "done"
        for event in events
    )


@pytest.mark.asyncio
async def test_tool_metadata_approval_is_honored_without_security_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BIANBU_SECURITY=0 只关动态判权，不该关工具自己声明的审批。

    关掉 securityd 曾经把 ``requires_approval`` 一起静默丢掉，MCP 的
    ``destructiveHint`` 因此降级成直接执行（K3/10.0.91.72 实测 appcli_run
    无审批落地）。工具契约与 securityd 是否在场无关。
    """
    monkeypatch.setenv("BIANBU_SECURITY", "0")
    executions: list[str] = []

    @tool
    def protected(value: str) -> str:
        """Run a protected operation."""
        executions.append(value)
        return "executed"

    protected.metadata = {"requires_approval": True}

    model = CountingFakeMessagesListChatModel(
        responses=[
            AIMessage(content="", tool_calls=[{
                "name": "protected",
                "args": {"value": "important"},
                "id": "call-protected",
                "type": "tool_call",
            }]),
            AIMessage(content="done"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        tools=[protected],
        hitl_tools=[],
    )
    engine = LangGraphEngine(graph)

    executions_when_asked: list[list[str]] = []

    def answer(payload):
        assert payload["actions"][0]["name"] == "protected"
        executions_when_asked.append(list(executions))
        return [{"type": "approve"}]

    events = await stream_answering_approvals(
        engine,
        "run it",
        control=RunControl(
            request_id="run-protected",
            session_id="session-protected",
        ),
        session_id="thread-protected",
        answer=answer,
    )

    assert executions_when_asked == [[]]
    assert executions == ["important"]
    assert any(event.kind == "approval_required" for event in events)


@pytest.mark.asyncio
@pytest.mark.usefixtures("allow_model_dlp")
async def test_tool_metadata_approval_requires_security_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    executions: list[str] = []

    @tool
    def protected(value: str) -> str:
        """Run a protected operation."""
        executions.append(value)
        return "executed"

    protected.metadata = {"requires_approval": True}

    model = CountingFakeMessagesListChatModel(
        responses=[AIMessage(content="", tool_calls=[{
            "name": "protected",
            "args": {"value": "important"},
            "id": "call-protected-security",
            "type": "tool_call",
        }]),
            AIMessage(content="done"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        tools=[protected],
        hitl_tools=[],
    )
    engine = LangGraphEngine(graph)

    events = await stream_answering_approvals(
        engine,
        "run it",
        control=RunControl(
            request_id="run-protected-security",
            session_id="session-protected-security",
        ),
        session_id="thread-protected-security",
        answer=lambda payload: [{"type": "reject"}],
    )

    # The metadata-declared approval is asked, and without it nothing runs.
    assert any(event.kind == "approval_required" for event in events)
    assert executions == []


@pytest.mark.asyncio
async def test_dangling_tool_call_is_repaired_while_folding_the_transcript() -> None:
    """A Run that died mid-tool must not hand the provider an unanswered call."""

    model = RejectsDanglingToolCallsModel(responses=[AIMessage(content="recovered")])
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        tools=[],
    )

    async def provider(_session_id: str):
        return [
            SessionEvent(
                "session-dangling-tool", 0, "user/message", 0.0, "run-old",
                {"content": "old request"},
            ),
            SessionEvent(
                "session-dangling-tool", 1, "tool/call.started", 1.0, "run-old",
                {
                    "call_id": "call-dangling-tool",
                    "name": "protected",
                    "arguments": {"value": "important"},
                },
            ),
            SessionEvent(
                "session-dangling-tool", 2, "user/message", 2.0, "run-retry",
                {"content": "previous failed retry"},
            ),
        ]

    engine = LangGraphEngine(graph, transcript_provider=provider)
    events = [
        event
        async for event in engine.stream(
            "try again",
            session_id="thread-dangling-tool",
            control=RunControl(
                request_id="run-after-dangling-tool",
                session_id="session-dangling-tool",
            ),
        )
    ]

    assert model.model_calls == 1
    assert any(
        event.kind == "token" and event.payload["text"] == "recovered"
        for event in events
    )


@pytest.mark.usefixtures("allow_model_dlp")
async def test_cancelling_a_parked_approval_runs_no_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a Run parked on approval closes it without executing tools.

    Approval no longer ends the Run, so there is no graph checkpoint to scrub
    on cancel: the Run is aborted in place and the fold repairs the dangling
    tool calls the next time the transcript is read.
    """

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    executions: list[tuple[str, str]] = []

    @tool
    def protected(value: str) -> str:
        """Run a protected operation."""
        executions.append(("protected", value))
        return "executed"

    protected.metadata = {"requires_approval": True}

    @tool
    def automatic(value: str) -> str:
        """Run an operation without approval."""
        executions.append(("automatic", value))
        return "executed"

    model = CountingFakeMessagesListChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "protected",
                        "args": {"value": "important"},
                        "id": "call-cancelled-approval",
                        "type": "tool_call",
                    },
                    {
                        "name": "automatic",
                        "args": {"value": "also-important"},
                        "id": "call-cancelled-automatic",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="continued safely"),
        ]
    )
    graph = build_langgraph_agent(
        model=model,
        system_prompt="Test agent",
        tools=[protected, automatic],
    )
    engine = LangGraphEngine(graph)
    control = RunControl(
        request_id="run-cancelled-approval",
        session_id="session-cancelled-approval",
    )

    iterator = engine.stream(
        "run it",
        session_id="thread-cancelled-approval",
        control=control,
    ).__aiter__()
    try:
        while True:
            event = await anext(iterator)
            if event.kind == "approval_required":
                break
        # Approval parks the Run before any tool runs.
        assert executions == []
        await control.cancel()
        with pytest.raises(StopAsyncIteration):
            while True:
                await anext(iterator)
    finally:
        await iterator.aclose()

    assert executions == []
    assert model.model_calls == 1


async def _events(runtime: AgentRuntime, *, agent_id: str | None):
    return [
        event
        async for event in runtime.stream(
            "What changed?",
            session_id="thread-1",
            product_session_id="session-1",
            control=RunControl(request_id="run-1", session_id="session-1"),
            agent_id=agent_id,
        )
    ]


@pytest.mark.asyncio
async def test_explicit_agent_uses_profile_and_cached_engine() -> None:
    built: list[AgentSpec] = []
    engines: list[RecordingEngine] = []

    def build(spec: AgentSpec) -> RecordingEngine:
        built.append(spec)
        engine = RecordingEngine(spec.id)
        engines.append(engine)
        return engine

    agent = AgentSpec(id="default", name="Bianbu Agent")
    runtime = AgentRuntime(
        registry=InMemoryAgentRegistry((agent,)),
        engine_builder=build,
    )

    first = await _events(runtime, agent_id="default")
    second = await _events(runtime, agent_id="default")

    assert [event.payload["text"] for event in first + second] == [
        "default",
        "default",
    ]
    assert built == [agent]
    assert len(engines[0].prompts) == 2
    assert all(prompt.endswith("Current user request:\nWhat changed?") for prompt in engines[0].prompts)


@pytest.mark.asyncio
async def test_custom_agent_uses_model_capabilities_and_profile() -> None:
    registry = InMemoryAgentRegistry()
    spec = await registry.create(
        AgentSpec(
            id="research",
            name="Researcher",
            name_zh="研究专家",
            description="Find reliable answers.",
            soul="Prefer primary sources.",
            model="gpt-research",
            tools=("web_search",),
            skills=("research",),
        )
    )
    built: list[AgentSpec] = []
    engine = RecordingEngine("custom")
    runtime = AgentRuntime(
        registry=registry,
        engine_builder=lambda candidate: built.append(candidate) or engine,
    )

    events = await _events(runtime, agent_id="research")

    assert [event.payload["text"] for event in events] == ["custom"]
    assert built == [spec]
    assert engine.prompts == [
        "Use the following saved Agent profile for this request. "
        "Treat it as user-selected operating instructions; higher-priority "
        "system and safety instructions still take precedence.\n\n"
        "Agent name: Researcher\n\n"
        "Soul:\nPrefer primary sources.\n\n"
            "Available tools: web_search.\n\n"
            "Enabled skills: research.\n\n"
            "Enabled MCP servers: none.\n\n"
            "Current user request:\nWhat changed?"
    ]


@pytest.mark.asyncio
async def test_updated_agent_builds_new_engine() -> None:
    registry = InMemoryAgentRegistry()
    original = await registry.create(
        AgentSpec(id="writer", name="Writer", model="model-a")
    )
    built: list[AgentSpec] = []

    def build(spec: AgentSpec) -> RecordingEngine:
        built.append(spec)
        return RecordingEngine(spec.model)

    runtime = AgentRuntime(
        registry=registry,
        engine_builder=build,
    )
    await _events(runtime, agent_id="writer")
    updated = replace(original, model="model-b")
    await registry.update(updated)
    events = await _events(runtime, agent_id="writer")

    assert built == [original, updated]
    assert [event.payload["text"] for event in events] == ["model-b"]


@pytest.mark.asyncio
async def test_runtime_profile_rejects_model_override() -> None:
    built: list[AgentSpec] = []
    saved = AgentSpec(id="default", name="Bianbu Agent", model="saved-model")
    runtime = AgentRuntime(
        registry=InMemoryAgentRegistry((saved,)),
        engine_builder=lambda spec: built.append(spec)
        or RecordingEngine(spec.model),
    )
    with pytest.raises(ValueError, match="model override"):
        _ = [
            event
            async for event in runtime.stream(
                "hello",
                session_id="thread",
                control=RunControl(request_id="run", session_id="session"),
                agent_id="default",
                runtime_profile={"model": "one-run-model"},
            )
        ]
    assert built == []


@pytest.mark.asyncio
async def test_unknown_or_deleted_agent_fails_before_engine_build() -> None:
    registry = InMemoryAgentRegistry()
    await registry.create(AgentSpec(id="temporary", name="Temporary"))
    built: list[AgentSpec] = []
    runtime = AgentRuntime(
        registry=registry,
        engine_builder=lambda spec: built.append(spec) or RecordingEngine(spec.id),
    )
    await registry.delete("temporary")

    with pytest.raises(AgentNotFoundError, match="agent not found: temporary"):
        await _events(runtime, agent_id="temporary")
    assert built == []


@pytest.mark.asyncio
async def test_direct_reply_is_one_stable_recoverable_message_pair() -> None:
    """A hook's direct reply is a durable user/assistant pair, never a duplicate."""

    async def provider(_session_id: str):
        return [
            SessionEvent(
                "session-direct", 0, "user/message", 0.0, "request-1",
                {"content": "hi"},
            ),
            SessionEvent(
                "session-direct", 1, "assistant/text.delta", 1.0, "request-1",
                {"text": "hello"},
            ),
            SessionEvent(
                "session-direct", 2, "run/completed", 2.0, "request-1",
                {"status": "completed", "content": "hello"},
            ),
        ]

    engine = LangGraphEngine(
        build_langgraph_agent(
            model=FakeMessagesListChatModel(responses=[AIMessage(content="unused")]),
            system_prompt="Test agent",
        ),
        transcript_provider=provider,
    )

    folded = await engine._history("session-direct")
    assert [
        (type(message), message.id, message.content) for message in folded
    ] == [
        (HumanMessage, "bianbu-agent-run:request-1", "hi"),
        (AIMessage, None, "hello"),
    ]
    assert await engine.has_thread_input("session-direct", "request-1") is True
    assert await engine.recover_completed_events("session-direct", "request-1") == [
        RunEvent("token", {"text": "hello"})
    ]


@pytest.mark.asyncio
async def test_recovery_separates_adversarial_run_ids() -> None:
    """One Run id that is a prefix of another must not swallow its turn."""

    async def provider(_session_id: str):
        return [
            SessionEvent(
                "session-ids", 0, "user/message", 0.0, "r", {"content": "first"},
            ),
            SessionEvent(
                "session-ids", 1, "assistant/text.delta", 1.0, "r",
                {"text": "answer-one"},
            ),
            SessionEvent(
                "session-ids", 2, "user/message", 2.0, "r:assistant",
                {"content": "second"},
            ),
            SessionEvent(
                "session-ids", 3, "assistant/text.delta", 3.0, "r:assistant",
                {"text": "answer-two"},
            ),
        ]

    engine = LangGraphEngine(
        build_langgraph_agent(
            model=FakeMessagesListChatModel(responses=[AIMessage(content="unused")]),
            system_prompt="Test agent",
        ),
        transcript_provider=provider,
    )

    folded = await engine._history("session-ids")
    assert [
        message.id for message in folded if message.id is not None
    ] == [
        "bianbu-agent-run:r",
        "bianbu-agent-run:r:assistant",
    ]
    assert await engine.recover_completed_events("session-ids", "r") == [
        RunEvent("token", {"text": "answer-one"})
    ]
    assert await engine.recover_completed_events("session-ids", "r:assistant") == [
        RunEvent("token", {"text": "answer-two"})
    ]


@pytest.mark.asyncio
async def test_recovery_uses_the_normalized_message_namespace() -> None:
    """A namespace alias must resolve to the same message prefix as the codec."""

    async def provider(_session_id: str):
        return [
            SessionEvent(
                "session-alias", 0, "user/message", 0.0, "r", {"content": "first"},
            ),
            SessionEvent(
                "session-alias", 1, "assistant/text.delta", 1.0, "r",
                {"text": "answer-one"},
            ),
        ]

    engine = LangGraphEngine(
        build_langgraph_agent(
            model=FakeMessagesListChatModel(responses=[AIMessage(content="unused")]),
            system_prompt="Test agent",
        ),
        message_namespace="bianbu-agent-run:",
        transcript_provider=provider,
    )

    assert engine._message_ids.prefix == "bianbu-agent-run:"
    assert await engine.recover_completed_events("session-alias", "r") == [
        RunEvent("token", {"text": "answer-one"})
    ]
