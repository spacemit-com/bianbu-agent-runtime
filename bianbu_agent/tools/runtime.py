"""Generic, call-scoped runtime exposed to directory-owned Tool packages."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bianbu_agent.config import RuntimeConfig
from bianbu_agent.home import AgentHome
from bianbu_agent.models import YAMLModelResolver
from bianbu_agent.plugins import ToolCallContext
from bianbu_agent.tools.runtime_services import RuntimeServices
from bianbu_agent.tools.models import ToolPackageContext
from bianbu_agent.tools.processes import (
    ProcessManager,
    ProcessRequest,
    ProcessResult,
)
from bianbu_agent.tools.browser import BrowserManager

if TYPE_CHECKING:
    from bianbu_agent.server.runtime import BianbuAgentRuntime


class ToolPermissionError(PermissionError):
    """A Tool call attempted to exceed its package or Agent scope."""


@dataclass(frozen=True, slots=True)
class ToolExecutionScope:
    tool_name: str
    run_id: str
    session_id: str
    agent_id: str
    workspace: Path
    enabled_tools: tuple[str, ...]
    allowed_skills: tuple[str, ...] | None
    permissions: frozenset[str]
    hook_ids: tuple[str, ...] = ()


ScopeResolver = Callable[
    [str],
    Awaitable[tuple[tuple[str, ...], tuple[str, ...] | None]],
]
_DEFAULT_SCOPE_RESOLVER = object()


class ToolRuntime:
    def __init__(
        self,
        *,
        scope: ToolExecutionScope,
        state_root: Path,
        process_manager: ProcessManager | None = None,
        browser_manager: BrowserManager | None = None,
        services: RuntimeServices | None = None,
    ) -> None:
        self.scope = scope
        self._services = services
        self.state = ToolState(state_root, scope.permissions)
        self.workspace = ToolWorkspace(scope.workspace, services)
        self.sessions = ToolSessions(services)
        self.processes = ToolProcesses(
            process_manager or ProcessManager(workspace=scope.workspace),
        )
        self.browser = ToolBrowser(scope, browser_manager or BrowserManager())
        self.console_state = ToolConsoleState(services)
        self.agents = ToolAgents(scope, services)
        self.interaction = ToolInteraction(services)
        self.tools = ToolCalls(scope, services)

    def resolve_model_capability(self, name: str) -> object | None:
        if self._services is None:
            return None
        return self._services.resolve_model_capability(name)

class ToolRuntimeFactory:
    def __init__(
        self,
        *,
        workspace_root: Path,
        state_root: Path | None = None,
        scope_resolver: ScopeResolver | None = None,
        services: RuntimeServices | None = None,
        browser_manager: BrowserManager | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.state_root = (
            Path(state_root or self.workspace_root.parent / "runtime" / "tool-state")
            .expanduser()
            .resolve()
        )
        self._scope_resolver = scope_resolver
        self._services = services
        self._process_manager = ProcessManager(workspace=self.workspace_root)
        self._browser_manager = browser_manager or BrowserManager()

    async def for_call(
        self,
        package: ToolPackageContext,
        call: ToolCallContext,
    ) -> ToolRuntime:
        return await self._for_call(package, call, services=self._services)

    async def _for_call(
        self,
        package: ToolPackageContext,
        call: ToolCallContext,
        *,
        services: RuntimeServices | None,
        selected_by_agent: bool = False,
        scope_resolver: ScopeResolver | None | object = _DEFAULT_SCOPE_RESOLVER,
    ) -> ToolRuntime:
        selected_scope_resolver = (
            self._scope_resolver
            if scope_resolver is _DEFAULT_SCOPE_RESOLVER
            else scope_resolver
        )
        if selected_scope_resolver is None:
            enabled_tools = (package.qualified_name,)
            allowed_skills = None
        else:
            try:
                enabled_tools, allowed_skills = await selected_scope_resolver(
                    call.agent_id
                )
            except KeyError as exc:
                raise ToolPermissionError(f"unknown Agent: {call.agent_id}") from exc
        if package.qualified_name not in enabled_tools and not selected_by_agent:
            raise ToolPermissionError(
                f"Tool {package.qualified_name} is not enabled for Agent {call.agent_id}"
            )
        scope = ToolExecutionScope(
            tool_name=package.qualified_name,
            run_id=call.run_id,
            session_id=call.session_id,
            agent_id=call.agent_id,
            workspace=self.workspace_root,
            enabled_tools=tuple(enabled_tools),
            allowed_skills=(
                tuple(allowed_skills) if allowed_skills is not None else None
            ),
            permissions=frozenset(),
            hook_ids=_call_hook_snapshot(call),
        )
        return ToolRuntime(
            scope=scope,
            state_root=self.state_root,
            process_manager=self._process_manager,
            browser_manager=self._browser_manager,
            services=services,
        )

    def for_tool_generation(self, name: str) -> object:
        services = self._services
        if services is None:
            return self
        local_name = str(name).rsplit("/", 1)[-1]
        generation_services = services.for_tool_generation(local_name)
        if generation_services is services:
            return self
        return _GenerationToolRuntimeFactory(self, generation_services)

    def tool_unavailable_reason(self, name: str) -> str | None:
        if self._services is None:
            return None
        return self._services.tool_unavailable_reason(
            str(name).rsplit("/", 1)[-1]
        )

    async def _agent_selects_tool(self, agent_id: str, name: str) -> bool:
        return False

    async def close(self) -> None:
        await self._process_manager.close()


class ToolRuntimeServices(ToolRuntimeFactory):
    """Product composition adapter retained while callers adopt ToolRuntimeFactory."""

    def __init__(
        self,
        *,
        home: AgentHome,
        model: object | None,
        config: RuntimeConfig | None = None,
        model_resolver: YAMLModelResolver | None = None,
        sessions_root: Path | None = None,
        runtime_home: Path | None = None,
    ) -> None:
        from bianbu_agent.engines.default import to_langchain_model

        selected_config = config or RuntimeConfig(data_dir=home.root)
        selected_resolver = model_resolver or YAMLModelResolver(home.models_dir)
        services = RuntimeServices(
            sessions_root=sessions_root or home.sessions_dir,
            workspace=home.workspace_dir,
            model=(to_langchain_model(model) if model is not None else None),
            runtime_home=runtime_home or home.root / "runtime",
            model_resolver=selected_resolver,
            config=selected_config,
            model_provider_id=str(getattr(model, "provider_id", "") or ""),
        )
        super().__init__(
            workspace_root=home.workspace_dir,
            state_root=(runtime_home or home.root / "runtime") / "tool-state",
            scope_resolver=self.resolve_agent_capabilities,
            services=services,
        )
        self.home = home
        self.model = model
        self.config = selected_config
        self.model_resolver = selected_resolver
        self.sdk: BianbuAgentRuntime | None = None
        self._generation: object | None = None
        self._candidate_services: list[object] | None = None
        self._candidate_capabilities: dict[str, object | None] | None = None
        self._candidate_model: object | None = None

    @property
    def workspace(self) -> Path:
        return self.workspace_root

    def bind_sdk(self, sdk: BianbuAgentRuntime) -> None:
        if sdk.home.root != self.home.root:
            raise ValueError("Tool runtime and service runtime must use the same Home")
        self.sdk = sdk
        self._services.capture_callback = sdk.provider_capture_callback

    async def start(self, sdk: BianbuAgentRuntime, generation: object) -> None:
        if self.sdk is not sdk:
            raise ValueError("Tool runtime is not bound to this service runtime")
        if self._generation is not generation:
            raise RuntimeError("Tool runtime Generation was not activated")

    def begin_generation_candidate(self) -> None:
        if self._candidate_services is not None:
            raise RuntimeError("Tool runtime candidate build is already active")
        from bianbu_agent.engines.default import to_langchain_model

        chat = self._services.resolve_model_capability("chat")
        self._candidate_services = []
        self._candidate_capabilities = {
            "chat": chat,
            "vision": self._services.resolve_model_capability("vision"),
        }
        self._candidate_model = to_langchain_model(chat) if chat is not None else None

    def for_tool_generation(self, _name: str) -> object:
        from bianbu_agent.engines.default import to_langchain_model
        from bianbu_agent.tools.runtime_services import _GenerationRuntimeServices

        if self._candidate_capabilities is None:
            chat = self._services.resolve_model_capability("chat")
            capabilities = {
                "chat": chat,
                "vision": self._services.resolve_model_capability("vision"),
            }
            model = to_langchain_model(chat) if chat is not None else None
        else:
            capabilities = self._candidate_capabilities
            model = self._candidate_model
        chat = capabilities.get("chat")
        generation_services = _GenerationRuntimeServices(
            self._services,
            capabilities,
            scope_resolver=self.resolve_agent_capabilities,
            model=model,
            model_provider_id=str(getattr(chat, "provider_id", "") or ""),
            capture_callback=self._services.capture_callback,
        )
        if self._candidate_services is not None:
            self._candidate_services.append(generation_services)
        return _GenerationToolRuntimeFactory(self, generation_services)

    def prepare_generation_candidate(self, generation: object) -> object:
        if self._candidate_services is None:
            raise RuntimeError("Tool runtime candidate build was not begun")
        from bianbu_agent.engines.langgraph import build_plugin_tools

        available_tools = build_plugin_tools(
            generation.tools.values(),
            hook_runner=generation,
            hook_ids_resolver=lambda agent_id: self.resolve_agent_hooks(
                agent_id,
                generation,
            ),
        )
        prepared = (
            generation,
            tuple(self._candidate_services),
            available_tools,
            self._generation,
            self._services.available_tools,
        )
        self._candidate_services = None
        self._candidate_capabilities = None
        self._candidate_model = None
        return prepared

    def activate_generation_candidate(self, prepared: object) -> None:
        (
            generation,
            generation_services,
            available_tools,
            _old_generation,
            _old_tools,
        ) = prepared
        for services in generation_services:
            services.activate(generation, available_tools)
        self._generation = generation
        self._services.available_tools = available_tools

    def rollback_generation_candidate(self, prepared: object) -> None:
        (
            _generation,
            generation_services,
            _available_tools,
            old_generation,
            old_tools,
        ) = prepared
        for services in generation_services:
            services.deactivate()
        self._generation = old_generation
        self._services.available_tools = old_tools

    def abort_generation_candidate(self) -> None:
        self._candidate_services = None
        self._candidate_capabilities = None
        self._candidate_model = None

    async def close(self) -> None:
        errors: list[BaseException] = []
        try:
            await super().close()
        except BaseException as exc:
            errors.append(exc)
        self._services.available_tools = []
        if errors:
            raise ExceptionGroup("Tool runtime close failed", errors)

    async def resolve_agent_capabilities(
        self,
        agent_id: str,
        generation: object | None = None,
    ) -> tuple[tuple[str, ...], tuple[str, ...] | None]:
        if self.sdk is None:
            raise KeyError(agent_id)
        selected_generation = self._generation if generation is None else generation
        agent = await self.sdk.agents.materialize(agent_id, selected_generation)
        return tuple(agent.tools), tuple(agent.skills)

    async def resolve_agent_hooks(
        self,
        agent_id: str,
        generation: object | None = None,
    ) -> tuple[str, ...]:
        if self.sdk is None:
            raise KeyError(agent_id)
        selected_generation = self._generation if generation is None else generation
        agent = await self.sdk.agents.materialize(agent_id, selected_generation)
        return tuple(agent.hooks)

    async def _agent_selects_tool(self, agent_id: str, name: str) -> bool:
        if self.sdk is None:
            return False
        try:
            generation = self._generation
            if generation is None:
                return False
            agent = await self.sdk.agents.materialize(agent_id, generation)
        except KeyError:
            return False
        return name in agent.tools


class _GenerationToolRuntimeFactory:
    """Bind Tool calls and availability to one immutable service snapshot."""

    def __init__(
        self,
        factory: ToolRuntimeFactory,
        services: RuntimeServices,
    ) -> None:
        self._factory = factory
        self._services = services

    async def for_call(
        self,
        package: ToolPackageContext,
        call: ToolCallContext,
    ) -> ToolRuntime:
        selected = await self._factory._agent_selects_tool(
            call.agent_id,
            package.qualified_name,
        )
        return await self._factory._for_call(
            package,
            call,
            services=self._services,
            selected_by_agent=selected,
            scope_resolver=(
                self._services.resolve_agent_capabilities
                if getattr(self._services, "_scope_resolver", None) is not None
                else _DEFAULT_SCOPE_RESOLVER
            ),
        )

    def tool_unavailable_reason(self, name: str) -> str | None:
        return self._services.tool_unavailable_reason(name)


class ToolWorkspace:
    def __init__(self, root: Path, services: RuntimeServices | None) -> None:
        self.root = root
        self._services = services



class ToolSessions:
    def __init__(self, services: RuntimeServices | None) -> None:
        self._services = services

    async def records(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        if self._services is None:
            return [], []
        sessions, runs = await asyncio.to_thread(self._services.records)
        return sessions, runs


class ToolProcesses:
    def __init__(
        self,
        manager: ProcessManager,
    ) -> None:
        self._manager = manager

    async def run(
        self,
        *,
        command: str,
        timeout_seconds: int = 180,
        workdir: str | None = None,
    ) -> ProcessResult:
        return await self._manager.run(
            ProcessRequest(
                command=command,
                timeout_seconds=max(1, int(timeout_seconds)),
                workdir=workdir,
                pty=False,
            )
        )

    async def start_background(
        self,
        *,
        command: str,
        timeout_seconds: int = 180,
        workdir: str | None = None,
    ):
        return await self._manager.start(
            ProcessRequest(
                command=command,
                timeout_seconds=max(1, int(timeout_seconds)),
                workdir=workdir,
                pty=False,
            )
        )

    async def wait(self, session_id: str, timeout_seconds: int) -> ProcessResult:
        return await self._manager.wait(
            session_id,
            timeout_seconds=timeout_seconds,
        )

    async def list(self):
        return await self._manager.list()

    async def poll(self, session_id: str):
        return await self._manager.poll(session_id)

    async def log(self, session_id: str, *, offset: int | None, limit: int):
        return await self._manager.log(session_id, offset=offset, limit=limit)

    async def write(self, session_id: str, data: str) -> None:
        await self._manager.write(session_id, data)

    async def kill(self, session_id: str) -> ProcessResult:
        return await self._manager.kill(session_id)


class ToolConsoleState:
    def __init__(self, services: RuntimeServices | None) -> None:
        self._services = services

    async def mutate(
        self,
        section: str,
        default: object,
        callback: Callable[[object], object],
    ) -> object:
        if self._services is None:
            raise RuntimeError("Console state runtime is unavailable")
        return await asyncio.to_thread(
            self._services.mutate_console_section,
            section,
            default,
            callback,
        )


class ToolBrowser:
    def __init__(self, scope: ToolExecutionScope, manager: BrowserManager) -> None:
        self._scope = scope
        self._manager = manager

    async def run(
        self, command: str, arguments: tuple[str, ...] = ()
    ) -> dict[str, object]:
        return await self._manager.run(self._scope.session_id, command, arguments)


class ToolAgents:
    def __init__(
        self, scope: ToolExecutionScope, services: RuntimeServices | None
    ) -> None:
        self._scope = scope
        self._services = services

    async def run_child(
        self,
        prompt: str,
        *,
        system_prompt: str,
        child_key: str,
    ) -> str:
        if self._services is None:
            raise RuntimeError("Agent runtime is unavailable")
        return await asyncio.to_thread(
            self._services.run_child,
            self._scope.session_id,
            prompt,
            system_prompt,
            child_key,
            enabled_tools=self._scope.enabled_tools,
            run_id=self._scope.run_id,
            agent_id=self._scope.agent_id,
            excluded_tool=self._scope.tool_name,
            hook_ids=self._scope.hook_ids,
        )


class ToolInteraction:
    def __init__(self, services: RuntimeServices | None) -> None:
        self._services = services

    async def request(self, payload: Mapping[str, object]) -> object:
        if self._services is None:
            raise RuntimeError("Interaction runtime is unavailable")
        return self._services.interrupt(dict(payload))


class ToolCalls:
    def __init__(
        self, scope: ToolExecutionScope, services: RuntimeServices | None
    ) -> None:
        self._scope = scope
        self._services = services

    @property
    def session_id(self) -> str:
        return self._scope.session_id

    @property
    def workspace(self) -> Path:
        return self._scope.workspace

    @property
    def enabled_tools(self) -> tuple[str, ...]:
        return self._scope.enabled_tools

    def available_tools(self) -> list[object]:
        if self._services is None:
            return []
        return [
            tool
            for tool in self._services.available_tools
            if self.can_invoke(str(getattr(tool, "name", "") or ""))
        ]

    def can_invoke(self, name: str) -> bool:
        return bool(
            name and name != self._scope.tool_name and name in self._scope.enabled_tools
        )

    def invoke_sync(self, name: str, arguments: Mapping[str, object]) -> object:
        if self._services is None:
            raise RuntimeError("Nested Tool runtime is unavailable")
        if not self.can_invoke(name):
            raise ToolPermissionError(f"Tool {name} is not available in this scope")
        tool = next(
            (item for item in self._services.available_tools if item.name == name),
            None,
        )
        if tool is None:
            raise KeyError(f"unknown Tool: {name}")
        return asyncio.run(
            tool.ainvoke(
                dict(arguments),
                config={
                    "configurable": {
                        "thread_id": self._scope.session_id,
                        "product_session_id": self._scope.session_id,
                        "session_id": self._scope.session_id,
                        "run_id": self._scope.run_id,
                        "agent_id": self._scope.agent_id,
                        "hook_ids": self._scope.hook_ids,
                    }
                },
            )
        )


def _call_hook_snapshot(call: ToolCallContext) -> tuple[str, ...]:
    raw = call.metadata.get("hook_ids", ())
    if isinstance(raw, (str, bytes, bytearray)):
        return (str(raw),)
    try:
        return tuple(str(item) for item in raw)
    except TypeError:
        return ()


_STATE_COMPONENT = re.compile(r"[a-z][a-z0-9_.-]*")
_STATE_LOCK = threading.Lock()


class ToolState:
    def __init__(self, root: Path, permissions: frozenset[str]) -> None:
        self._root = Path(root).resolve()
        self._permissions = permissions

    async def read(self, namespace: str, key: str) -> list[object]:
        self._require(namespace, "read")
        path = self._path(namespace, key)
        return await asyncio.to_thread(self._read, path)

    async def append(self, namespace: str, key: str, value: object) -> None:
        path = self._path(namespace, key)
        await asyncio.to_thread(self._append, path, value)

    async def mutate(
        self,
        namespace: str,
        key: str,
        callback: Callable[[list[object]], object],
    ) -> object:
        path = self._path(namespace, key)
        return await asyncio.to_thread(self._mutate, path, callback)

    def _require(self, namespace: str, operation: str) -> None:
        del namespace, operation

    def _path(self, namespace: str, key: str) -> Path:
        if _STATE_COMPONENT.fullmatch(namespace) is None:
            raise ValueError("invalid Tool state namespace")
        if _STATE_COMPONENT.fullmatch(key) is None:
            raise ValueError("invalid Tool state key")
        return self._root / namespace / f"{key}.json"

    @staticmethod
    def _read(path: Path) -> list[object]:
        with _STATE_LOCK:
            if not path.is_file() or path.is_symlink():
                return []
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return []
            return value if isinstance(value, list) else []

    @classmethod
    def _append(cls, path: Path, value: object) -> None:
        with _STATE_LOCK:
            if path.is_file() and not path.is_symlink():
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    current = []
            else:
                current = []
            if not isinstance(current, list):
                current = []
            current.append(value)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(current, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)

    @classmethod
    def _mutate(
        cls,
        path: Path,
        callback: Callable[[list[object]], object],
    ) -> object:
        with _STATE_LOCK:
            current = cls._read_unlocked(path)
            result = callback(current)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(current, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
            return result

    @staticmethod
    def _read_unlocked(path: Path) -> list[object]:
        if not path.is_file() or path.is_symlink():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []


__all__ = [
    "ToolExecutionScope",
    "ToolPermissionError",
    "ToolRuntime",
    "ToolRuntimeFactory",
    "ToolRuntimeServices",
    "ToolState",
]
