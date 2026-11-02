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
        title_store: object | None = None,
    ) -> None:
        self._store = store
        self._plugins = plugin_manager
        self._run_factory = run_factory
        # Titles are log-only state.  The transcript store may be a different
        # object from the Session metadata store (the control plane keeps
        # metadata in JSON/SQLite and events in the Session log), so title
        # reads and writes go to the store that owns the log.
        self._title_store = title_store if title_store is not None else store
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
        return await self._handle_with_title(record)

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
        # Admission is not a read: folding here would make creating a Session
        # open its transcript, defeating the zero-I/O listing contract.
        return self._handle(record)

    async def list(self) -> list[SessionHandle]:
        """List Sessions without opening any transcript.

        A listing is a projection read.  The folded event cache answers when
        this process already touched the Session; otherwise the stored record
        is used and the API overlays the persisted projection row's title.
        """

        folded = getattr(self._title_store, "folded_title", None)
        handles: list[SessionHandle] = []
        for record in await self._store.list():
            value = folded(record.id) if callable(folded) else None
            handles.append(self._handle(
                record, title=None if value is None else str(value),
            ))
        return handles

    async def delete(self, session_id: str) -> bool:
        deleted = await self._store.delete(session_id)
        if deleted:
            self._changes.notify()
        return deleted

    async def rename(self, session_id: str, title: str) -> SessionHandle:
        """Accept an explicit user title and pin it as the latest revision."""

        normalized = normalize_session_title(title)
        if not normalized:
            raise ValueError("session title must not be empty")
        record = await self._store.get(session_id)
        if record is None:
            raise KeyError(str(session_id).strip())
        await self.set_title(session_id, normalized, source_kind="user")
        self._changes.notify()
        refreshed = await self._store.get(session_id)
        return await self._handle_with_title(refreshed or record)

    async def set_title(
        self,
        session_id: str,
        title: str,
        *,
        message_seqs: tuple[int, ...] = (),
        source_kind: str = "provider",
        durable: bool = False,
        request_id: str = "",
    ) -> str:
        """Append one log-only title revision; the latest revision wins.

        The title never becomes a Run: it creates no turn, claims no
        concurrency slot, and triggers no Hooks.
        """

        normalized = normalize_session_title(title)
        if not normalized:
            raise ValueError("session title must not be empty")
        append = getattr(self._title_store, "append_title", None)
        if callable(append):
            await append(
                session_id,
                normalized,
                message_seqs=tuple(message_seqs),
                source_kind=str(source_kind),
                durable=durable,
                request_id=request_id,
            )
        else:
            await self._store.update_title(session_id, normalized)
        self._changes.notify()
        return normalized

    async def set_title_with_request(
        self,
        session_id: str,
        title: str,
        *,
        request: Mapping[str, object] | None = None,
        message_seqs: tuple[int, ...] = (),
        source_kind: str = "fallback",
        request_id: str = "",
    ) -> str:
        """Write the fallback title and its model request in one log batch."""

        normalized = normalize_session_title(title)
        if not normalized:
            raise ValueError("session title must not be empty")
        append = getattr(self._title_store, "append_title_with_request", None)
        if callable(append) and request is not None:
            await append(
                session_id,
                normalized,
                request=request,
                message_seqs=tuple(message_seqs),
                source_kind=str(source_kind),
                request_id=request_id,
            )
        else:
            return await self.set_title(
                session_id,
                normalized,
                message_seqs=message_seqs,
                source_kind=source_kind,
                durable=True,
                request_id=request_id,
            )
        self._changes.notify()
        return normalized

    async def title(self, session_id: str) -> str:
        """Latest accepted title, preferring the log over stored metadata."""

        value = await self._read_log_title(str(session_id))
        if value is not None:
            return value
        record = await self._store.get(session_id)
        return "" if record is None else str(record.title)

    async def _read_log_title(self, session_id: str) -> str | None:
        reader = getattr(self._title_store, "read_title", None)
        if callable(reader):
            return await reader(session_id)
        folded = getattr(self._title_store, "folded_title", None)
        return folded(session_id) if callable(folded) else None

    async def _handle_with_title(self, record: StoredSession) -> SessionHandle:
        value = await self._read_log_title(record.id)
        if value is None or str(value) == record.title:
            return self._handle(record)
        return self._handle(record, title=str(value))

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

    def _handle(self, record: StoredSession, *, title: str | None = None) -> SessionHandle:
        return SessionHandle(
            id=record.id,
            title=record.title if title is None else title,
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
