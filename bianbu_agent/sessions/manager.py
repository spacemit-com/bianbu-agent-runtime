"""Session runtime contracts and file-backed persistence."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping

from bianbu_agent.sessions.contracts import (
    RunFactory,
    SessionAgentMismatchError,
    SessionHandle,
    SessionStore,
)
from bianbu_agent.sessions.models import StoredSession
from bianbu_agent._internal.change_signal import AsyncRevisionSignal


_TITLE_MARKUP = re.compile(r"[`*_~#>\[\]{}]")
_TITLE_QUOTES = re.compile(r"^[\"'“”‘’]+|[\"'“”‘’]+$")


def normalize_session_title(value: object) -> str:
    """Keep generated and user-edited titles plain and readable."""
    title = _TITLE_MARKUP.sub("", str(value or ""))
    title = _TITLE_QUOTES.sub("", title)
    return " ".join(title.split()).strip()[:120]


class SessionManager:
    def __init__(
        self,
        *,
        store: SessionStore,
        plugin_manager: object,
        run_factory: RunFactory | None = None,
    ) -> None:
        self._store = store
        self._plugins = plugin_manager
        self._run_factory = run_factory
        self._changes = AsyncRevisionSignal()

    @property
    def change_revision(self) -> int:
        return self._changes.revision

    async def wait_for_change(self, after: int) -> int:
        return await self._changes.wait(after)

    async def create(
        self,
        session_id: str | None = None,
        *,
        agent_id: str | None = None,
        title: str = "",
        metadata: Mapping[str, object] | None = None,
    ) -> SessionHandle:
        selected_agent = self._normalize_agent_id(agent_id)
        record = await self._store.create(
            session_id or str(uuid.uuid4()),
            agent_id=selected_agent,
            title=normalize_session_title(title),
            metadata=metadata,
        )
        self._assert_agent(record, selected_agent)
        self._changes.notify()
        return self._handle(record)

    async def get(self, session_id: str) -> SessionHandle:
        record = await self._store.get(session_id)
        if record is None:
            raise KeyError(str(session_id).strip())
        return self._handle(record)

    async def get_or_create(
        self,
        *,
        session_id: str,
        agent_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> SessionHandle:
        record = await self._store.get(session_id)
        if record is None:
            selected_agent = self._normalize_agent_id(agent_id)
            record = await self._store.create(
                session_id,
                agent_id=selected_agent,
                metadata=metadata,
            )
            self._assert_agent(record, selected_agent)
            self._changes.notify()
        elif agent_id is not None:
            self._assert_agent(record, self._normalize_agent_id(agent_id))
        return self._handle(record)

    async def list(self) -> list[SessionHandle]:
        return [self._handle(record) for record in await self._store.list()]

    async def delete(self, session_id: str) -> bool:
        deleted = await self._store.delete(session_id)
        if deleted:
            self._changes.notify()
        return deleted

    async def rename(self, session_id: str, title: str) -> SessionHandle:
        normalized = normalize_session_title(title)
        if not normalized:
            raise ValueError("session title must not be empty")
        record = await self._store.update_title(session_id, normalized)
        self._changes.notify()
        return self._handle(record)

    async def clear_context(self, session_id: str) -> SessionHandle:
        record = await self._store.get(session_id)
        if record is None:
            raise KeyError(str(session_id).strip())
        replacement = str(uuid.uuid4())
        changed = await self._store.rotate_thread(
            record.id,
            expected=record.active_thread_id,
            replacement=replacement,
        )
        if not changed:
            raise RuntimeError("session context changed concurrently")
        refreshed = await self._store.get(record.id)
        if refreshed is None:
            raise RuntimeError("session disappeared after clearing context")
        self._changes.notify()
        return self._handle(refreshed)

    def _handle(self, record: StoredSession) -> SessionHandle:
        return SessionHandle(
            id=record.id,
            title=record.title,
            agent_id=record.agent_id,
            metadata=record.metadata,
            created_at=record.created_at,
            updated_at=record.updated_at,
            _thread_id=record.active_thread_id,
            _plugin_manager=self._plugins,
            _run_factory=self._run_factory,
        )

    def _normalize_agent_id(self, agent_id: str | None) -> str:
        normalized = str(agent_id or "").strip()
        if not normalized:
            raise ValueError("agent_id must not be empty")
        return normalized

    @staticmethod
    def _assert_agent(record: StoredSession, selected_agent: str) -> None:
        if record.agent_id != selected_agent:
            raise SessionAgentMismatchError(
                record.id, record.agent_id, selected_agent
            )


__all__ = [
    "SessionManager",
]
