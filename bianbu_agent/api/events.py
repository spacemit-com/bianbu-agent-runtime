"""Harness-style shared downlinks for Session events and Host state."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bianbu_agent.api.sessions import session_views
from bianbu_agent.sessions.events import session_event_to_dict


router = APIRouter(prefix="/events")
async def _wait_for_change_or_disconnect(
    websocket: WebSocket,
    watchers: tuple[tuple[object, int], ...],
) -> tuple[int, ...]:
    change_tasks = [
        asyncio.create_task(watcher.wait_for_change(revision))
        for watcher, revision in watchers
    ]
    receive_task = asyncio.create_task(websocket.receive())
    tasks = [*change_tasks, receive_task]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if receive_task in done:
            message = receive_task.result()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(int(message.get("code") or 1000))
        return tuple(int(watcher.change_revision) for watcher, _ in watchers)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _event_store(runtime: object):
    store = getattr(getattr(runtime, "runs", None), "_store", None)
    if store is None or not callable(getattr(store, "read_session_events", None)):
        raise RuntimeError("Session event storage is unavailable")
    return store


def _host_change_key(sessions: list[dict[str, object]]) -> str:
    """主机快照的变化键：只认侧栏画得出来的状态。

    `persistence` 的 `pending`/`saved` 与两个计数是每条事件 append/flush 都会动
    的写入簿记，侧栏既不读也不显示。把它算进变化键，等于每落一次盘就把整个
    会话列表重推一次，界面只能跟着重画（悬停中的行被重建，看起来就是鼠标一
    上去就闪）。真正要让界面立刻知道的持久化信号只有带原因的写失败。

    `as_of_seq` 是投影行的水位（每来一条事件就前进），同样不进变化键：它是
    「这个答案有多旧」的标注，不是侧栏要画的状态；它一变就重推，与写入簿记
    造成的抖动没有区别。
    """

    rendered: list[dict[str, object]] = []
    for item in sessions:
        compact = {
            key: value
            for key, value in item.items()
            if key not in {"persistence", "as_of_seq"}
        }
        persistence = item.get("persistence")
        if isinstance(persistence, dict):
            compact["persistence_error"] = persistence.get("error")
        rendered.append(compact)
    return json.dumps(
        rendered,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@router.websocket("/mux")
async def session_mux(websocket: WebSocket) -> None:
    """Publish accepted in-memory events for every Session over one connection."""

    await websocket.accept()
    runtime = websocket.app.state.runtime
    store = _event_store(runtime)
    tails: dict[str, int] = {}
    # 读内存日志，所以唤醒源只需要「有人在内存里追加了事件」。落盘是异步的，
    # 不参与可见性，因此这里不订阅存储的提交信号。
    watchers: list[object] = [runtime.runs, runtime.sessions]
    revisions = [int(watcher.change_revision) for watcher in watchers]
    try:
        while True:
            session_ids = {str(item.id) for item in await runtime.sessions.list()}
            for session_id in sorted(session_ids - tails.keys()):
                tail = await store.session_event_tail(session_id)
                tails[session_id] = -1 if tail is None else int(tail)
                await websocket.send_json({
                    "type": "session/subscribed",
                    "session_id": session_id,
                    "last_seq": tails[session_id],
                })
            for session_id in sorted(session_ids & tails.keys()):
                events = await store.read_session_events(
                    session_id,
                    from_seq=tails[session_id] + 1,
                )
                for event in events:
                    await websocket.send_json({
                        "type": "session/event",
                        "session_id": session_id,
                        "event": session_event_to_dict(event),
                    })
                    tails[session_id] = event.seq
            for session_id in sorted(tails.keys() - session_ids):
                tails.pop(session_id, None)
                await websocket.send_json({
                    "type": "session/unsubscribed",
                    "session_id": session_id,
                })
            revisions = list(await _wait_for_change_or_disconnect(
                websocket,
                tuple(zip(watchers, revisions)),
            ))
    except (WebSocketDisconnect, RuntimeError):
        return


@router.websocket("/host")
async def host_events(websocket: WebSocket) -> None:
    """Publish complete Session-list and execution state when its view changes."""

    await websocket.accept()
    runtime = websocket.app.state.runtime
    previous = ""
    watchers = [runtime.runs, runtime.sessions]
    store = getattr(runtime.runs, "_store", None)
    if callable(getattr(store, "wait_for_change", None)):
        watchers.append(store)
    revisions = tuple(int(watcher.change_revision) for watcher in watchers)
    try:
        while True:
            sessions = await session_views(runtime)
            encoded = _host_change_key(sessions)
            if encoded != previous:
                previous = encoded
                await websocket.send_json({
                    "type": "host/sessions",
                    "sessions": sessions,
                })
            revisions = await _wait_for_change_or_disconnect(
                websocket, tuple(zip(watchers, revisions)),
            )
    except (WebSocketDisconnect, RuntimeError):
        return


__all__ = ["router"]
