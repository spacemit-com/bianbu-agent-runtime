from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

import pytest

from bianbu_agent.runs import RunResult
from bianbu_agent.runs.contracts import RunHandle
from bianbu_agent.runs.events import RunEvent
from bianbu_agent.sessions.log_store import (
    SessionLogStore,
    read_session_transcript,
)


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_failed_finish_keeps_the_result_and_reports_the_unsaved_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写不进去不改变 Run 的结论，但必须明确报出「还没确认保存」这一段。"""

    from bianbu_agent.sessions import log_store as module

    monkeypatch.setattr(module, "_DURABLE_STALL_SECONDS", 0)
    store = SessionLogStore(tmp_path, write_batch_max_delay=.02)
    await store.open()
    handle = RunHandle(request_id="run", session_id="session", store=store)
    await handle.start({"prompt": "question"})
    await store.flush_session("session")
    original = store._append_transcript

    def failing(*args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_append_transcript", failing)
    await handle.publish(RunEvent("token", {"text": "answer"}))
    await handle.finish(RunResult("run", "completed", "answer"))
    result = await asyncio.wait_for(handle.wait(), .2)
    assert result.status == "completed"
    persistence = store.persistence_state("session")
    assert persistence["state"] == "error"
    assert persistence["error"]["code"] == "session_log_write_failed"
    assert "No space" in persistence["error"]["details"]["cause"]
    assert persistence["durable_events"] == 1
    assert persistence["pending_events"] == 2
    # 内存里的对话照常可读：写盘失败不该让用户看不到内容。
    assert [event.type for event in await store.read_session_events("session")] == [
        "user/message", "assistant/text.delta", "run/completed",
    ]
    # 磁盘恢复后写批自己重试，不需要新事件或显式 flush。
    monkeypatch.setattr(store, "_append_transcript", original)
    await _wait_until(lambda: store.persistence_state("session")["state"] == "saved")
    assert store.write_failure("session") is None
    await store.close()
    reopened = SessionLogStore(tmp_path)
    await reopened.open()
    assert [event.type for event in await reopened.read_session_events("session")] == [
        "user/message", "assistant/text.delta", "run/completed",
    ]
    assert (await reopened.get("run")).result.content == "answer"
    await reopened.close()


@pytest.mark.asyncio
async def test_explicit_flush_failure_is_tracked_and_recovers_without_new_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.sessions import log_store as module

    monkeypatch.setattr(module, "_DURABLE_STALL_SECONDS", 0)
    store = SessionLogStore(tmp_path, write_batch_max_delay=.02)
    await store.open()
    await store.start("run", "session", {"prompt": "question"})
    await store.flush_session("session")
    original = store._append_transcript

    def failing(*args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_append_transcript", failing)
    await store.append("run", RunEvent("token", {"text": "pending"}))
    with pytest.raises(OSError):
        await store.flush_session("session")
    assert store.write_failure("session") is not None
    with pytest.raises(module.SessionLogWriteError):
        await store.append("run", RunEvent("token", {"text": "rejected"}))
    monkeypatch.setattr(store, "_append_transcript", original)
    await _wait_until(lambda: store.durable_events("session") == 2)
    assert store.write_failure("session") is None
    await store.close()


@pytest.mark.asyncio
async def test_finish_timeout_keeps_the_shared_write_alive_and_commits_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.sessions import log_store as module

    monkeypatch.setattr(module, "_FINISH_TIMEOUT_SECONDS", .02)
    store = SessionLogStore(tmp_path, write_batch_max_delay=60)
    await store.open()
    handle = RunHandle(request_id="run", session_id="session", store=store)
    await handle.start({"prompt": "question"})
    await store.flush_session("session")
    original = store._persist_batch
    entered, release = asyncio.Event(), asyncio.Event()
    writes = 0

    async def stalled(session_id, batch):
        nonlocal writes
        writes += 1
        entered.set()
        await release.wait()
        await original(session_id, batch)

    monkeypatch.setattr(store, "_persist_batch", stalled)
    finishing = asyncio.create_task(handle.finish(RunResult("run", "completed", "answer")))
    await entered.wait()
    cancelled_reader = asyncio.create_task(store.flush_session("session"))
    await asyncio.sleep(0)
    cancelled_reader.cancel()
    await asyncio.gather(cancelled_reader, return_exceptions=True)
    try:
        await asyncio.wait_for(finishing, .5)
        result = await handle.wait()
        assert result.status == "completed"
        # 期限只让调用方先走，后台那次写入必须继续活着，而且只写一次。
        assert [event.type for event in await store.read_session_events("session")].count(
            "run/completed"
        ) == 1
    finally:
        release.set()
        await store.flush_session("session")
        await store.close()
    assert writes == 1
    assert (await store.get("run")).status == "completed"
    assert [event.type for event in await store.read_session_events("session")].count("run/completed") == 1


@pytest.mark.asyncio
async def test_one_transcript_restores_run_and_complete_event_order(
    tmp_path: Path,
) -> None:
    """Duplicating Run events outside the Session transcript is a storage bug."""

    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=0.01)
    await store.open()
    await store.start(
        "run-a",
        "session-a",
        {
            "prompt": "question",
            "thread_id": "thread-a",
            "agent_id": "agent-a",
            "idempotency_key": "key-a",
            "metadata": {},
        },
    )
    await store.append("run-a", RunEvent("reasoning", {"text": "plan"}))
    await store.append(
        "run-a",
        RunEvent(
            "tool_start",
            {"call_id": "call-a", "name": "lookup", "arguments": {"q": 1}},
        ),
    )
    await store.append(
        "run-a",
        RunEvent(
            "tool_end",
            {"call_id": "call-a", "name": "lookup", "result": "found"},
        ),
    )
    await store.append("run-a", RunEvent("token", {"text": "answer"}))
    await store.finish(
        "run-a",
        RunResult(request_id="run-a", status="completed", content="answer"),
    )
    await store.close()

    transcripts = [path for path in root.rglob("*") if path.is_file()]
    # One Session directory holds exactly its transcript and its projection.
    assert sorted(path.relative_to(root).as_posix() for path in transcripts) == [
        "session-a/projection.json",
        "session-a/session.jsonl.zstd",
    ]
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "session-events").exists()

    reopened = SessionLogStore(root)
    await reopened.open()
    events = await reopened.read_session_events("session-a")
    record = await reopened.get("run-a")

    assert [event.seq for event in events] == list(range(6))
    assert [event.type for event in events] == [
        "user/message",
        "assistant/reasoning.delta",
        "tool/call.started",
        "tool/call.completed",
        "assistant/text.delta",
        "run/completed",
    ]
    assert record is not None
    assert record.request["idempotency_key"] == "key-a"
    assert record.events == (
        RunEvent("reasoning", {"text": "plan"}),
        RunEvent(
            "tool_start",
            {"call_id": "call-a", "name": "lookup", "arguments": {"q": 1}},
        ),
        RunEvent(
            "tool_end",
            {"call_id": "call-a", "name": "lookup", "result": "found"},
        ),
        RunEvent("token", {"text": "answer"}),
    )
    assert record.result == RunResult(
        request_id="run-a", status="completed", content="answer"
    )
    await reopened.close()


@pytest.mark.asyncio
async def test_window_read_slices_the_contiguous_transcript(tmp_path: Path) -> None:
    """A window read is a slice, not a scan of the whole Session transcript."""

    store = SessionLogStore(tmp_path / "sessions", write_batch_max_delay=0.01)
    await store.open()
    await store.start("run-window", "session-window", {"prompt": "question"})
    for chunk in range(500):
        await store.append("run-window", RunEvent("reasoning", {"text": f"{chunk}"}))
    await store.finish(
        "run-window", RunResult("run-window", "completed", "answer"),
    )
    events = await store.read_session_events("session-window")
    assert [event.seq for event in events] == list(range(len(events)))

    for start, stop in ((0, None), (7, None), (0, 13), (13, 260), (501, None), (0, 0)):
        expected = tuple(
            event for event in events
            if event.seq >= start and (stop is None or event.seq < stop)
        )
        assert await store.read_session_events(
            "session-window", from_seq=start, before_seq=stop,
        ) == expected

    assert await store.read_session_events("session-window", from_seq=10_000) == ()
    assert await store.read_session_events("missing-session") == ()
    with pytest.raises(ValueError):
        await store.read_session_events("session-window", before_seq=-1)


@pytest.mark.asyncio
async def test_new_event_retries_retained_background_batch_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached write failure must not permanently strand accepted events."""

    store = SessionLogStore(
        tmp_path / "sessions",
        write_batch_max_delay=0.01,
    )
    await store.open()
    original = store._append_transcript
    attempts: list[list[int]] = []

    def fail_first(session_id, batch, first):
        attempts.append([event.seq for event in batch])
        if len(attempts) == 1:
            raise OSError("simulated disk failure")
        return original(session_id, batch, first)

    monkeypatch.setattr(store, "_append_transcript", fail_first)
    await store.start("run-a", "session-a", {"prompt": "hello"})
    await _wait_until(lambda: len(attempts) == 1)

    await store.append("run-a", RunEvent("reasoning", {"text": "still live"}))
    await _wait_until(lambda: len(attempts) == 2)
    await store.flush_session("session-a")

    assert attempts == [[0], [0, 1]]
    assert [event.seq for event in await store.read_session_events("session-a")] == [
        0,
        1,
    ]
    await store.close()


@pytest.mark.asyncio
async def test_live_append_is_visible_immediately_and_still_reaches_disk(
    tmp_path: Path,
) -> None:
    """写入不等待磁盘，可见性也不等待磁盘；未落盘由 persistence 单独上报。"""

    store = SessionLogStore(tmp_path / "sessions", write_batch_max_delay=0.01)
    await store.open()
    release = asyncio.Event()
    entered = asyncio.Event()
    original = store._persist_batch

    async def blocked(session_id, batch):
        entered.set()
        await release.wait()
        await original(session_id, batch)

    store._writers.clear()
    store._persist_batch = blocked  # type: ignore[method-assign]
    await store.start("run-a", "session-a", {"prompt": "hello"})
    await asyncio.wait_for(entered.wait(), timeout=0.2)

    await asyncio.wait_for(
        store.append("run-a", RunEvent("token", {"text": "immediate"})),
        timeout=0.02,
    )
    # 生产不被磁盘阻塞；磁盘还慢着的时候，读内存也照样能看到内容。
    assert store.durable_events("session-a") == 0
    assert [event.seq for event in await store.read_session_events("session-a")] == [0, 1]
    assert await store.session_event_tail("session-a") == 1
    assert store.persistence_state("session-a")["state"] == "pending"

    release.set()
    await store.flush_session("session-a")
    assert store.durable_events("session-a") == 2
    assert store.persistence_state("session-a")["state"] == "saved"
    await store.close()


@pytest.mark.asyncio
async def test_commit_wakes_subscribers_even_when_no_further_event_arrives(
    tmp_path: Path,
) -> None:
    """最后一批提交后必须自己能唤醒订阅者：通知不能依赖下一条事件。"""

    store = SessionLogStore(tmp_path / "session-logs", write_batch_max_delay=0.01)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hello"})
    await store.flush_session("session-a")

    revision = store.change_revision
    waiter = asyncio.create_task(store.wait_for_change(revision))
    await asyncio.sleep(0)
    assert not waiter.done()

    # 提交发生在订阅者已经睡下之后，且提交之后再没有新事件。
    await store.append("run-a", RunEvent("token", {"text": "last"}))
    await store.flush_session("session-a")

    await asyncio.wait_for(waiter, timeout=1.0)
    assert await store.session_event_tail("session-a") == 1
    await store.close()


@pytest.mark.asyncio
async def test_persistent_write_failure_stops_accepting_new_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写不进去时必须显式失败，并报出还有多少内容确实存住了。"""

    from bianbu_agent.sessions import log_store as log_store_module

    monkeypatch.setattr(log_store_module, "_DURABLE_STALL_SECONDS", 0.0)
    store = SessionLogStore(tmp_path / "session-logs", write_batch_max_delay=0.01)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hello"})
    await store.flush_session("session-a")

    async def failing(session_id, batch):
        raise OSError("No space left on device")

    store._persist_batch = failing  # type: ignore[method-assign]
    await store.append("run-a", RunEvent("token", {"text": "lost"}))
    await _wait_until(lambda: store.write_failure("session-a") is not None)

    failure = store.write_failure("session-a")
    assert failure is not None
    assert failure.session_log_durable_events == 1
    assert "No space left on device" in failure.session_log_cause
    with pytest.raises(log_store_module.SessionLogWriteError):
        await store.append("run-a", RunEvent("token", {"text": "rejected"}))
    # 停收只拦新工作，不拦读取：没保存的那一段也要让用户看得见，
    # 并带上「存住了几条、为什么存不住」。
    assert [event.seq for event in await store.read_session_events("session-a")] == [0, 1]
    persistence = store.persistence_state("session-a")
    assert persistence["state"] == "error"
    assert persistence["durable_events"] == 1
    assert persistence["pending_events"] == 1
    assert "No space left on device" in persistence["error"]["details"]["cause"]

    await store.close()


@pytest.mark.asyncio
async def test_reopen_discards_only_a_torn_final_compression_frame(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hello"})
    await store.append("run-a", RunEvent("reasoning", {"text": "kept"}))
    await store.flush_session("session-a")
    await store.close()
    transcript = root / "session-a" / "session.jsonl.zstd"
    valid_size = transcript.stat().st_size
    with transcript.open("ab") as stream:
        stream.write(b"\x28\xb5\x2f\xfd\x00torn")

    reopened = SessionLogStore(root)
    await reopened.open()

    assert [event.seq for event in await reopened.read_session_events("session-a")] == [0, 1]
    assert transcript.stat().st_size == valid_size
    await reopened.close()


@pytest.mark.asyncio
async def test_read_only_transcript_reader_never_writes(tmp_path: Path) -> None:
    """A sandbox Worker folds the control-owned log without touching it.

    The Worker shares the data directory but owns no writer for it, so the
    reader must skip a torn tail instead of truncating it and must leave the
    projection file alone.
    """

    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=60)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hello"})
    await store.flush_session("session-a")
    await store.close()
    transcript = root / "session-a" / "session.jsonl.zstd"
    valid_size = transcript.stat().st_size
    session_dir = transcript.parent
    untouched = {
        path.name: path.stat().st_mtime_ns
        for path in session_dir.iterdir()
        if path != transcript
    }
    outside = sorted(path.name for path in root.iterdir() if path.is_dir() and path != session_dir)
    with transcript.open("ab") as stream:
        stream.write(b"\x28\xb5\x2f\xfd\x00torn")

    events = read_session_transcript(root, "session-a")

    assert [event.type for event in events] == ["user/message"]
    assert events[0].session_id == "session-a"
    assert transcript.stat().st_size > valid_size
    assert outside == []
    assert {
        path.name: path.stat().st_mtime_ns
        for path in session_dir.iterdir()
        if path != transcript
    } == untouched
    # An unknown Session is empty rather than an error: a Worker may be asked
    # to fold a Session the control plane has not written yet.
    assert read_session_transcript(root, "session-missing") == ()


@pytest.mark.parametrize(
    "boundary",
    ["\u0085", "\u2028", "\u2029"],
    ids=["next-line", "line-separator", "paragraph-separator"],
)
@pytest.mark.asyncio
async def test_reopen_splits_transcript_documents_on_newline_only(
    tmp_path: Path,
    boundary: str,
) -> None:
    """Tool output can carry line boundaries that JSON never escapes.

    ``json.dumps`` escapes ``U+0000``-``U+001F`` but leaves ``U+0085``,
    ``U+2028``, and ``U+2029`` verbatim, so they reach the transcript as-is.
    Splitting documents with ``str.splitlines()`` would break them into
    fragments that are not JSON, failing Session log restore at startup.
    """

    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=0.01)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "hello"})
    output = f"before{boundary}after"
    await store.append(
        "run-a",
        RunEvent(
            "tool_end",
            {
                "call_id": "call-a",
                "name": "terminal",
                "result": json.dumps(
                    {"output": output, "exit_code": 0, "status": "completed"},
                    ensure_ascii=False,
                ),
            },
        ),
    )
    await store.close()

    reopened = SessionLogStore(root)
    await reopened.open()
    events = await reopened.read_session_events("session-a")
    await reopened.close()

    assert [event.seq for event in events] == [0, 1]
    completed = [event for event in events if event.type == "tool/call.completed"]
    assert len(completed) == 1
    assert output in str(completed[0].data["result"])
@pytest.mark.asyncio
async def test_finish_budget_also_covers_waiting_for_the_session_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.sessions import log_store as module

    monkeypatch.setattr(module, "_FINISH_TIMEOUT_SECONDS", .05)
    store = SessionLogStore(tmp_path, write_batch_max_delay=60)
    await store.open()
    handle = RunHandle(request_id="run", session_id="session", store=store)
    await handle.start({"prompt": "question"})
    await store.flush_session("session")
    holder = store._lock("session")
    await holder.acquire()

    finishing = asyncio.create_task(
        handle.finish(RunResult("run", "completed", "answer"))
    )
    await asyncio.wait_for(finishing, 1)
    # 期限覆盖等锁：锁被别人握着时也不能把调用方挂死，结论照常是真实结果。
    assert (await handle.wait()).status == "completed"

    holder.release()
    # 等那个后台任务真的把终态补上（用「saved」会空转：还没追加时本来就无待写）
    await _wait_until(lambda: store.durable_events("session") == 2)
    assert (await store.get("run")).status == "completed"
    assert store.write_failure("session") is None
    assert [event.type for event in await store.read_session_events("session")].count(
        "run/completed"
    ) == 1
    await store.close()


@pytest.mark.asyncio
async def test_a_late_timeout_cannot_overwrite_a_committed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionLogStore(tmp_path, write_batch_max_delay=60)
    await store.open()
    handle = RunHandle(request_id="run", session_id="session", store=store)
    await handle.start({"prompt": "question"})
    await store.flush_session("session")
    real_flush = store.flush_session

    async def commit_then_report_timeout(session_id):
        await real_flush(session_id)
        raise TimeoutError("simulated late timeout")

    monkeypatch.setattr(store, "flush_session", commit_then_report_timeout)
    await handle.finish(RunResult("run", "completed", "answer"))

    assert (await handle.wait()).status == "completed"
    assert store.persistence_state("session")["state"] == "saved"
    assert store.write_failure("session") is None
    await store.close()


@pytest.mark.asyncio
async def test_a_slow_flush_never_turns_a_finished_run_into_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.sessions import log_store as module

    monkeypatch.setattr(module, "_FINISH_TIMEOUT_SECONDS", .01)
    monkeypatch.setattr(module, "_DURABLE_STALL_SECONDS", 0)
    store = SessionLogStore(tmp_path, write_batch_max_delay=.02)
    await store.open()
    handle = RunHandle(request_id="run", session_id="session", store=store)
    await handle.start({"prompt": "question"})
    await store.flush_session("session")
    real_flush = store.flush_session

    async def slow_flush(session_id):
        await asyncio.sleep(.05)
        await real_flush(session_id)

    monkeypatch.setattr(store, "flush_session", slow_flush)
    await handle.finish(RunResult("run", "completed", "answer"))
    result = await handle.wait()
    assert result.status == "completed"

    monkeypatch.setattr(store, "flush_session", real_flush)
    await _wait_until(lambda: store.persistence_state("session")["state"] == "saved")
    assert store.write_failure("session") is None
    assert (await store.get("run")).status == "completed"
    # 过时的超时不允许留下会永久拦住写入的错误。
    await store.start("run2", "session", {"prompt": "next"})
    await store.flush_session("session")
    await store.close()


@pytest.mark.asyncio
async def test_reopening_a_store_keeps_flushing_new_events(
    tmp_path: Path,
) -> None:
    store = SessionLogStore(tmp_path, write_batch_max_delay=.02)
    await store.open()
    await store.start("run", "session", {"prompt": "question"})
    await store.flush_session("session")
    await store.close()

    await store.open()
    await store.start("run2", "session", {"prompt": "next"})
    await _wait_until(lambda: store.durable_events("session") == 2)
    await store.close()


@pytest.mark.asyncio
async def test_first_batch_recovers_after_a_failed_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionLogStore(tmp_path, write_batch_max_delay=60)
    await store.open()
    await store.start("run", "session", {"prompt": "question"})
    original = store._fsync_directory

    def failing_directory_sync(directory):
        raise OSError(5, "injected directory fsync failure")

    monkeypatch.setattr(store, "_fsync_directory", failing_directory_sync)
    with pytest.raises(OSError):
        await store.flush_session("session")
    monkeypatch.setattr(store, "_fsync_directory", original)
    await store.flush_session("session")
    assert store.durable_events("session") == 1
    await store.close()

    reopened = SessionLogStore(tmp_path)
    await reopened.open()
    assert reopened.durable_events("session") == 1
    await reopened.close()
@pytest.mark.asyncio
async def test_session_filtered_list_stays_on_the_bounded_projection(
    tmp_path: Path,
) -> None:
    """A Session status view must not re-read the Session transcript."""

    root = tmp_path / "sessions"
    store = SessionLogStore(root, write_batch_max_delay=0.01)
    await store.open()
    await store.start("run-a", "session-a", {"prompt": "question", "metadata": {}})
    for index in range(50):
        await store.append("run-a", RunEvent("token", {"text": f"{index},"}))
    await store.append(
        "run-a", RunEvent("approval_required", {"approval_id": "a-1"})
    )
    await store.finish("run-a", RunResult("run-a", "completed", "done"))

    async def forbidden(session_id: str):
        raise AssertionError("a Session listing re-read the transcript")

    store._session_run_events = forbidden
    records = await store.list(session_id="session-a")
    await store.close()

    assert [record.request_id for record in records] == ["run-a"]
    assert records[0].status == "completed"
    # 状态、请求与待审批都来自投影，因此不需要回放任何 Run 事件。
    assert [event.kind for event in records[0].events] == [
        "token",
        "approval_required",
    ]
