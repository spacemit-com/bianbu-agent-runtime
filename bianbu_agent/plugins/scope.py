"""Generation-local ownership scope for Plugin effects and contributions."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, MutableMapping
from types import MappingProxyType
from typing import Any

from bianbu_agent.plugins.contributions import (
    HookContribution,
    MCPRuntimeContribution,
    PluginContributions,
    SkillContribution,
    ToolContribution,
)


Disposer = Callable[[], object | Awaitable[object]]
EventHandler = Callable[..., object | Awaitable[object]]


class PluginScope:
    """Own everything registered by one Plugin inside one generation."""

    def __init__(
        self,
        plugin_id: str,
        *,
        shared_services: MutableMapping[str, object] | None = None,
        shared_service_owners: MutableMapping[str, str] | None = None,
        allowed_service_owners: frozenset[str] = frozenset(),
        base_services: dict[str, object] | None = None,
        shared_events: MutableMapping[str, list[EventHandler]] | None = None,
    ) -> None:
        self.plugin_id = str(plugin_id)
        self._services = shared_services if shared_services is not None else {}
        self._service_owners = (
            shared_service_owners if shared_service_owners is not None else {}
        )
        self._allowed_service_owners = allowed_service_owners
        self._base_services = dict(base_services or {})
        self._events = shared_events if shared_events is not None else {}
        self._tools: list[ToolContribution] = []
        self._skills: list[SkillContribution] = []
        self._hooks: list[HookContribution] = []
        self._mcp_runtime: MCPRuntimeContribution | None = None
        self._disposers: list[Disposer] = []
        self._sealed = False
        self._disposed = False

    def _ensure_mutable(self) -> None:
        if self._sealed:
            raise RuntimeError(f"plugin scope {self.plugin_id} is sealed")
        if self._disposed:
            raise RuntimeError(f"plugin scope {self.plugin_id} is disposed")

    def _register(self, collection: list[Any], value: Any) -> Callable[[], None]:
        self._ensure_mutable()
        if value.plugin_id != self.plugin_id:
            raise ValueError(
                f"contribution owner {value.plugin_id} does not match scope {self.plugin_id}"
            )
        collection.append(value)
        active = True

        def dispose() -> None:
            nonlocal active
            if active:
                active = False
                try:
                    collection.remove(value)
                except ValueError:
                    pass

        self._disposers.append(dispose)
        return dispose

    def register_tool(self, contribution: ToolContribution) -> Callable[[], None]:
        return self._register(self._tools, contribution)

    def register_skill(self, contribution: SkillContribution) -> Callable[[], None]:
        return self._register(self._skills, contribution)

    def register_hook(self, contribution: HookContribution) -> Callable[[], None]:
        return self._register(self._hooks, contribution)

    def register_mcp_runtime(
        self, contribution: MCPRuntimeContribution
    ) -> Callable[[], None]:
        self._ensure_mutable()
        if self._mcp_runtime is not None:
            raise ValueError(
                f"plugin {self.plugin_id} already registered an MCP runtime"
            )
        if contribution.plugin_id != self.plugin_id:
            raise ValueError(
                f"contribution owner {contribution.plugin_id} does not match scope {self.plugin_id}"
            )
        self._mcp_runtime = contribution
        active = True

        def dispose() -> None:
            nonlocal active
            if active:
                active = False
                self._mcp_runtime = None

        self._disposers.append(dispose)
        return dispose

    def provide(self, name: str, service: object) -> Callable[[], None]:
        self._ensure_mutable()
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("service name must not be empty")
        if normalized in self._services or normalized in self._base_services:
            raise ValueError(f"service already provided: {normalized}")
        self._services[normalized] = service
        self._service_owners[normalized] = self.plugin_id
        active = True

        def dispose() -> None:
            nonlocal active
            if active:
                active = False
                if self._services.get(normalized) is service:
                    self._services.pop(normalized, None)
                    self._service_owners.pop(normalized, None)

        self._disposers.append(dispose)
        return dispose

    def require(self, name: str) -> object:
        normalized = str(name).strip()
        if normalized in self._base_services:
            return self._base_services[normalized]
        if normalized not in self._services:
            raise KeyError(f"required service is missing: {normalized}")
        owner = self._service_owners.get(normalized)
        if owner != self.plugin_id and owner not in self._allowed_service_owners:
            raise PermissionError(
                f"service {normalized} is owned by undeclared dependency {owner or 'unknown'}"
            )
        return self._services[normalized]

    @property
    def service_view(self):
        visible = {
            name: service
            for name, service in self._services.items()
            if self._service_owners.get(name) == self.plugin_id
            or self._service_owners.get(name) in self._allowed_service_owners
        }
        return MappingProxyType({**self._base_services, **visible})

    def on(self, event: str, handler: EventHandler) -> Callable[[], None]:
        self._ensure_mutable()
        name = str(event).strip()
        if not name:
            raise ValueError("event name must not be empty")
        if not callable(handler):
            raise TypeError("event handler must be callable")
        handlers = self._events.setdefault(name, [])
        handlers.append(handler)
        active = True

        def dispose() -> None:
            nonlocal active
            if not active:
                return
            active = False
            try:
                handlers.remove(handler)
            except ValueError:
                pass
            if not handlers and self._events.get(name) is handlers:
                self._events.pop(name, None)

        self._disposers.append(dispose)
        return dispose

    async def emit(self, event: str, *args: object, **kwargs: object) -> tuple[object, ...]:
        results: list[object] = []
        for handler in tuple(self._events.get(str(event).strip(), ())):
            result = handler(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            results.append(result)
        return tuple(results)

    def effect(self, setup: Callable[[], Disposer | None]) -> Disposer:
        self._ensure_mutable()
        disposer = setup()
        if disposer is None:
            disposer = lambda: None
        if not callable(disposer):
            raise TypeError("effect setup must return a disposer or None")
        active = True

        async def dispose_once() -> None:
            nonlocal active
            if not active:
                return
            active = False
            result = disposer()
            if inspect.isawaitable(result):
                await result

        self._disposers.append(dispose_once)
        return dispose_once

    def seal(self) -> None:
        self._sealed = True

    def snapshot(self) -> PluginContributions:
        return PluginContributions(
            hooks=tuple(self._hooks),
            tools=tuple(self._tools),
            skills=tuple(self._skills),
            mcp_runtime=self._mcp_runtime,
        )

    async def dispose(self) -> tuple[BaseException, ...]:
        if self._disposed:
            return ()
        self._disposed = True
        self._sealed = True
        failures: list[BaseException] = []
        for disposer in reversed(self._disposers):
            try:
                result = disposer()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                failures.append(exc)
        self._disposers.clear()
        self._tools.clear()
        self._skills.clear()
        self._hooks.clear()
        self._mcp_runtime = None
        return tuple(failures)


__all__ = ["PluginScope"]
