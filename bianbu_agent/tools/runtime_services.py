from __future__ import annotations

from copy import deepcopy
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.types import interrupt

from bianbu_agent.sessions.log_store import transcript_run_records
from bianbu_agent.sessions.projection import read_projection, record_from
from bianbu_agent.tools.state import ConsoleState


class _GenerationRuntimeServices:
    """Delegate durable state while freezing one Generation's runtime view."""

    __slots__ = (
        "_capabilities",
        "_generation",
        "_scope_resolver",
        "_services",
        "available_tools",
        "capture_callback",
        "model",
        "model_provider_id",
    )

    def __init__(
        self,
        services: RuntimeServices,
        capabilities: Mapping[str, object | None],
        *,
        scope_resolver: object | None = None,
        model: BaseChatModel | str | None = None,
        model_provider_id: str | None = None,
        capture_callback: object | None = None,
    ) -> None:
        self._services = services
        self._capabilities = MappingProxyType(dict(capabilities))
        self._scope_resolver = scope_resolver
        self._generation: object | None = None
        self.available_tools: list[BaseTool] = []
        self.model = model
        self.model_provider_id = str(model_provider_id or "").strip()
        self.capture_callback = capture_callback

    def activate(self, generation: object, available_tools: list[BaseTool]) -> None:
        self._generation = generation
        self.available_tools = available_tools

    def deactivate(self) -> None:
        self._generation = None
        self.available_tools = []

    async def resolve_agent_capabilities(
        self,
        agent_id: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...] | None]:
        if self._generation is None or not callable(self._scope_resolver):
            raise KeyError(agent_id)
        return await self._scope_resolver(agent_id, self._generation)

    def resolve_model_capability(self, name: str) -> object | None:
        capability = str(name).strip().lower()
        if capability in self._capabilities:
            return self._capabilities[capability]
        return self._services.resolve_model_capability(capability)

    def tool_unavailable_reason(self, name: str) -> str | None:
        return self._services.tool_unavailable_reason(name)

    def run_child(
        self,
        session_id: str,
        prompt: str,
        system_prompt: str,
        child_key: str,
        *,
        enabled_tools: tuple[str, ...] | None = None,
        run_id: str = "unknown-run",
        agent_id: str = "default-agent",
        excluded_tool: str = "",
        hook_ids: tuple[str, ...] = (),
    ) -> str:
        return self._services.run_child_with_snapshot(
            self.model,
            self.available_tools,
            session_id,
            prompt,
            system_prompt,
            child_key,
            enabled_tools=enabled_tools,
            run_id=run_id,
            agent_id=agent_id,
            excluded_tool=excluded_tool,
            hook_ids=hook_ids,
            model_provider_id=self.model_provider_id,
            capture_callback=self.capture_callback,
        )

    def __getattr__(self, name: str) -> object:
        return getattr(self._services, name)


class RuntimeServices:
    """Host services required by tools normally intercepted by their own loop."""

    def __init__(
        self,
        *,
        sessions_root: Path,
        workspace: Path,
        model: BaseChatModel | str | None,
        console_state: ConsoleState | None = None,
        runtime_home: Path | None = None,
        model_resolver: object | None = None,
        config: object | None = None,
        model_provider_id: str | None = None,
        capture_callback: object | None = None,
    ) -> None:
        self.sessions_root = Path(sessions_root)
        self.workspace = workspace
        self.model = model
        self.model_provider_id = str(model_provider_id or "").strip()
        self.capture_callback = capture_callback
        self.model_resolver = model_resolver
        self.config = config
        self.runtime_home = Path(
            runtime_home or Path(sessions_root).parent / "runtime"
        )
        self.available_tools: list[BaseTool] = []
        self._console_state = console_state or ConsoleState(
            self.runtime_home.parent / "console-state.json"
        )
        self._console_state_lock = threading.RLock()

    def resolve_model_capability(self, name: str) -> object | None:
        resolver = self.model_resolver
        resolve = getattr(resolver, "resolve_capability", None)
        if not callable(resolve):
            return None
        return resolve(name)

    def for_tool_generation(self, name: str) -> RuntimeServices:
        del name
        return self

    def tool_unavailable_reason(self, name: str) -> str | None:
        del name
        return None

    def mutate_console_section(
        self,
        name: str,
        default: object,
        callback: Any,
    ) -> object:
        """Run one atomic read-modify-write operation on shared console state."""

        with self._console_state_lock:
            value = deepcopy(self._console_state.section(name, default))
            result = callback(value)
            self._console_state.set_section(name, value)
            return result

    @staticmethod
    def interrupt(payload: dict[str, object]) -> object:
        return interrupt(payload)

    def records(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return (
            session_records(self.sessions_root),
            transcript_run_records(self.sessions_root),
        )

    def run_child(
        self,
        session_id: str,
        prompt: str,
        system_prompt: str,
        child_key: str,
        *,
        enabled_tools: tuple[str, ...] | None = None,
        run_id: str = "unknown-run",
        agent_id: str = "default-agent",
        excluded_tool: str = "",
        hook_ids: tuple[str, ...] = (),
    ) -> str:
        return self.run_child_with_snapshot(
            self.model,
            self.available_tools,
            session_id,
            prompt,
            system_prompt,
            child_key,
            enabled_tools=enabled_tools,
            run_id=run_id,
            agent_id=agent_id,
            excluded_tool=excluded_tool,
            hook_ids=hook_ids,
        )

    def run_child_with_snapshot(
        self,
        model: BaseChatModel | str | None,
        available_tools: list[BaseTool],
        session_id: str,
        prompt: str,
        system_prompt: str,
        child_key: str,
        *,
        enabled_tools: tuple[str, ...] | None = None,
        run_id: str = "unknown-run",
        agent_id: str = "default-agent",
        excluded_tool: str = "",
        hook_ids: tuple[str, ...] = (),
        model_provider_id: str | None = None,
        capture_callback: object | None = None,
    ) -> str:
        if model is None:
            raise RuntimeError("No model is configured for child-Agent execution.")
        from bianbu_agent.engines.langgraph import build_langgraph_agent
        from bianbu_agent.context import CompressionSettingsProvider

        allowed_tools = None if enabled_tools is None else set(enabled_tools)

        def qualified_tool_name(tool: BaseTool) -> str:
            metadata = getattr(tool, "metadata", None)
            if isinstance(metadata, Mapping):
                qualified = str(metadata.get("sdk_qualified_name") or "").strip()
                if qualified:
                    return qualified
            return tool.name

        child_tools = [
            tool
            for tool in available_tools
            if (
                (
                    allowed_tools is None
                    or qualified_tool_name(tool) in allowed_tools
                )
                and tool.name != excluded_tool
                and not (
                    isinstance(getattr(tool, "metadata", None), Mapping)
                    and tool.metadata.get("requires_approval") is True
                )
            )
        ]
        child = build_langgraph_agent(
            model=model,
            tools=child_tools,
            system_prompt=system_prompt,
            context_settings=CompressionSettingsProvider(),
        )
        child_id = f"{session_id}:child:{child_key}:{uuid.uuid4().hex[:8]}"
        selected_provider_id = str(
            self.model_provider_id
            if model_provider_id is None
            else model_provider_id
        ).strip()
        selected_callback = (
            self.capture_callback if capture_callback is None else capture_callback
        )
        configurable = {
            "thread_id": child_id,
            "product_session_id": session_id,
            "session_id": session_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "hook_ids": tuple(hook_ids),
        }
        config: dict[str, object] = {
            "configurable": configurable,
            "recursion_limit": float("inf"),
        }
        if selected_provider_id:
            config["metadata"] = {
                "provider_id": selected_provider_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "run_id": run_id,
            }
            if selected_callback is not None:
                config["callbacks"] = [selected_callback]
        output = child.invoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=config,
        )
        return _message_text(output["messages"][-1].content)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        )
    return str(content)


def session_records(root: Path) -> list[dict[str, Any]]:
    """The Session identities recorded in one Sessions directory.

    Identity lives in each Session's own ``projection.json`` beside its
    transcript, so this is a scan of small files, never of the event logs.
    """

    records: list[dict[str, Any]] = []
    try:
        paths = sorted(
            Path(root).glob("*/projection.json"),
            key=lambda path: path.parent.name.casefold(),
        )
    except OSError:
        return records
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        payload = record_from(read_projection(path))
        if payload is None or payload.get("id") != path.parent.name:
            continue
        records.append(payload)
    return records
