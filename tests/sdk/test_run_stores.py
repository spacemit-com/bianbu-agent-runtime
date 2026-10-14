from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from bianbu_agent.runs.events import RunEvent
from bianbu_agent.runs import RunResult
from bianbu_agent.runs.stores.sqlite import SQLiteRunStore


STORE_TYPES = (SQLiteRunStore,)


def _store(store_type: type[SQLiteRunStore], root: Path):
    return store_type(root / "runs.sqlite3")


@pytest.mark.asyncio
@pytest.mark.parametrize("store_type", STORE_TYPES)
async def test_store_persists_contiguous_session_event_log(
    tmp_path: Path,
    store_type: type[SQLiteRunStore],
) -> None:
    store = _store(store_type, tmp_path)
    await store.open()
    await store.start(
        "run-a", "session-a",
        {"prompt": "hello", "thread_id": "thread-a", "metadata": {}},
    )
    await store.append("run-a", RunEvent("reasoning", {"text": "plan"}))
    await store.append("run-a", RunEvent("token", {"text": "answer"}))
    await store.finish("run-a", RunResult("run-a", "completed", "answer"))

    events = await store.read_session_events("session-a")

    assert [event.seq for event in events] == [0, 1, 2, 3]
    assert [event.type for event in events] == [
        "user/message",
        "assistant/reasoning.delta",
        "assistant/text.delta",
        "run/completed",
    ]
    assert await store.session_event_tail("session-a") == 3
    assert [event.seq for event in await store.read_session_events(
        "session-a", from_seq=2
    )] == [2, 3]
    assert [event.seq for event in await store.read_session_events(
        "session-a", before_seq=2
    )] == [0, 1]
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_type", STORE_TYPES)
async def test_reasoning_session_events_survive_store_reopen(
    tmp_path: Path,
    store_type: type[SQLiteRunStore],
) -> None:
    store = _store(store_type, tmp_path)
    await store.open()
    await store.start("thinking-run", "thinking-session", {"prompt": "question"})
    await store.append("thinking-run", RunEvent("reasoning", {"text": "deep "}))
    await store.append("thinking-run", RunEvent("reasoning", {"text": "thought"}))
    await store.close()

    reopened = _store(store_type, tmp_path)
    await reopened.open()
    events = await reopened.read_session_events("thinking-session")

    assert [
        event.data["text"]
        for event in events
        if event.type == "assistant/reasoning.delta"
    ] == ["deep ", "thought"]
    await reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_type", STORE_TYPES)
async def test_session_event_sequences_are_unique_under_concurrent_append(
    tmp_path: Path,
    store_type: type[SQLiteRunStore],
) -> None:
    store = _store(store_type, tmp_path)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hello"})

    await asyncio.gather(*(
        store.append("run-a", RunEvent("token", {"text": str(index)}))
        for index in range(20)
    ))

    events = await store.read_session_events("session-a")
    assert [event.seq for event in events] == list(range(21))
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_type", STORE_TYPES)
async def test_deleting_run_removes_and_compacts_session_events(
    tmp_path: Path,
    store_type: type[SQLiteRunStore],
) -> None:
    store = _store(store_type, tmp_path)
    await store.open()
    for run_id in ("run-a", "run-b"):
        await store.start(run_id, "session-a", {"prompt": run_id})
        await store.append(run_id, RunEvent("token", {"text": run_id}))
        await store.finish(run_id, RunResult(run_id, "completed", run_id))

    assert await store.delete("run-a") is True
    events = await store.read_session_events("session-a")

    assert [event.seq for event in events] == [0, 1, 2]
    assert {event.run_id for event in events} == {"run-b"}
    assert await store.delete_session("session-a") == 1
    assert await store.read_session_events("session-a") == ()
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store_type", STORE_TYPES)
async def test_concurrent_append_once_persists_one_run_event(
    tmp_path: Path,
    store_type: type[SQLiteRunStore],
) -> None:
    store = _store(store_type, tmp_path)
    await store.open()
    await store.start("source-run", "session-1", {"prompt": "source"})
    event = RunEvent(
        "context_compression",
        {"summary": "compacted"},
    )

    first, second = await asyncio.gather(
        store.append_once("source-run", event, key="context_compression:batch-1"),
        store.append_once("source-run", event, key="context_compression:batch-1"),
    )

    record = await store.get("source-run")
    assert sorted((first, second)) == [False, True]
    assert [stored for stored in record.events if stored.kind == event.kind] == [event]
    await store.close()


@pytest.mark.asyncio
async def test_sqlite_append_once_is_atomic_across_connections(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.sqlite3"
    first_store = SQLiteRunStore(path)
    second_store = SQLiteRunStore(path)
    await first_store.open()
    await second_store.open()
    await first_store.start("source-run", "session-1", {"prompt": "source"})
    event = RunEvent(
        "context_compression",
        {"summary": "compacted"},
    )

    first, second = await asyncio.gather(
        first_store.append_once(
            "source-run", event, key="context_compression:batch-1"
        ),
        second_store.append_once(
            "source-run", event, key="context_compression:batch-1"
        ),
    )

    record = await first_store.get("source-run")
    assert sorted((first, second)) == [False, True]
    assert [stored for stored in record.events if stored.kind == event.kind] == [event]
    await first_store.close()
    await second_store.close()


@pytest.mark.asyncio
async def test_sqlite_store_schema_contains_only_run_tables(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    store = SQLiteRunStore(path)
    await store.open()
    await store.close()

    with sqlite3.connect(path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "sdk_run_followups" not in names
