from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import aiosqlite

from bianbu_agent.runs.events import RunEvent
from bianbu_agent.runs.models import StoredRun
from bianbu_agent.sessions.events import (
    SessionEvent,
    session_event_for_run_event,
    session_event_for_run_result,
    session_event_for_run_start,
)


class SQLiteRunStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path).expanduser().resolve()
        self._connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def open(self) -> None:
        if self._connection is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self._path)
        connection.row_factory = aiosqlite.Row
        await connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sdk_runs (
                request_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sdk_run_events (
                request_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(request_id, sequence),
                FOREIGN KEY(request_id) REFERENCES sdk_runs(request_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS sdk_run_event_keys (
                request_id TEXT NOT NULL,
                event_key TEXT NOT NULL,
                PRIMARY KEY(request_id, event_key),
                FOREIGN KEY(request_id) REFERENCES sdk_runs(request_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS sdk_session_events (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                time REAL NOT NULL,
                run_id TEXT,
                data_json TEXT NOT NULL,
                PRIMARY KEY(session_id, seq)
            );
            CREATE INDEX IF NOT EXISTS sdk_session_events_run_id
            ON sdk_session_events(run_id);
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
            raise RuntimeError("Run store is not open")
        return self._connection

    async def start(self, request_id: str, session_id: str, request: dict) -> None:
        now = time.time()
        async with self._write_lock:
            connection = self._opened()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    "INSERT INTO sdk_runs VALUES (?, ?, 'running', ?, NULL, ?, ?)",
                    (
                        request_id,
                        session_id,
                        json.dumps(request, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
                seq = await self._next_session_seq(connection, session_id)
                await self._insert_session_event(
                    connection,
                    session_event_for_run_start(
                        session_id, request_id, request, seq=seq, event_time=now,
                    ),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def append(self, request_id: str, event: RunEvent) -> None:
        async with self._write_lock:
            connection = self._opened()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                run = await self._run_identity(connection, request_id)
                cursor = await connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM sdk_run_events WHERE request_id = ?",
                    (request_id,),
                )
                row = await cursor.fetchone()
                event_time = time.time()
                await connection.execute(
                    "INSERT INTO sdk_run_events VALUES (?, ?, ?, ?)",
                    (
                        request_id,
                        int(row["next"]),
                        event.kind,
                        json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                seq = await self._next_session_seq(connection, run["session_id"])
                await self._insert_session_event(
                    connection,
                    session_event_for_run_event(
                        run["session_id"], request_id, event,
                        seq=seq, event_time=event_time,
                    ),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def append_once(
        self,
        request_id: str,
        event: RunEvent,
        *,
        key: str,
    ) -> bool:
        selected_key = str(key).strip()
        if not selected_key:
            raise ValueError("event key must not be empty")
        async with self._write_lock:
            connection = self._opened()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                reserved = await connection.execute(
                    """
                    INSERT OR IGNORE INTO sdk_run_event_keys (request_id, event_key)
                    VALUES (?, ?)
                    """,
                    (request_id, selected_key),
                )
                if reserved.rowcount == 0:
                    await connection.commit()
                    return False
                cursor = await connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1 AS next
                    FROM sdk_run_events WHERE request_id = ?
                    """,
                    (request_id,),
                )
                row = await cursor.fetchone()
                run = await self._run_identity(connection, request_id)
                event_time = time.time()
                await connection.execute(
                    "INSERT INTO sdk_run_events VALUES (?, ?, ?, ?)",
                    (
                        request_id,
                        int(row["next"]),
                        event.kind,
                        json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                seq = await self._next_session_seq(connection, run["session_id"])
                await self._insert_session_event(
                    connection,
                    session_event_for_run_event(
                        run["session_id"], request_id, event,
                        seq=seq, event_time=event_time,
                    ),
                )
                await connection.commit()
                return True
            except BaseException:
                await connection.rollback()
                raise

    async def finish(self, request_id: str, result) -> None:
        from bianbu_agent.runs import encode_result

        async with self._write_lock:
            connection = self._opened()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                run = await self._run_identity(connection, request_id)
                event_time = time.time()
                await connection.execute(
                    "UPDATE sdk_runs SET status = ?, result_json = ?, updated_at = ? WHERE request_id = ?",
                    (
                        result.status,
                        json.dumps(encode_result(result), ensure_ascii=False, sort_keys=True),
                        event_time,
                        request_id,
                    ),
                )
                seq = await self._next_session_seq(connection, run["session_id"])
                terminal = session_event_for_run_result(
                    run["session_id"], result, seq=seq, event_time=event_time,
                )
                existing = await connection.execute(
                    """
                    SELECT seq FROM sdk_session_events
                    WHERE session_id = ? AND run_id = ?
                      AND type IN ('run/completed', 'run/failed', 'run/cancelled')
                    """,
                    (run["session_id"], request_id),
                )
                terminal_row = await existing.fetchone()
                if terminal_row is None:
                    await self._insert_session_event(connection, terminal)
                else:
                    await connection.execute(
                        """
                        UPDATE sdk_session_events
                        SET type = ?, time = ?, data_json = ?
                        WHERE session_id = ? AND seq = ?
                        """,
                        (
                            terminal.type,
                            terminal.time,
                            json.dumps(dict(terminal.data), ensure_ascii=False, sort_keys=True),
                            run["session_id"],
                            int(terminal_row["seq"]),
                        ),
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def get(self, request_id: str) -> StoredRun | None:
        cursor = await self._opened().execute(
            "SELECT * FROM sdk_runs WHERE request_id = ?", (request_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        event_cursor = await self._opened().execute(
            "SELECT kind, payload_json FROM sdk_run_events WHERE request_id = ? ORDER BY sequence",
            (request_id,),
        )
        events = tuple(
            RunEvent(kind=event["kind"], payload=json.loads(event["payload_json"]))
            for event in await event_cursor.fetchall()
        )
        result = None
        if row["result_json"]:
            from bianbu_agent.runs import decode_result

            result = decode_result(json.loads(row["result_json"]))
        return StoredRun(
            request_id=row["request_id"],
            session_id=row["session_id"],
            status=row["status"],
            request=json.loads(row["request_json"]),
            events=events,
            result=result,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    async def read_session_events(
        self,
        session_id: str,
        *,
        from_seq: int = 0,
        before_seq: int | None = None,
    ) -> tuple[SessionEvent, ...]:
        if from_seq < 0 or (before_seq is not None and before_seq < 0):
            raise ValueError("Session event positions must not be negative")
        query = """
            SELECT seq, type, time, run_id, data_json
            FROM sdk_session_events
            WHERE session_id = ? AND seq >= ?
        """
        params: list[object] = [str(session_id), from_seq]
        if before_seq is not None:
            query += " AND seq < ?"
            params.append(before_seq)
        query += " ORDER BY seq"
        cursor = await self._opened().execute(query, params)
        return tuple(
            SessionEvent(
                session_id=str(session_id), seq=int(row["seq"]),
                type=str(row["type"]), time=float(row["time"]),
                run_id=None if row["run_id"] is None else str(row["run_id"]),
                data=json.loads(row["data_json"]),
            )
            for row in await cursor.fetchall()
        )

    async def session_event_tail(self, session_id: str) -> int | None:
        cursor = await self._opened().execute(
            "SELECT MAX(seq) AS tail FROM sdk_session_events WHERE session_id = ?",
            (str(session_id),),
        )
        row = await cursor.fetchone()
        return None if row["tail"] is None else int(row["tail"])

    async def delete(self, request_id: str) -> bool:
        normalized = str(request_id).strip()
        async with self._write_lock:
            connection = self._opened()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                run = await connection.execute(
                    "SELECT session_id FROM sdk_runs WHERE request_id = ?", (normalized,)
                )
                run_row = await run.fetchone()
                await connection.execute(
                    "DELETE FROM sdk_run_events WHERE request_id = ?", (normalized,)
                )
                await connection.execute(
                    "DELETE FROM sdk_run_event_keys WHERE request_id = ?", (normalized,)
                )
                cursor = await connection.execute(
                    "DELETE FROM sdk_runs WHERE request_id = ?", (normalized,)
                )
                if run_row is not None:
                    await connection.execute(
                        "DELETE FROM sdk_session_events WHERE run_id = ?", (normalized,)
                    )
                    await self._compact_session_events(connection, run_row["session_id"])
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            return cursor.rowcount == 1

    async def delete_session(self, session_id: str) -> int:
        normalized = str(session_id).strip()
        async with self._write_lock:
            connection = self._opened()
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute(
                    """
                    DELETE FROM sdk_run_events
                    WHERE request_id IN (
                        SELECT request_id FROM sdk_runs WHERE session_id = ?
                    )
                    """,
                    (normalized,),
                )
                await connection.execute(
                    """
                    DELETE FROM sdk_run_event_keys
                    WHERE request_id IN (
                        SELECT request_id FROM sdk_runs WHERE session_id = ?
                    )
                    """,
                    (normalized,),
                )
                cursor = await connection.execute(
                    "DELETE FROM sdk_runs WHERE session_id = ?", (normalized,)
                )
                await connection.execute(
                    "DELETE FROM sdk_session_events WHERE session_id = ?", (normalized,)
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            return cursor.rowcount

    async def _run_identity(self, connection, request_id: str):
        cursor = await connection.execute(
            "SELECT session_id FROM sdk_runs WHERE request_id = ?", (request_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise KeyError(request_id)
        return row

    @staticmethod
    async def _next_session_seq(connection, session_id: str) -> int:
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM sdk_session_events WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return int(row["next"])

    @staticmethod
    async def _insert_session_event(connection, event: SessionEvent) -> None:
        await connection.execute(
            "INSERT INTO sdk_session_events VALUES (?, ?, ?, ?, ?, ?)",
            (
                event.session_id, event.seq, event.type, event.time, event.run_id,
                json.dumps(dict(event.data), ensure_ascii=False, sort_keys=True),
            ),
        )

    @staticmethod
    async def _compact_session_events(connection, session_id: str) -> None:
        cursor = await connection.execute(
            """
            SELECT type, time, run_id, data_json FROM sdk_session_events
            WHERE session_id = ? ORDER BY seq
            """,
            (session_id,),
        )
        rows = await cursor.fetchall()
        await connection.execute(
            "DELETE FROM sdk_session_events WHERE session_id = ?", (session_id,)
        )
        await connection.executemany(
            "INSERT INTO sdk_session_events VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id, index, row["type"], row["time"],
                    row["run_id"], row["data_json"],
                )
                for index, row in enumerate(rows)
            ],
        )

__all__ = ["SQLiteRunStore", "StoredRun"]
