from __future__ import annotations

import asyncio
from dataclasses import replace
import shutil
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

from bianbu_agent.sessions.models import StoredSession
from bianbu_agent.sessions.projection import (
    RECORD_VERSION,
    SessionProjections,
    projection_path,
    read_projection,
    record_from,
    session_id_of,
)


class JSONSessionStore:
    """Session identity stored inside each Session's ``projection.json``.

    A Session directory owns exactly two files, and this store owns the
    ``record`` half of the projection: the id, title, agent, metadata, and
    active thread.  The folded list row the Run log store stages is preserved
    on every write, so the two halves share one file without clobbering each
    other.
    """

    def __init__(self, root: Path) -> None:
        candidate = Path(root).expanduser()
        if candidate.is_symlink():
            raise ValueError(f"session directory must not be a symbolic link: {candidate}")
        self.root = candidate.resolve()
        self._projections = SessionProjections(self.root)
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError(f"unsafe session directory: {self.root}")

    async def close(self) -> None:
        return None

    async def create(
        self,
        session_id: str,
        *,
        agent_id: str,
        title: str = "",
        metadata: Mapping[str, object] | None = None,
    ) -> StoredSession:
        now = time.time()
        record = StoredSession(
            id=session_id,
            title=str(title).strip(),
            agent_id=str(agent_id).strip(),
            metadata=dict(metadata or {}),
            active_thread_id=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            existing = self._read(session_id)
            if existing is not None:
                return existing
            self._projections.stage_record(session_id, _encode(record))
            await asyncio.to_thread(self._projections.flush)
        return record

    async def backfill_agent_id(self, agent_id: str) -> None:
        normalized = str(agent_id).strip()
        if not normalized:
            raise ValueError("agent_id must not be empty")
        async with self._lock:
            for record in self._list():
                if record.agent_id:
                    continue
                self._stage(record.id, _encode(replace(record, agent_id=normalized)))
            await asyncio.to_thread(self._projections.flush)

    async def get(self, session_id: str) -> StoredSession | None:
        async with self._lock:
            return self._read(session_id)

    async def list(self) -> list[StoredSession]:
        async with self._lock:
            records = await asyncio.to_thread(self._list)
        return sorted(records, key=lambda item: (item.created_at, item.id))

    async def delete(self, session_id: str) -> bool:
        owner = session_id_of(session_id)
        async with self._lock:
            if self._read(owner) is None:
                return False
            # The Session is its directory: identity, transcript, and folded
            # row leave together so nothing can resurrect half a Session.
            directory = self.root / owner
            if directory.exists():
                await asyncio.to_thread(shutil.rmtree, directory, True)
            self._projections.forget(owner)
            return True

    async def update_title(self, session_id: str, title: str) -> StoredSession:
        owner = session_id_of(session_id)
        async with self._lock:
            record = self._read(owner)
            if record is None:
                raise KeyError(owner)
            updated = replace(
                record, title=str(title).strip(), updated_at=time.time()
            )
            self._stage(owner, _encode(updated))
            await asyncio.to_thread(self._projections.flush)
            return updated

    async def rotate_thread(
        self, session_id: str, *, expected: str, replacement: str
    ) -> bool:
        if not str(replacement).strip():
            raise ValueError("replacement thread must not be empty")
        owner = session_id_of(session_id)
        async with self._lock:
            record = self._read(owner)
            if record is None or record.active_thread_id != expected:
                return False
            self._stage(
                owner,
                _encode(
                    replace(
                        record,
                        active_thread_id=str(replacement).strip(),
                        updated_at=time.time(),
                    )
                ),
            )
            await asyncio.to_thread(self._projections.flush)
            return True

    def _read(self, session_id: object) -> StoredSession | None:
        try:
            owner = session_id_of(session_id)
        except ValueError:
            return None
        document = read_projection(projection_path(self.root, owner))
        payload = record_from(document)
        if payload is None or str(payload.get("id") or "") != owner:
            return None
        return _decode(payload)

    def _list(self) -> list[StoredSession]:
        self._projections.load()
        records: list[StoredSession] = []
        for session_id in self._projections.documents():
            payload = record_from(self._projections.documents()[session_id])
            if payload is None or str(payload.get("id") or "") != session_id:
                continue
            records.append(_decode(payload))
        return records

    def _stage(self, session_id: str, payload: Mapping[str, object]) -> None:
        self._projections.stage_record(session_id, payload)


def _encode(record: StoredSession) -> dict[str, object]:
    return {
        "id": record.id,
        "title": record.title,
        "agent_id": record.agent_id,
        "metadata": dict(record.metadata),
        "active_thread_id": record.active_thread_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _decode(payload: Mapping[str, object]) -> StoredSession:
    return StoredSession(
        id=str(payload["id"]),
        title=str(payload.get("title", "")),
        agent_id=str(payload.get("agent_id", "")),
        metadata=payload.get("metadata", {}),
        active_thread_id=str(payload.get("active_thread_id", "")),
        created_at=float(payload.get("created_at", 0)),
        updated_at=float(payload.get("updated_at", 0)),
    )


__all__ = ["JSONSessionStore", "RECORD_VERSION"]
