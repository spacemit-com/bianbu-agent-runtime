from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from bianbu_agent.api.events import session_mux, host_events
from bianbu_agent.sessions import SessionEvent
from bianbu_agent.sessions.log_store import SessionLogStore


class _Store:
    def __init__(self) -> None:
        self.available = False

    async def session_event_tail(self, _session_id: str) -> int | None:
        return None

    async def read_session_events(
        self,
        session_id: str,
        *,
        from_seq: int = 0,
    ) -> tuple[SessionEvent, ...]:
        if not self.available or from_seq > 0:
            return ()
        return (
            SessionEvent(
                session_id=session_id, seq=0, type="user/message", time=1,
                run_id="run-1", data={"content": "hello"},
            ),
            SessionEvent(
                session_id=session_id,
                seq=1,
                type="assistant/text.delta",
                time=1,
                run_id="run-1",
                data={"text": "visible immediately"},
            ),
            SessionEvent(
                session_id=session_id, seq=2, type="tool/call.started", time=2,
                run_id="run-1", data={"call_id": "tool-1", "name": "read"},
            ),
            SessionEvent(
                session_id=session_id, seq=3, type="tool/call.completed", time=3,
                run_id="run-1", data={"call_id": "tool-1", "result": "ok"},
            ),
            SessionEvent(
                session_id=session_id, seq=4, type="run/completed", time=4,
                run_id="run-1", data={"content": "done"},
            ),
        )


class _Runs:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self.change_revision = 0
        self.changed = asyncio.Event()

    async def wait_for_change(self, after: int) -> int:
        assert after == self.change_revision
        await self.changed.wait()
        self.change_revision += 1
        return self.change_revision


class _Sessions:
    def __init__(self) -> None:
        self.change_revision = 0

    async def list(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(id="session-1", updated_at=0, created_at=0, title="title", agent_id="default")]

    async def wait_for_change(self, _after: int) -> int:
        await asyncio.Future()
        raise AssertionError("unreachable")


class _WebSocket:
    def __init__(self, runtime: object) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))
        self.sent: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    async def accept(self) -> None:
        return None

    async def send_json(self, payload: dict[str, object]) -> None:
        await self.sent.put(payload)

    async def receive(self) -> dict[str, object]:
        await asyncio.Future()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_mux_subscribes_then_streams_the_whole_ordered_conversation() -> None:
    """One mux carries the baseline and every accepted conversation event."""

    store = _Store()
    runs = _Runs(store)
    websocket = _WebSocket(
        SimpleNamespace(runs=runs, sessions=_Sessions()),
    )
    task = asyncio.create_task(session_mux(websocket))
    try:
        subscribed = await asyncio.wait_for(websocket.sent.get(), timeout=1)
        assert subscribed == {
            "type": "session/subscribed",
            "session_id": "session-1",
            "last_seq": -1,
        }

        store.available = True
        runs.changed.set()
        frames = [
            await asyncio.wait_for(websocket.sent.get(), timeout=0.02)
            for _ in range(5)
        ]
        assert [frame["type"] for frame in frames] == ["session/event"] * 5
        assert [frame["event"]["seq"] for frame in frames] == list(range(5))
        assert [frame["event"]["type"] for frame in frames] == [
            "user/message", "assistant/text.delta", "tool/call.started",
            "tool/call.completed", "run/completed",
        ]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_mux_publishes_in_memory_events_without_waiting_for_disk(
    tmp_path: Path,
) -> None:
    """可见性不依赖落盘：还没写进磁盘的事件也要立刻推给订阅方。

    用真实存储，且故意不让写批有机会提交（批窗口 60s），这样「发布了」只能
    解释为读了内存日志，而不是等了落盘。
    """

    store = SessionLogStore(tmp_path / "session-logs", write_batch_max_delay=60)
    await store.open()
    runs = _Runs(store)
    websocket = _WebSocket(
        SimpleNamespace(runs=runs, sessions=_Sessions()),
    )
    task = asyncio.create_task(session_mux(websocket))
    try:
        await asyncio.wait_for(websocket.sent.get(), timeout=1)

        await store.start("run-1", "session-1", {"prompt": "hello"})
        assert store.durable_events("session-1") == 0
        runs.changed.set()

        frame = await asyncio.wait_for(websocket.sent.get(), timeout=1)
        assert frame["type"] == "session/event"
        assert frame["event"]["seq"] == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await store.close()


@pytest.mark.asyncio
async def test_host_snapshot_ignores_write_bookkeeping_churn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入簿记的抖动不推帧。

    `pending`/`saved` 与 `durable_events` 是每条事件 append/flush 都会动的
    记账，侧栏既读不到也画不出；拿它当变化判据等于每落一次盘就把整个会话列表
    重推给界面，界面就只能跟着重画一遍（悬停中的行会被反复重建，鼠标一上去
    就闪）。真正要让界面立刻知道的持久化信号只有带原因的写失败。
    """

    from bianbu_agent.runs.events import RunEvent

    monkeypatch.setattr(
        "bianbu_agent.sessions.log_store._DURABLE_STALL_SECONDS", 0
    )
    store = SessionLogStore(tmp_path / "session-logs", write_batch_max_delay=60)
    await store.open()
    await store.start("run-1", "session-1", {"prompt": "hello"})
    await store.flush_session("session-1")
    runs = _Runs(store)
    runs.list = store.list
    websocket = _WebSocket(SimpleNamespace(runs=runs, sessions=_Sessions()))
    task = asyncio.create_task(host_events(websocket))
    original = store._append_transcript
    try:
        initial = await asyncio.wait_for(websocket.sent.get(), timeout=1)
        assert initial["sessions"][0]["persistence"]["state"] == "saved"

        for index in range(3):
            await store.append("run-1", RunEvent("token", {"text": f"chunk {index}"}))
            runs.changed.set()
            await asyncio.sleep(0.05)
            await store.flush_session("session-1")
            runs.changed.set()
            await asyncio.sleep(0.05)
        assert websocket.sent.empty()

        def failing(*args):
            raise OSError(28, "No space left on device")

        store._append_transcript = failing
        await store.append("run-1", RunEvent("token", {"text": "pending"}))
        with pytest.raises(OSError):
            await store.flush_session("session-1")
        failed = await asyncio.wait_for(websocket.sent.get(), timeout=1)
        assert failed["sessions"][0]["persistence"]["error"]["code"] == (
            "session_log_write_failed"
        )
    finally:
        store._append_transcript = original
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await store.close()


@pytest.mark.asyncio
async def test_host_reports_write_failure_and_recovery_without_log_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.sessions import log_store as log_store_module

    monkeypatch.setattr(log_store_module, "_DURABLE_STALL_SECONDS", 0)
    store = SessionLogStore(tmp_path, write_batch_max_delay=60)
    await store.open()
    await store.start("run-1", "session-1", {"prompt": "hello"})
    await store.flush_session("session-1")
    runs = _Runs(store)
    runs.list = store.list
    websocket = _WebSocket(SimpleNamespace(runs=runs, sessions=_Sessions()))
    task = asyncio.create_task(host_events(websocket))
    original = store._append_transcript

    def failing(*args):
        raise OSError(28, "No space left on device")

    try:
        initial = await asyncio.wait_for(websocket.sent.get(), 1)
        assert initial["sessions"][0]["persistence"]["state"] == "saved"
        store._append_transcript = failing
        from bianbu_agent.runs.events import RunEvent
        await store.append("run-1", RunEvent("token", {"text": "pending"}))
        with pytest.raises(OSError):
            await store.flush_session("session-1")
        failed = await asyncio.wait_for(websocket.sent.get(), 1)
        persistence = failed["sessions"][0]["persistence"]
        assert persistence["error"]["code"] == "session_log_write_failed"
        assert "No space" in persistence["error"]["details"]["cause"]
        assert persistence["durable_events"] == 1
        store._append_transcript = original
        await store.flush_session("session-1")
        recovered = await asyncio.wait_for(websocket.sent.get(), 1)
        assert recovered["sessions"][0]["persistence"]["state"] == "saved"
    finally:
        store._append_transcript = original
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await store.close()
