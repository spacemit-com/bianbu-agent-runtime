from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

import aiosqlite
from bianbu_agent.sessions.models import StoredSession


class SQLiteSessionStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path).expanduser().resolve()
        self._connection: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._connection is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._path)
        connection.row_factory = aiosqlite.Row
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sdk_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                active_thread_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        await connection.commit()
        self._connection = connection

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()

    def _opened(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Session store is not open")
        return self._connection

    async def create(
        self,
        session_id: str,
        *,
        agent_id: str,
        title: str = "",
        metadata: Mapping[str, object] | None = None,
    ) -> StoredSession:
        normalized = str(session_id).strip()
        if not normalized:
            raise ValueError("session_id must not be empty")
        normalized_agent = str(agent_id).strip()
        if not normalized_agent:
            raise ValueError("agent_id must not be empty")
        now = time.time()
        await self._opened().execute(
            """
            INSERT OR IGNORE INTO sdk_sessions(
                id, title, agent_id, metadata_json, active_thread_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized,
                str(title).strip(),
                normalized_agent,
                json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True),
                str(uuid.uuid4()),
                now,
                now,
            ),
        )
        await self._opened().commit()
        result = await self.get(normalized)
        if result is None:
            raise RuntimeError("Session insert did not produce a record")
        return result

    async def get(self, session_id: str) -> StoredSession | None:
        cursor = await self._opened().execute(
            "SELECT * FROM sdk_sessions WHERE id = ?", (str(session_id).strip(),)
        )
        row = await cursor.fetchone()
        return None if row is None else _record(row)

    async def list(self) -> list[StoredSession]:
        cursor = await self._opened().execute(
            "SELECT * FROM sdk_sessions ORDER BY created_at, id"
        )
        return [_record(row) for row in await cursor.fetchall()]

    async def delete(self, session_id: str) -> bool:
        cursor = await self._opened().execute(
            "DELETE FROM sdk_sessions WHERE id = ?", (str(session_id).strip(),)
        )
        await self._opened().commit()
        return cursor.rowcount == 1

    async def update_title(self, session_id: str, title: str) -> StoredSession:
        cursor = await self._opened().execute(
            "UPDATE sdk_sessions SET title = ?, updated_at = ? WHERE id = ?",
            (str(title).strip(), time.time(), str(session_id).strip()),
        )
        await self._opened().commit()
        if cursor.rowcount != 1:
            raise KeyError(str(session_id).strip())
        result = await self.get(session_id)
        if result is None:
            raise RuntimeError("updated session could not be reloaded")
        return result

    async def rotate_thread(
        self,
        session_id: str,
        *,
        expected: str,
        replacement: str,
    ) -> bool:
        if not str(replacement).strip():
            raise ValueError("replacement thread must not be empty")
        cursor = await self._opened().execute(
            """
            UPDATE sdk_sessions
            SET active_thread_id = ?, updated_at = ?
            WHERE id = ? AND active_thread_id = ?
            """,
            (replacement, time.time(), str(session_id).strip(), expected),
        )
        await self._opened().commit()
        return cursor.rowcount == 1


def _record(row: aiosqlite.Row) -> StoredSession:
    return StoredSession(
        id=row["id"],
        title=row["title"],
        agent_id=row["agent_id"],
        metadata=json.loads(row["metadata_json"]),
        active_thread_id=row["active_thread_id"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


__all__ = ["SQLiteSessionStore", "StoredSession"]
