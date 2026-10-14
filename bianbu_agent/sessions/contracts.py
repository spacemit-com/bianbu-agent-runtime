"""Stable Session contracts shared by managers and stores."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from bianbu_agent.sessions.models import StoredSession
from bianbu_agent.sessions.inbox import SessionInboxMessage


class SessionStore(Protocol):
    async def create(
        self,
        session_id: str,
        *,
        agent_id: str,
        title: str = "",
        metadata: Mapping[str, object] | None = None,
    ) -> StoredSession: ...
    async def backfill_agent_id(self, agent_id: str) -> None: ...
    async def get(self, session_id: str) -> StoredSession | None: ...
    async def list(self) -> list[StoredSession]: ...
    async def delete(self, session_id: str) -> bool: ...
    async def update_title(self, session_id: str, title: str) -> StoredSession: ...
    async def rotate_thread(
        self,
        session_id: str,
        *,
        expected: str,
        replacement: str,
    ) -> bool: ...


RunFactory = Callable[..., Awaitable[object]]


class SessionAgentMismatchError(ValueError):
    def __init__(self, session_id: str, expected: str, supplied: str) -> None:
        self.session_id = session_id
        self.expected = expected
        self.supplied = supplied
        super().__init__(
            f"session '{session_id}' is bound to agent '{expected}', "
            f"not '{supplied}'"
        )


@dataclass(frozen=True, slots=True)
class SessionHandle:
    id: str
    title: str
    agent_id: str
    metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: float = 0
    updated_at: float = 0
    _plugin_manager: object = field(repr=False, compare=False, default=None)
    _run_factory: RunFactory | None = field(repr=False, compare=False, default=None)
    _thread_id: str = field(repr=False, compare=False, default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    async def restart(self):
        if self._plugin_manager is None:
            raise RuntimeError("Session has no PluginManager")
        await self._plugin_manager.reload_installed()
        return self

    async def run(
        self,
        prompt: str | Sequence[SessionInboxMessage],
        *,
        agent_id: str | None = None,
        model: object | str | None = None,
        metadata: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
        request_id: str | None = None,
    ):
        if self._run_factory is None:
            raise RuntimeError("Session has no RunManager")
        if model is not None and (
            not isinstance(model, str) or bool(model.strip())
        ):
            raise ValueError("per-Run model override is not allowed")
        selected_agent = self.agent_id if agent_id is None else str(agent_id).strip()
        if selected_agent != self.agent_id:
            raise SessionAgentMismatchError(self.id, self.agent_id, selected_agent)
        options = {"thread_id": self._thread_id, "agent_id": self.agent_id}
        if metadata is not None:
            options["metadata"] = metadata
        if idempotency_key is not None:
            options["idempotency_key"] = idempotency_key
        if idempotency_fingerprint is not None:
            options["idempotency_fingerprint"] = idempotency_fingerprint
        if request_id is not None:
            options["request_id"] = request_id
        return await self._run_factory(self.id, prompt, **options)


__all__ = [
    "RunFactory",
    "SessionAgentMismatchError",
    "SessionHandle",
    "SessionStore",
]
