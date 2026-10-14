"""Harness-style in-memory Session log with append-only write-behind storage."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterator

import zstandard

from bianbu_agent._internal.change_signal import AsyncRevisionSignal
from bianbu_agent.runs.contracts import RunResult, decode_result, encode_result
from bianbu_agent.runs.events import RunEvent
from bianbu_agent.runs.models import StoredRun
from bianbu_agent.sessions.events import (
    SESSION_EVENT_TYPES,
    SessionEvent,
    session_event_for_inbox_claim,
    session_event_for_inbox_splice,
    session_event_for_inbox_user_message,
    session_event_for_run_event,
    session_event_for_run_result,
    session_event_for_run_start,
    session_event_to_dict,
)
from bianbu_agent.sessions.inbox import (
    SessionInboxMessage,
    SessionInboxProjection,
    SessionInboxSnapshot,
    inbox_message_from_dict,
)
from bianbu_agent.sessions.projection import (
    SessionProjections,
    session_id_of,
    unit_row,
)

logger = logging.getLogger(__name__)


# 写入持续失败超过这个时长就不再接受新事件：程序内还能凑合，用户却以为
# 「发出去了」。宁可显式失败，也不要把未落盘的内容当成已确认。
_DURABLE_STALL_SECONDS = 5.0
_FINISH_TIMEOUT_SECONDS = 5.0


def _discard_task_exception(task: asyncio.Task[None]) -> None:
    """取走没人 await 的任务异常：超时后调用方已走，写批还会在后台失败重试。"""

    if not task.cancelled():
        task.exception()


class SessionLogWriteError(RuntimeError):
    """Session transcript 已停止接受写入，新事件没有任何持久化保证。

    按属性识别（见 `security_errors` 的同款约定），避免上层为一个可选实现
    建立硬依赖。
    """

    def __init__(
        self,
        session_id: str,
        *,
        cause: BaseException | None,
        durable_events: int,
    ) -> None:
        super().__init__(
            f"Session {session_id} 的事件日志无法写入：已持久保存 "
            f"{durable_events} 条事件后停止接收新内容；原因：{cause}"
        )
        self.session_id = session_id
        self.cause = cause
        self.session_log_durable_events = durable_events
        self.session_log_cause = str(cause) if cause is not None else ""


_TERMINAL_TYPES = frozenset({"run/completed", "run/failed", "run/cancelled"})

# Session transcripts are append-only and grow without bound, so the store
# never keeps a whole Session history in memory.  ``_TAIL_EVENTS`` bounds the
# always-resident live window of one Session; ``_HYDRATED_EVENT_BUDGET`` bounds
# the optional full-history caches (Session events and per-Run replay) used
# when a caller needs an older window or an exact event stream.  Eviction is
# least-recently-used and keeps at least the newest entry, so one Session above
# the budget stays cached instead of being re-read on every access.
_TAIL_EVENTS = 4096
_HYDRATED_EVENT_BUDGET = 120_000
_TEXT_COMMIT_PARTS = 256
_TEXT_COMMIT_CHARS = 1 << 16
_EVENT_KINDS = {
    "request/context": "request_context",
    "hook/invoked": "hook_invoked",
    "hook/result": "hook_result",
    "assistant/reasoning.delta": "reasoning",
    "assistant/text.delta": "token",
    "assistant/message.end": "message_end",
    "tool/call.started": "tool_start",
    "tool/call.progress": "tool_progress",
    "tool/call.completed": "tool_end",
    "tool/call.rejected": "tool_rejected",
    "approval/requested": "approval_required",
    "approval/resolved": "approval_resolved",
    "context/compressed": "context_compression",
    "agent/input.applied": "input_applied",
}


def _session_id(value: object) -> str:
    return session_id_of(value)


def _is_session_id(value: str) -> bool:
    try:
        session_id_of(value)
    except ValueError:
        return False
    return True


def _same_input(
    left: SessionInboxMessage,
    right: SessionInboxMessage,
) -> bool:
    return (
        left.message_id == right.message_id
        and left.content == right.content
        and tuple(dict(value) for value in left.attachments)
        == tuple(dict(value) for value in right.attachments)
        and left.skill_ids == right.skill_ids
    )


@dataclass(slots=True)
class _ListProjection:
    """The Session's own list row: is it still blank, and when was it prompted.

    Harness folds this as a per-event projection unit (``sessionListMetadata``):
    ``blank`` stays true until the Session's first turn starts, and
    ``lastPromptAt`` is the time of the first user-authored message.  The
    Session's ``updatedAt`` for list ordering is ``max(createdAt,
    lastPromptAt)``.

    One deviation: Harness keys ``blank`` off ``turn/start``, which our log
    does not write.  The durable start of a turn here is the accepted
    ``user/message`` itself, so that is what clears ``blank``.  Console
    operations (title generation) are Runs the conversation view hides, so
    their messages never count as user activity.
    """

    blank: bool = True
    last_prompt_at: float | None = None

    def observe(self, event: SessionEvent) -> None:
        if event.type != "user/message" or event.data.get("console_operation"):
            return
        self.blank = False
        if self.last_prompt_at is None or event.time > self.last_prompt_at:
            self.last_prompt_at = event.time

    def state(self) -> dict[str, object]:
        return {"blank": self.blank, "last_prompt_at": self.last_prompt_at}

    @classmethod
    def from_state(cls, raw: object) -> "_ListProjection | None":
        if not isinstance(raw, Mapping):
            return None
        blank = raw.get("blank")
        if not isinstance(blank, bool):
            return None
        last_prompt_at = raw.get("last_prompt_at")
        if last_prompt_at is not None and not isinstance(last_prompt_at, (int, float)):
            return None
        return cls(
            blank=blank,
            last_prompt_at=None if last_prompt_at is None else float(last_prompt_at),
        )


@dataclass(slots=True)
class _RunProjection:
    """Bounded in-memory view of one Run.

    A Run can carry hundreds of thousands of events, so the projection keeps
    only what Runtime itself reads back from a Run at rest: accumulated
    assistant text, applied steered input, pending approvals, dedupe keys and
    Run metadata.  The full event stream stays in the append-only Session
    transcript and is re-read on demand.
    """

    request_id: str
    session_id: str
    request: dict[str, object]
    result: RunResult | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    dedupe_keys: set[str] = field(default_factory=set)
    text: str = ""
    text_parts: list[str] = field(default_factory=list)
    applied_input: list[RunEvent] = field(default_factory=list)
    pending: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)

    def observe(self, event: RunEvent) -> None:
        """Fold one Run event into the bounded projection."""

        if event.kind == "token":
            self.text_parts.append(str(event.payload.get("text") or ""))
            if (
                len(self.text_parts) >= _TEXT_COMMIT_PARTS
                or sum(map(len, self.text_parts)) >= _TEXT_COMMIT_CHARS
            ):
                self.text = self.text + "".join(self.text_parts)
                self.text_parts.clear()
            return
        if event.kind == "input_applied":
            # Steered input is a small, bounded list and stays observable in
            # the projection; the rest of the stream does not.
            self.applied_input.append(event)
            return
        _track_pending_action(self.pending, event)

    def content(self) -> str:
        return self.text + "".join(self.text_parts)

    def control_events(self) -> tuple[RunEvent, ...]:
        """Synthesize the events Runtime projects out of a stored Run.

        Readers that only need Run status and pending actions (Session feeds,
        monitoring, listing) must not pay for the full event stream; the
        synthesized events preserve ``_pending_actions``, applied steered input
        and Run content.
        """

        events: list[RunEvent] = []
        text = self.content()
        if text:
            events.append(RunEvent("token", {"text": text}))
        events.extend(self.applied_input)
        events.extend(
            RunEvent(str(action.get("type") or "approval_required"), dict(action))
            for action in self.pending.values()
        )
        return tuple(events)

    def snapshot(self, events: tuple[RunEvent, ...] | None = None) -> StoredRun:
        return StoredRun(
            request_id=self.request_id,
            session_id=self.session_id,
            status="running" if self.result is None else self.result.status,
            request=dict(self.request),
            events=self.control_events() if events is None else tuple(events),
            result=self.result,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def state(self) -> dict[str, object]:
        """The projection as plain JSON, for the persisted projection cache."""

        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "request": dict(self.request),
            "result": None if self.result is None else encode_result(self.result),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dedupe_keys": sorted(self.dedupe_keys),
            "text": self.text,
            "text_parts": list(self.text_parts),
            "applied_input": [
                {"kind": str(event.kind), "payload": dict(event.payload)}
                for event in self.applied_input
            ],
            "pending": [
                [key[0], key[1], dict(action)]
                for key, action in self.pending.items()
            ],
        }

    @classmethod
    def from_state(cls, raw: object) -> "_RunProjection | None":
        """Rebuild one projection from cache state; ``None`` when unusable."""

        if not isinstance(raw, Mapping):
            return None
        try:
            request = raw.get("request")
            if not isinstance(request, Mapping):
                return None
            raw_result = raw.get("result")
            projection = cls(
                request_id=str(raw["request_id"]),
                session_id=str(raw["session_id"]),
                request=dict(request),
                result=None if raw_result is None else decode_result(raw_result),
                created_at=float(raw.get("created_at") or 0.0),
                updated_at=float(raw.get("updated_at") or 0.0),
            )
            projection.dedupe_keys = {
                str(key) for key in raw.get("dedupe_keys") or ()
            }
            projection.text = str(raw.get("text") or "")
            projection.text_parts = [
                str(part) for part in raw.get("text_parts") or ()
            ]
            projection.applied_input = [
                RunEvent(str(entry["kind"]), dict(entry["payload"]))
                for entry in raw.get("applied_input") or ()
            ]
            projection.pending = {
                (str(key[0]), str(key[1])): dict(key[2])
                for key in raw.get("pending") or ()
            }
        except Exception:  # noqa: BLE001 - 坏行当没有：缓存只是捷径
            return None
        return projection


def _track_pending_action(
    pending: dict[tuple[str, str], dict[str, object]],
    event: RunEvent,
) -> None:
    """Mirror the Runtime pending-action projection without keeping events."""

    if event.kind == "approval_required":
        action = {"type": event.kind, **dict(event.payload)}
        identifier = str(action.get("approval_id") or "")
        if identifier:
            pending[("approval", identifier)] = action
    elif event.kind == "approval_resolved":
        identifier = str(event.payload.get("approval_id") or "")
        pending.pop(("approval", identifier), None)


class _WriteBehind:
    """One Session's fixed-window ordered persistence queue."""

    def __init__(
        self,
        *,
        max_delay: float,
        write: Callable[[tuple[SessionEvent, ...]], Awaitable[None]],
    ) -> None:
        self._max_delay = float(max_delay)
        self._write = write
        self._pending: list[SessionEvent] = []
        self._timer: asyncio.Task[None] | None = None
        self._barrier: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._failed_since: float | None = None
        self._closed = False

    @property
    def has_work(self) -> bool:
        return bool(self._pending) or self._barrier is not None

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def failed_for(self) -> float:
        """已连续失败多久；从未失败或最近一次成功后为 0。"""

        if self._failure is None or self._failed_since is None:
            return 0.0
        return max(0.0, time.monotonic() - self._failed_since)

    def enqueue(self, event: SessionEvent) -> None:
        self._pending.append(event)
        if self._barrier is None:
            self._arm()

    def _arm(self) -> None:
        if self._timer is not None or self._closed:
            return
        self._timer = asyncio.create_task(self._after_delay())

    async def _after_delay(self) -> None:
        delay = self._max_delay if self._failure is None else max(.1, self._max_delay)
        await asyncio.sleep(delay)
        self._timer = None
        try:
            await self.flush()
        except Exception:
            pass

    async def _write_once(self) -> None:
        batch = tuple(self._pending)
        self._pending.clear()
        try:
            await self._write(batch)
            self._failure = None
            self._failed_since = None
        except BaseException as exc:
            self._pending[:0] = batch
            self._failure = exc
            if self._failed_since is None:
                self._failed_since = time.monotonic()
            raise

    async def flush(self) -> None:
        if self._barrier is None:
            self._barrier = asyncio.create_task(self._drain())
            self._barrier.add_done_callback(self._flushed)
        await asyncio.shield(self._barrier)

    def _flushed(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        self._barrier = None
        if self._pending:
            self._arm()

    async def _drain(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        while self._pending:
            await self._write_once()

    async def close(self) -> None:
        self._closed = True
        await self.flush()


class SessionLogStore:
    """Single-source Run facade over Harness-style Session transcripts."""

    def __init__(
        self,
        root: Path,
        *,
        write_batch_max_delay: float = 0.2,
        projection_flush_interval: float = 5.0,
    ) -> None:
        if write_batch_max_delay <= 0:
            raise ValueError("write_batch_max_delay must be positive")
        if projection_flush_interval < 0:
            raise ValueError("projection_flush_interval must not be negative")
        self.root = Path(root).expanduser().resolve()
        self.write_batch_max_delay = float(write_batch_max_delay)
        self.projection_flush_interval = float(projection_flush_interval)
        self._sessions: dict[str, deque[SessionEvent]] = {}
        self._counts: dict[str, int] = {}
        self._hydrated: OrderedDict[str, list[SessionEvent]] = OrderedDict()
        self._hydrated_runs: OrderedDict[str, dict[str, list[RunEvent]]] = OrderedDict()
        self._hydrated_events = 0
        self._runs: dict[str, _RunProjection] = {}
        self._lists: dict[str, _ListProjection] = {}
        self._inboxes: dict[str, SessionInboxProjection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # 折叠过的会话：这些会话的内存投影就是日志本身折出来的，别的会话只有
        # 缓存行喂出来的列表视图，真正的读写会先把它们折一遍。
        self._indexed: set[str] = set()
        self._index_locks: dict[str, asyncio.Lock] = {}
        self._projections = SessionProjections(self.root)
        self._flushed_at = 0.0
        self._pending_rows: set[str] = set()
        self._writers: dict[str, _WriteBehind] = {}
        self._durable_counts: dict[str, int] = {}
        self._write_errors: dict[str, tuple[SessionLogWriteError, float]] = {}
        # 终态提交任务：调用方只等一个预算，写入本身留在后台继续跑。没有强引用
        # 的任务会被回收，所以这里必须自己收着，并在 close 时等它收尾。
        self._result_tasks: set[asyncio.Task[None]] = set()
        # 提交信号：只在事件真正落盘后触发。订阅者据此判断「有新的已确认事件」，
        # 与「有事件进了内存」是两件事——后者可能永远不落盘。
        self._commits = AsyncRevisionSignal()
        self._opened = False

    async def open(self) -> None:
        if self._opened:
            return
        # Runtime 支持 stop→start 复用同一个 store。上一轮关闭过的写批器带着
        # `_closed=True`，不换掉的话新事件永远不自动落盘；残留的失败状态还会
        # 继续拦住写入，而已经没有待写批次能证明磁盘恢复了。
        self._writers.clear()
        self._write_errors.clear()
        self.root.mkdir(parents=True, exist_ok=True)
        # 启动不折叠任何 Session。读模型的 cell 由 `_ensure_indexed` 在首次触达
        # 时建；在那之前，持久投影行就是列表能看到的全部（Harness 的零 I/O 一级）。
        # 从前这里把每份 transcript 解压再折一遍，K3 上 175 个会话、114 万事件要
        # 63.7s，整个 HTTP 就绪都堵在它后面。
        await asyncio.to_thread(self._projections.load)
        self._seed_from_projections()
        self._commits.notify()
        self._opened = True

    async def close(self) -> None:
        if not self._opened:
            return
        await asyncio.gather(*tuple(self._result_tasks), return_exceptions=True)
        results = await asyncio.gather(
            *(writer.close() for writer in self._writers.values()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                # 关停时最后一次落盘失败必须留痕：它决定了重启后能看到什么。
                logger.error("关闭 Session 日志时落盘失败：%s", result)
        await self._flush_projections(force=True)
        self._opened = False

    def _required_open(self) -> None:
        if not self._opened:
            raise RuntimeError("Session log store is not open")

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _writer(self, session_id: str) -> _WriteBehind:
        writer = self._writers.get(session_id)
        if writer is None:
            writer = _WriteBehind(
                max_delay=self.write_batch_max_delay,
                write=lambda batch: self._persist_batch(session_id, batch),
            )
            self._writers[session_id] = writer
        return writer

    async def _live_run(self, request_id: str) -> _RunProjection:
        """The live projection of one Run, after its Session has been folded.

        A Run seeded from the projection cache is a listing shortcut, not an
        authority: the fold replaces the object it came from, so every caller
        must continue with the returned projection rather than the one it
        looked up.
        """

        run_id = str(request_id)
        projection = self._runs.get(run_id)
        if projection is None:
            raise KeyError(run_id)
        await self._ensure_indexed(projection.session_id)
        live = self._runs.get(run_id)
        if live is None:
            raise KeyError(run_id)
        return live

    # --- projection rows: seeding, first touch, flushing --------------------

    def _transcript_path(self, session_id: str) -> Path:
        return self.root / session_id / "session.jsonl.zstd"

    def _seed_from_projections(self) -> None:
        """Feed the list views from the projection files alone.

        No transcript is read, so a listing costs one small JSON per Session.
        Every value here is a fold shortcut: a stale Run status or event count
        is old, never wrong, and the Session's own first touch replaces it with
        the log's answer.
        """

        for session_id in self._projections.rows():
            listed = _ListProjection.from_state(
                self._projections.unit(session_id, "list")
            )
            if listed is not None:
                self._lists[session_id] = listed
            count = self._projections.unit(session_id, "count")
            if isinstance(count, int) and count >= 0:
                self._counts[session_id] = count
                self._durable_counts[session_id] = count
            inbox = SessionInboxProjection.from_state(
                self._projections.unit(session_id, "inbox")
            )
            if inbox is not None:
                self._inboxes[session_id] = inbox
            runs = self._projections.unit(session_id, "runs")
            if isinstance(runs, list):
                for raw in runs:
                    projection = _RunProjection.from_state(raw)
                    if projection is not None:
                        self._runs[projection.request_id] = projection

    def _forget_session(self, session_id: str) -> None:
        """Drop one Session's in-memory projections, from the cache or the log."""

        for run_id in [
            run_id
            for run_id, run in self._runs.items()
            if run.session_id == session_id
        ]:
            self._runs.pop(run_id, None)
        self._sessions.pop(session_id, None)
        self._counts.pop(session_id, None)
        self._durable_counts.pop(session_id, None)
        self._lists.pop(session_id, None)
        self._inboxes.pop(session_id, None)
        hydrated = self._hydrated.pop(session_id, None)
        if hydrated is not None:
            self._hydrated_events -= len(hydrated)
        self._hydrated_runs.pop(session_id, None)

    async def _ensure_indexed(self, session_id: str) -> None:
        """Fold one Session's transcript, once per process. The log is authority.

        Anything the persisted rows put in memory for this Session is discarded
        first: a row that disagrees with the transcript must lose.
        """

        if session_id in self._indexed:
            return
        lock = self._index_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if session_id in self._indexed:
                return
            self._forget_session(session_id)
            path = self._transcript_path(session_id)
            if path.is_file():
                await asyncio.to_thread(self._index_transcript, path)
            else:
                # 还没有日志的会话就是「空会话」：这条事实不需要读文件，但它
                # 必须和折出来的会话一样有列表行，否则第一次 prompt 之前列表
                # 只能回答「不知道」。
                self._lists[session_id] = _ListProjection()
            self._indexed.add(session_id)
            # 折出来的答案可能比投影行新（上一次崩溃留下了没来得及写进去的
            # 尾巴，或撕裂帧被截断），这一行要跟着前进。
            self._pending_rows.add(session_id)
            await self._flush_projections()

    def _exact_count(self, session_id: str) -> int | None:
        """The Session's event count when it is provably current, else ``None``.

        A transcript only ever grows, so a row whose recorded byte size still
        matches the file describes a log with no newer frame in it — provided
        every unit it carries is still readable at its live version.  Together
        those make the row's answer the log's answer, which the host push and
        the tail reads need without folding every Session on the device.
        """

        if session_id in self._indexed:
            return self._counts.get(session_id, 0)
        if not self._projections.row_is_current(session_id):
            return None
        row = self._projections.row(session_id)
        count = self._projections.unit(session_id, "count")
        if row is None or not isinstance(count, int):
            return None
        try:
            size = self._transcript_path(session_id).stat().st_size
        except OSError:
            return None
        if size != row.get("size"):
            return None
        return count

    def _stage_row(self, session_id: str) -> None:
        """Refresh one Session's folded row from the live projections."""

        try:
            size = self._transcript_path(session_id).stat().st_size
        except OSError:
            return
        count = self._counts.get(session_id, 0)
        runs = [
            run.state()
            for run in self._runs.values()
            if run.session_id == session_id
        ]
        listed = self._lists.get(session_id)
        inbox = self._inboxes.get(session_id)
        self._projections.stage_row(
            session_id,
            {
                "size": size,
                "seq": count - 1,
                "units": {
                    "count": unit_row("count", seq=count - 1, value=count),
                    "list": unit_row(
                        "list",
                        seq=count - 1,
                        value=None if listed is None else listed.state(),
                    ),
                    "inbox": unit_row(
                        "inbox",
                        seq=count - 1,
                        value=None if inbox is None else inbox.to_state(),
                    ),
                    "runs": unit_row("runs", seq=count - 1, value=runs),
                },
            },
        )

    async def _flush_projections(self, *, force: bool = False) -> None:
        """Write the staged projection files, throttled unless forced."""

        if not self._pending_rows and not self._projections.dirty:
            return
        now = time.monotonic()
        if not force and now - self._flushed_at < self.projection_flush_interval:
            return
        for session_id in tuple(self._pending_rows):
            self._stage_row(session_id)
        self._pending_rows.clear()
        if not self._projections.dirty:
            return
        try:
            await asyncio.to_thread(self._projections.flush)
        except Exception as exc:  # noqa: BLE001 - 投影写失败只让它落后，不挡写入
            logger.warning("Session projection write failed: %s", exc)
            return
        self._flushed_at = now

    async def start(self, request_id: str, session_id: str, request: dict) -> None:
        self._required_open()
        run_id = str(request_id).strip()
        owner = _session_id(session_id)
        await self._ensure_indexed(owner)
        failure = self.write_failure(owner)
        if failure is not None:
            raise failure
        async with self._lock(owner):
            existing = self._runs.get(run_id)
            if existing is not None:
                if existing.session_id != owner:
                    raise ValueError(f"run already exists for another session: {run_id}")
                return
            now = time.time()
            projection = _RunProjection(
                request_id=run_id,
                session_id=owner,
                request=dict(request),
                created_at=now,
                updated_at=now,
            )
            raw_messages = request.get("input_messages")
            if isinstance(raw_messages, list) and raw_messages:
                messages = tuple(
                    inbox_message_from_dict(raw)
                    for raw in raw_messages
                    if isinstance(raw, dict)
                )
                if len(messages) != len(raw_messages):
                    raise ValueError("input_messages must contain only objects")
                inbox = self._inboxes.setdefault(owner, SessionInboxProjection())
                claimable = inbox.claimable()
                if messages != claimable[:len(messages)]:
                    raise ValueError("Run input does not match the Session inbox claim")
                message_ids = tuple(message.message_id for message in messages)
                base_seq = self._counts.get(owner, 0)
                events = (
                    session_event_for_inbox_claim(
                        owner,
                        run_id,
                        message_ids,
                        seq=base_seq,
                        event_time=now,
                    ),
                    *(
                        session_event_for_inbox_user_message(
                            owner,
                            run_id,
                            message,
                            seq=base_seq + index + 1,
                            event_time=now,
                            request=request if index == 0 else None,
                        )
                        for index, message in enumerate(messages)
                    ),
                )
                await self._accept_durable_batch(owner, events)
                inbox.apply_claim(message_ids, run_id=run_id)
                self._runs[run_id] = projection
                return
            self._runs[run_id] = projection
            await self._accept(
                owner,
                session_event_for_run_start(
                    owner,
                    run_id,
                    request,
                    seq=self._counts.get(owner, 0),
                    event_time=now,
                ),
            )

    async def append(self, request_id: str, event: RunEvent) -> None:
        self._required_open()
        projection = await self._live_run(request_id)
        run_id = projection.request_id
        failure = self.write_failure(projection.session_id)
        if failure is not None:
            raise failure
        async with self._lock(projection.session_id):
            if event.kind == "input_applied":
                await self._append_applied_input(projection, event)
                return
            projection.observe(event)
            projection.updated_at = time.time()
            await self._accept(
                projection.session_id,
                session_event_for_run_event(
                    projection.session_id,
                    run_id,
                    event,
                    seq=self._counts.get(projection.session_id, 0),
                    event_time=projection.updated_at,
                ),
            )

    async def _append_applied_input(
        self,
        projection: _RunProjection,
        event: RunEvent,
    ) -> None:
        session_id = projection.session_id
        message_id = str(event.payload.get("message_id") or "").strip()
        inbox = self._inboxes.setdefault(session_id, SessionInboxProjection())
        message = inbox.known(message_id)
        if message is None:
            raise ValueError(f"applied input is not in the Session inbox: {message_id}")
        pending = inbox.snapshot().next_step
        if not pending or pending[0].message_id != message_id:
            raise ValueError(f"applied input is not the next-step prefix: {message_id}")
        now = time.time()
        base_seq = self._counts.get(session_id, 0)
        applied = session_event_for_run_event(
            session_id,
            projection.request_id,
            event,
            seq=base_seq,
            event_time=now,
        )
        claim = session_event_for_inbox_claim(
            session_id,
            projection.request_id,
            (message_id,),
            seq=base_seq + 1,
            event_time=now,
        )
        user = session_event_for_inbox_user_message(
            session_id,
            projection.request_id,
            message,
            seq=base_seq + 2,
            event_time=now,
        )
        await self._accept_durable_batch(session_id, (applied, claim, user))
        projection.observe(event)
        projection.updated_at = now
        inbox.apply_claim((message_id,), run_id=projection.request_id)

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
        projection = await self._live_run(request_id)
        failure = self.write_failure(projection.session_id)
        if failure is not None:
            raise failure
        async with self._lock(projection.session_id):
            if selected_key in projection.dedupe_keys:
                return False
            projection.dedupe_keys.add(selected_key)
            projection.observe(event)
            projection.updated_at = time.time()
            base = session_event_for_run_event(
                projection.session_id,
                projection.request_id,
                event,
                seq=self._counts.get(projection.session_id, 0),
                event_time=projection.updated_at,
            )
            await self._accept(
                projection.session_id,
                SessionEvent(
                    base.session_id,
                    base.seq,
                    base.type,
                    base.time,
                    base.run_id,
                    {**dict(base.data), "_dedupe_key": selected_key},
                ),
            )
            return True

    async def finish(self, request_id: str, result: RunResult) -> None:
        self._required_open()
        projection = await self._live_run(request_id)
        task = asyncio.ensure_future(self._commit_result(projection, result))
        self._result_tasks.add(task)
        task.add_done_callback(self._result_tasks.discard)
        task.add_done_callback(_discard_task_exception)
        try:
            # 期限只约束「调用方等多久」：等锁和落盘都在后台任务里，超时只放弃等待，
            # 不取消写入。终态此时已经在内存里可见，落盘由写批继续重试。
            await asyncio.wait_for(
                asyncio.shield(task), _FINISH_TIMEOUT_SECONDS
            )
        except Exception:
            # 终态没落盘不等于 Run 失败：这是内存优先语义下明确接受的取舍，界面上
            # 该 Run 仍然是它真实的结果，而「还没确认保存」由 persistence 单独上报，
            # 真实写失败会带原因出现在那里，不会在这里被升级成 Run 失败。
            logger.warning(
                "Run %s 的终态尚未确认落盘，写入会在后台继续重试", request_id,
                exc_info=True,
            )

    async def _commit_result(
        self,
        projection: _RunProjection,
        result: RunResult,
    ) -> None:
        """把终态写进内存投影，随后等一次落盘确认；失败由写批重试接手。"""

        session_id = projection.session_id
        async with self._lock(session_id):
            if projection.result is None:
                projection.result = result
                projection.updated_at = time.time()
                await self._accept(
                    session_id,
                    session_event_for_run_result(
                        session_id,
                        result,
                        seq=self._counts.get(projection.session_id, 0),
                        event_time=projection.updated_at,
                    ),
                )
        await self.flush_session(session_id)

    async def admit_input(
        self,
        session_id: str,
        message: SessionInboxMessage,
    ) -> bool:
        """Durably admit one idempotent input before exposing it as pending."""

        self._required_open()
        owner = _session_id(session_id)
        await self._ensure_indexed(owner)
        async with self._lock(owner):
            inbox = self._inboxes.setdefault(owner, SessionInboxProjection())
            existing = inbox.known(message.message_id)
            if existing is not None:
                if _same_input(existing, message):
                    return False
                raise ValueError(
                    f"message_id {message.message_id!r} already identifies "
                    "different input"
                )
            event = session_event_for_inbox_splice(
                owner,
                message,
                seq=self._counts.get(owner, 0),
                event_time=time.time(),
            )
            await self._accept_durable(owner, event)
            inbox.apply_splice(message)
            return True

    async def claim_input(
        self,
        session_id: str,
        *,
        run_id: str,
    ) -> tuple[SessionInboxMessage, ...]:
        """Claim the complete durable next-step prefix for one boundary."""

        self._required_open()
        owner = _session_id(session_id)
        selected_run_id = str(run_id).strip()
        if not selected_run_id:
            raise ValueError("run_id must not be empty")
        await self._ensure_indexed(owner)
        async with self._lock(owner):
            inbox = self._inboxes.setdefault(owner, SessionInboxProjection())
            claimed = inbox.claimable()
            if not claimed:
                return ()
            message_ids = tuple(message.message_id for message in claimed)
            event = session_event_for_inbox_claim(
                owner,
                selected_run_id,
                message_ids,
                seq=self._counts.get(owner, 0),
                event_time=time.time(),
            )
            await self._accept_durable(owner, event)
            inbox.apply_claim(message_ids, run_id=selected_run_id)
            return claimed

    async def pending_input(self, session_id: str) -> SessionInboxSnapshot:
        self._required_open()
        owner = _session_id(session_id)
        if self._exact_count(owner) is None:
            await self._ensure_indexed(owner)
        async with self._lock(owner):
            return self._inboxes.setdefault(
                owner, SessionInboxProjection()
            ).snapshot()

    async def input_run_id(self, session_id: str, message_id: str) -> str | None:
        self._required_open()
        owner = _session_id(session_id)
        await self._ensure_indexed(owner)
        async with self._lock(owner):
            return self._inboxes.setdefault(
                owner, SessionInboxProjection()
            ).claimed_run(message_id)

    async def _accept(self, session_id: str, event: SessionEvent) -> None:
        expected = self._counts.get(session_id, 0)
        if event.seq != expected:
            raise ValueError(
                f"session event seq mismatch: expected {expected}, got {event.seq}"
            )
        self._retain(session_id, event)
        self._writer(session_id).enqueue(event)

    async def _accept_durable(self, session_id: str, event: SessionEvent) -> None:
        """Persist one event before mutating the live Session projection."""

        await self._accept_durable_batch(session_id, (event,))

    async def _accept_durable_batch(
        self,
        session_id: str,
        batch: tuple[SessionEvent, ...],
    ) -> None:
        writer = self._writers.get(session_id)
        if writer is not None:
            await writer.flush()
        if not batch:
            return
        expected = self._counts.get(session_id, 0)
        if batch[0].seq != expected or any(
            event.seq != batch[0].seq + index
            for index, event in enumerate(batch)
        ):
            raise ValueError(
                f"session event seq mismatch: expected {expected}, "
                f"got {batch[0].seq}"
            )
        await self._persist_batch(session_id, batch)
        for event in batch:
            self._retain(session_id, event)

    async def get(self, request_id: str) -> StoredRun | None:
        projection = self._runs.get(str(request_id))
        if projection is None:
            return None
        return projection.snapshot(await self._run_events(projection))

    async def list(self, session_id: str | None = None) -> list[StoredRun]:
        """List Run records from the bounded projection.

        Every reader of a listed Run draws on status, request and pending
        actions, and ``_RunProjection`` already keeps all three.  The full event
        stream stays in the transcript and is re-read only by :meth:`get`, so a
        Session-filtered listing costs the number of Runs rather than the
        number of events: it used to materialize a whole Session transcript to
        answer a Session status view, which took tens of seconds and hundreds
        of megabytes on a long Session.
        """

        selected = None if session_id is None else str(session_id)
        if selected is not None and _is_session_id(selected):
            # 行记录的文件字节数和日志一致时，行里的 Run 就是日志的答案，不必为
            # 一次列举把整份 transcript 折一遍——启动恢复正是逐个会话问这一句。
            if self._exact_count(selected) is None:
                await self._ensure_indexed(selected)
        projections = [
            run
            for run in self._runs.values()
            if selected is None or run.session_id == selected
        ]
        records = [run.snapshot() for run in projections]
        return sorted(records, key=lambda item: (item.created_at, item.request_id))

    async def read_session_events(
        self,
        session_id: str,
        *,
        from_seq: int = 0,
        before_seq: int | None = None,
    ) -> tuple[SessionEvent, ...]:
        """Return the requested slice of a Session transcript.

        A transcript is append-only and contiguous, so ``seq`` is the position
        in the in-memory list and a window read is a slice rather than a scan.
        Both entry points into the list (transcript hydration and batch append)
        reject a gap with ``ValueError``, and the bounds check below keeps a log
        that somehow lost the invariant on the slow path.
        """

        if from_seq < 0 or (before_seq is not None and before_seq < 0):
            raise ValueError("Session event positions must not be negative")
        owner = _session_id(session_id)
        # 完全落在水位之上的窗口不需要日志：行里的字节数和文件一致就说明没有
        # 新帧，答案已经确定；否则（崩溃留下的尾巴、被截断的撕裂帧）先折一遍。
        exact = self._exact_count(owner)
        if exact is None or before_seq is not None or from_seq < exact:
            await self._ensure_indexed(owner)
        count = self._counts.get(owner, 0)
        start = min(from_seq, count)
        stop = count if before_seq is None else min(before_seq, count)
        if stop <= start:
            return ()
        retained = self._sessions.get(owner)
        base = count - len(retained) if retained else count
        if retained and start >= base:
            return tuple(list(retained)[start - base:stop - base])
        # Older than the resident window: fall back to the full transcript.
        events = await self._session_events(owner)
        return tuple(event for event in events if start <= event.seq < stop)

    async def session_event_tail(self, session_id: str) -> int | None:
        owner = _session_id(session_id)
        if self._exact_count(owner) is None:
            await self._ensure_indexed(owner)
        count = self._counts.get(owner, 0)
        return count - 1 if count else None

    def list_metadata(self, session_id: str) -> Mapping[str, object]:
        """The Session's list row, without folding anything.

        Harness serves a listing from its projection columns: a live Session
        answers from the in-memory registry, a cold one from the persisted
        cache, and a Session with neither is *unknown* rather than hidden.  The
        same ladder applies here — the folded value when this Session has been
        indexed, the persisted row when its byte size still proves it current,
        and otherwise the honest "blank, never prompted" answer for a Session
        whose transcript does not exist yet.  ``as_of_seq`` is the watermark
        the answer was read at, which is what tells a caller how stale it is.
        """

        owner = _session_id(session_id)
        if owner in self._indexed:
            folded = self._lists.get(owner)
            return {
                "blank": True if folded is None else folded.blank,
                "last_prompt_at": None if folded is None else folded.last_prompt_at,
                "as_of_seq": self._counts.get(owner, 0) - 1,
            }
        if self._exact_count(owner) is not None:
            row = self._projections.row(owner)
            listed = _ListProjection.from_state(self._projections.unit(owner, "list"))
            if listed is not None:
                return {
                    "blank": listed.blank,
                    "last_prompt_at": listed.last_prompt_at,
                    "as_of_seq": None if row is None else row.get("seq"),
                }
        return {
            "blank": not self._transcript_path(owner).is_file(),
            "last_prompt_at": None,
            "as_of_seq": None,
        }

    def has_projection_row(self, session_id: str) -> bool:
        """Whether the persisted rows say anything about this Session.

        Harness folds a Session's projections on first touch and never at
        startup, and a start is not allowed to read a transcript nobody has
        touched: a lost table must not turn into a startup fold.  A row is the
        only evidence a start has, so a Session without one waits for the touch
        that will fold it -- the same touch that admits new input.
        """

        try:
            owner = _session_id(session_id)
        except ValueError:
            return False
        return self._projections.row(owner) is not None

    def durable_events(self, session_id: str) -> int:
        """该 Session 已持久提交的事件条数。这是可用性指标，不是可读水位。"""

        return self._durable_counts.get(str(session_id), 0)

    @property
    def change_revision(self) -> int:
        return self._commits.revision

    async def wait_for_change(self, after: int) -> int:
        """等待下一次提交。只在事件落盘后返回，因此唤醒即代表已保存条数前进。

        订阅方用它尽快发现「持久化从失败恢复」，而不是每次落盘都重推列表：
        `pending`/`saved` 与条数抖动是实时写入的常态，推给界面只会让它白重画
        （可见性本身读内存即可）。"""

        return await self._commits.wait(after)

    def write_failure(self, session_id: str) -> SessionLogWriteError | None:
        """写入长时间失败时的显式错误；短暂抖动仍交给写批重试自愈。"""

        recorded = self._write_errors.get(str(session_id))
        if recorded is not None and time.monotonic() - recorded[1] >= _DURABLE_STALL_SECONDS:
            return recorded[0]
        writer = self._writers.get(str(session_id))
        if writer is None or writer.failure is None:
            return None
        if writer.failed_for < _DURABLE_STALL_SECONDS:
            return None
        return SessionLogWriteError(
            str(session_id),
            cause=writer.failure,
            durable_events=self.durable_events(session_id),
        )

    def _record_write_failure(self, session_id: str, cause: BaseException) -> SessionLogWriteError:
        failure = SessionLogWriteError(
            session_id, cause=cause, durable_events=self.durable_events(session_id),
        )
        previous = self._write_errors.get(session_id)
        since = previous[1] if previous else time.monotonic()
        self._write_errors[session_id] = (failure, since)
        reason_changed = previous is None or str(previous[0]) != str(failure)
        # 第一次失败时界面只知道「还没保存」（pending）；持续失败跨过阈值的那一
        # 刻必须再通知一次，否则订阅方永远等不到那条带原因的 error。
        crossed_stall = (
            previous is not None
            and time.monotonic() - since >= _DURABLE_STALL_SECONDS
        )
        if reason_changed or crossed_stall:
            self._commits.notify()
        return failure

    def persistence_state(self, session_id: str) -> dict[str, object]:
        from bianbu_agent.security.security_errors import classify_run_exception

        committed = self.durable_events(session_id)
        # The resident window is bounded, so the appended total comes from the
        # index count rather than the number of events still held in memory.
        pending = max(0, self._counts.get(str(session_id), 0) - committed)
        # 用与「停收」同一个判据：一次能自愈的抖动只算 pending（还没保存），
        # 持续失败才升级成带原因的 error，避免界面为 200ms 的抖动闪横幅。
        failure = self.write_failure(session_id)
        return {
            "state": "error" if failure is not None else "pending" if pending else "saved",
            "durable_events": committed,
            "pending_events": pending,
            "error": None if failure is None else classify_run_exception(failure),
        }

    async def flush_session(self, session_id: str) -> None:
        writer = self._writers.get(str(session_id))
        if writer is not None:
            await writer.flush()
        if str(session_id) in self._pending_rows:
            # 会话收尾是必须写的一刻：调用方等的是「这一轮的答案下一次启动还能
            # 看到」，而不是「过一会儿会写进去」。
            await self._flush_projections(force=True)

    async def delete(self, request_id: str) -> bool:
        # An individual historical range cannot be removed from an append-only
        # Session transcript.  Public callers must delete the owning Session.
        return False

    async def delete_session(self, session_id: str) -> int:
        selected = _session_id(session_id)
        await self.flush_session(selected)
        run_ids = [
            run_id for run_id, run in self._runs.items()
            if run.session_id == selected
        ]
        for run_id in run_ids:
            self._runs.pop(run_id, None)
        self._sessions.pop(selected, None)
        self._counts.pop(selected, None)
        hydrated = self._hydrated.pop(selected, None)
        if hydrated is not None:
            self._hydrated_events -= len(hydrated)
        self._hydrated_runs.pop(selected, None)
        self._lists.pop(selected, None)
        self._inboxes.pop(selected, None)
        self._durable_counts.pop(selected, None)
        self._write_errors.pop(selected, None)
        self._writers.pop(selected, None)
        self._indexed.discard(selected)
        self._index_locks.pop(selected, None)
        self._pending_rows.discard(selected)
        self._projections.forget(selected)
        await asyncio.to_thread(shutil.rmtree, self.root / selected, True)
        await self._flush_projections(force=True)
        self._commits.notify()
        return len(run_ids)

    async def _persist_batch(
        self,
        session_id: str,
        batch: tuple[SessionEvent, ...],
    ) -> None:
        if not batch:
            return
        expected = self._durable_counts.get(session_id, 0)
        if batch[0].seq != expected:
            raise ValueError(
                f"durable seq mismatch for {session_id}: expected {expected}, "
                f"got {batch[0].seq}"
            )
        try:
            await asyncio.to_thread(self._append_transcript, session_id, batch, expected == 0)
        except Exception as exc:
            self._record_write_failure(session_id, exc)
            raise
        self._durable_counts[session_id] = expected + len(batch)
        self._write_errors.pop(session_id, None)
        # 行跟着日志走：先落盘的数据永远是权威，缓存只是列表的捷径。
        self._pending_rows.add(session_id)
        await self._flush_projections(
            force=any(event.type in _TERMINAL_TYPES for event in batch)
        )
        self._commits.notify()

    def _append_transcript(
        self,
        session_id: str,
        batch: tuple[SessionEvent, ...],
        first: bool,
    ) -> None:
        directory = self.root / session_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "session.jsonl.zstd"
        compressor = zstandard.ZstdCompressor(write_checksum=True)

        def encode(lines: list[str]) -> bytes:
            return compressor.compress(("\n".join(lines) + "\n").encode("utf-8"))

        if first:
            # The header owns the first frame alone: Harness's reader refuses a
            # first frame that is not exactly one header line, and a
            # metadata-only listing only has to decode that one frame.
            header = json.dumps({
                "type": "session",
                "version": 1,
                "id": session_id,
                "created_at": batch[0].time,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            frame = encode([header]) + encode(_transcript_documents(batch))
        else:
            frame = encode(_transcript_documents(batch))
        if first:
            descriptor, name = tempfile.mkstemp(prefix=".session-", dir=directory)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(frame)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    # 上一次「链接成功但目录 fsync 失败」留下的重试。同一批事件写出的
                    # 字节必须完全相同，才能确认这就是那一次，补做目录同步即可；
                    # 否则说明提交水位和磁盘对不上，不能当成幂等重试糊过去。
                    if path.read_bytes() != frame:
                        raise ValueError(
                            f"Session transcript already exists with different "
                            f"content: {path}"
                        ) from None
                self._fsync_directory(directory)
            finally:
                temporary.unlink(missing_ok=True)
            return
        before = path.stat().st_size
        try:
            with path.open("ab") as stream:
                stream.write(frame)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            with path.open("r+b") as stream:
                stream.truncate(before)
                stream.flush()
                os.fsync(stream.fileno())
            raise

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_transcript(self, path: Path) -> tuple[str, tuple[SessionEvent, ...]]:
        """Read every event of one transcript, for callers that need all of it."""

        session_id, events = self._open_transcript(path)
        return session_id, tuple(events)

    def _rebuild_runs(
        self,
        session_id: str,
        events: Iterator[SessionEvent] | tuple[SessionEvent, ...],
    ) -> None:
        for event in events:
            self._project_run_event(session_id, event)

    def _project_run_event(self, session_id: str, event: SessionEvent) -> None:
        """Fold one persisted Session event into its Run projection."""

        if event.type.startswith("agent/inbox/"):
            return
        run_id = str(event.run_id or "")
        if not run_id:
            return
        if event.type == "user/message":
            if run_id in self._runs:
                return
            raw_request = event.data.get("request")
            request = dict(raw_request) if isinstance(raw_request, dict) else {
                "prompt": str(event.data.get("content") or ""),
                "thread_id": str(event.data.get("thread_id") or session_id),
                "metadata": {},
            }
            self._runs[run_id] = _RunProjection(
                request_id=run_id,
                session_id=session_id,
                request=request,
                created_at=event.time,
                updated_at=event.time,
            )
            return
        projection = self._runs.get(run_id)
        if projection is None:
            raise ValueError(f"Session event references unknown Run: {run_id}")
        projection.updated_at = event.time
        if event.type in _TERMINAL_TYPES:
            raw_result = event.data.get("result")
            if not isinstance(raw_result, dict):
                raise ValueError(f"terminal Session event lacks Run result: {run_id}")
            projection.result = decode_result(raw_result)
            return
        run_event = _run_event(event)
        if run_event is not None:
            projection.observe(run_event)
            key = str(event.data.get("_dedupe_key") or "")
            if key:
                projection.dedupe_keys.add(key)

    def _project_inbox_event(
        self,
        inbox: SessionInboxProjection,
        session_id: str,
        event: SessionEvent,
    ) -> None:
        if event.type == "agent/inbox/spliced":
            raw = event.data.get("message")
            if not isinstance(raw, dict):
                raise ValueError(
                    f"invalid persisted inbox splice in Session {session_id}"
                )
            inbox.apply_splice(inbox_message_from_dict(raw))
        elif event.type == "agent/inbox/claimed":
            raw_ids = event.data.get("message_ids")
            if not isinstance(raw_ids, list) or any(
                not isinstance(message_id, str) for message_id in raw_ids
            ):
                raise ValueError(
                    f"invalid persisted inbox claim in Session {session_id}"
                )
            inbox.apply_claim(tuple(raw_ids), run_id=str(event.run_id or ""))

    def _retain(self, session_id: str, event: SessionEvent) -> None:
        """Append one live event to the resident window and its projections."""

        tail = self._sessions.get(session_id)
        if tail is None:
            tail = deque(maxlen=_TAIL_EVENTS)
            self._sessions[session_id] = tail
        tail.append(event)
        self._counts[session_id] = event.seq + 1
        listed = self._lists.get(session_id)
        if listed is None:
            listed = _ListProjection()
            self._lists[session_id] = listed
        listed.observe(event)
        hydrated = self._hydrated.get(session_id)
        if hydrated is not None:
            hydrated.append(event)
            self._hydrated_events += 1
            self._evict_hydrated()
        if session_id in self._hydrated_runs:
            # A live Run replay must observe events appended after the
            # transcript was materialized.
            self._hydrated_run_events(session_id, event)
            self._evict_replay()

    def _hydrated_run_events(self, session_id: str, event: SessionEvent) -> None:
        map_ = self._hydrated_runs.setdefault(session_id, {})
        run_id = str(event.run_id or "")
        if not run_id or event.type.startswith("agent/inbox/"):
            return
        run_event = _run_event(event)
        if run_event is not None:
            map_.setdefault(run_id, []).append(run_event)

    async def _session_events(self, session_id: str) -> list[SessionEvent]:
        """Full Session transcript, cached under a bounded event budget."""

        hydrated = self._hydrated.get(session_id)
        if hydrated is not None:
            self._hydrated.move_to_end(session_id)
            return hydrated
        path = self.root / session_id / "session.jsonl.zstd"
        if not path.exists():
            return []
        async with self._lock(session_id):
            hydrated = self._hydrated.get(session_id)
            if hydrated is not None:
                self._hydrated.move_to_end(session_id)
                return hydrated
            loaded_id, loaded = await asyncio.to_thread(
                self._read_transcript, path
            )
            if loaded_id != session_id:
                return []
            events = list(loaded)
            count = self._counts.get(session_id, len(events))
            if count > len(events):
                known = {event.seq for event in events}
                pending = [
                    event
                    for event in self._sessions.get(session_id, ())
                    if event.seq not in known
                ]
                pending.sort(key=lambda event: event.seq)
                events.extend(pending)
            self._hydrated[session_id] = events
            self._hydrated_events += len(events)
            self._counts[session_id] = len(events)
            self._sessions[session_id] = deque(
                events[-_TAIL_EVENTS:], maxlen=_TAIL_EVENTS
            )
            self._evict_hydrated()
            return events

    def _evict_hydrated(self) -> None:
        """Drop least-recently used full transcripts above the event budget."""

        while self._hydrated_events > _HYDRATED_EVENT_BUDGET and len(self._hydrated) > 1:
            session_id, events = next(iter(self._hydrated.items()))
            self._hydrated.pop(session_id, None)
            self._hydrated_runs.pop(session_id, None)
            self._hydrated_events -= len(events)

    async def _session_run_events(
        self,
        session_id: str,
    ) -> dict[str, list[RunEvent]]:
        """Per-Run replay events, materialized without the Session event list."""

        cached = self._hydrated_runs.get(session_id)
        if cached is not None:
            self._hydrated_runs.move_to_end(session_id)
            return cached
        path = self.root / session_id / "session.jsonl.zstd"
        if not path.exists():
            return {}
        async with self._lock(session_id):
            cached = self._hydrated_runs.get(session_id)
            if cached is not None:
                self._hydrated_runs.move_to_end(session_id)
                return cached
            loaded_id, count, grouped = await asyncio.to_thread(
                self._build_session_replay, path
            )
            if loaded_id != session_id:
                return {}
            total = self._counts.get(session_id, count)
            if total > count:
                for event in self._sessions.get(session_id, ()):
                    if event.seq < count:
                        continue
                    self._append_replay_event(grouped, event)
            self._hydrated_runs[session_id] = grouped
            self._evict_replay()
            return grouped

    def _evict_replay(self) -> None:
        """Bound the per-Run replay cache while keeping the newest Session."""

        # The size is summed on demand: the number of Runs per Session is small
        # next to the number of events, and a cached counter would need
        # bookkeeping on every eviction path.
        total = sum(
            len(items)
            for grouped in self._hydrated_runs.values()
            for items in grouped.values()
        )
        while total > _HYDRATED_EVENT_BUDGET and len(self._hydrated_runs) > 1:
            session_id, grouped = next(iter(self._hydrated_runs.items()))
            self._hydrated_runs.pop(session_id, None)
            total -= sum(len(items) for items in grouped.values())

    def _append_replay_event(
        self,
        grouped: dict[str, list[RunEvent]],
        event: SessionEvent,
    ) -> None:
        run_id = str(event.run_id or "")
        if not run_id or event.type.startswith("agent/inbox/"):
            return
        run_event = _run_event(event)
        if run_event is not None:
            grouped.setdefault(run_id, []).append(run_event)

    def _build_session_replay(
        self,
        path: Path,
    ) -> tuple[str, int, dict[str, list[RunEvent]]]:
        session_id, events = self._open_transcript(path)
        count = 0
        grouped: dict[str, list[RunEvent]] = {}
        for event in events:
            count += 1
            self._append_replay_event(grouped, event)
        return session_id, count, grouped

    async def _run_events(self, projection: _RunProjection) -> tuple[RunEvent, ...]:
        runs = await self._session_run_events(projection.session_id)
        return tuple(runs.get(projection.request_id, ()))

    def _index_transcript(self, path: Path) -> None:
        """Index one transcript without keeping its events in memory."""

        session_id, events = self._open_transcript(path)
        inbox = SessionInboxProjection()
        listed = _ListProjection()
        tail: deque[SessionEvent] = deque(maxlen=_TAIL_EVENTS)
        count = 0
        for event in events:
            count += 1
            tail.append(event)
            listed.observe(event)
            self._project_inbox_event(inbox, session_id, event)
            self._project_run_event(session_id, event)
        self._sessions[session_id] = tail
        self._counts[session_id] = count
        self._durable_counts[session_id] = count
        self._lists[session_id] = listed
        self._inboxes[session_id] = inbox

    def _open_transcript(
        self,
        path: Path,
    ) -> tuple[str, Iterator[SessionEvent]]:
        """Validate one transcript header and stream its events.

        The header is read eagerly so every caller learns the Session id, while
        the events stay lazy and are decoded one document at a time.
        """

        documents = _iter_transcript_documents(path, repair=True)
        header = next(documents, None)
        if header is None or header.get("type") != "session":
            raise ValueError(f"invalid Session transcript header: {path}")
        session_id = _session_id(header.get("id"))
        if path.parent.name != session_id:
            raise ValueError(f"Session transcript identity mismatch: {path}")
        return session_id, _iter_transcript_events(documents, path, session_id)

def _iter_transcript_documents(
    path: Path,
    *,
    repair: bool,
) -> Iterator[dict[str, object]]:
    """Yield transcript documents one frame at a time.

    Each append writes its own zstd frame, so decoding frame by frame keeps
    peak memory proportional to a single batch instead of the whole
    transcript.  A torn final frame is discarded, and a readable owner may
    also cut it off the file (``repair``); a pure reader leaves the bytes
    alone and only skips them.
    """

    encoded = path.read_bytes()
    remaining = encoded
    valid_size = 0
    while remaining:
        frame_start = len(encoded) - len(remaining)
        decoder = zstandard.ZstdDecompressor().decompressobj()
        try:
            payload = decoder.decompress(remaining)
        except zstandard.ZstdError:
            break
        if not decoder.eof:
            break
        consumed = len(remaining) - len(decoder.unused_data)
        valid_size = frame_start + consumed
        remaining = decoder.unused_data
        for line in payload.decode("utf-8").split("\n"):
            if line:
                yield json.loads(line)
    if valid_size == 0 and encoded:
        raise ValueError(f"invalid Session transcript compression: {path}")
    if repair and valid_size < len(encoded):
        with path.open("r+b") as stream:
            stream.truncate(valid_size)
            stream.flush()
            os.fsync(stream.fileno())


def _iter_transcript_events(
    documents: Iterator[dict[str, object]],
    path: Path,
    session_id: str,
) -> Iterator[SessionEvent]:
    """Flatten every transcript row into the events it stands for.

    A packed row carries a whole stream run, so ``expected_seq`` advances by
    however many events the previous row produced rather than by one.  A row
    this build cannot rebuild but that is marked ``ignorable`` still keeps its
    slot: the writer that produced it numbered the following row after it.
    """

    expected_seq = 0
    for document in documents:
        events = _events_from_document(document, path, session_id, expected_seq)
        expected_seq = events[-1].seq + 1 if events else expected_seq + 1
        yield from events


def read_session_transcript(root: Path, session_id: str) -> tuple[SessionEvent, ...]:
    """Read one Session transcript purely: nothing is repaired or written.

    A sandbox Worker shares the control plane's data directory and therefore
    the control-owned transcript, but it must not append to it, rewrite its
    projections, or truncate the frame the control is mid-append.  This reader
    is the fold source for that process: an unknown Session is empty and a
    torn tail is skipped rather than cut off.
    """

    owner = _session_id(session_id)
    path = Path(root) / owner / "session.jsonl.zstd"
    if not path.is_file():
        return ()
    documents = _iter_transcript_documents(path, repair=False)
    header = next(documents, None)
    if header is None:
        return ()
    if header.get("type") != "session":
        raise ValueError(f"invalid Session transcript header: {path}")
    if _session_id(header.get("id")) != owner:
        raise ValueError(f"Session transcript identity mismatch: {path}")
    return tuple(_iter_transcript_events(documents, path, owner))


#: Streaming deltas a whole message is made of: hundreds of rows that differ
#: only in one string and a timestamp.  Harness packs runs of them into one
#: ``*-chunks`` row (``packChunks`` defaults on) and rebuilds the individual
#: events on read; the logical log stays exact while the file shrinks by about
#: 60%.  Only deltas whose payload is one text plus uniform scalar metadata can
#: be packed, because packing must lose nothing.
_PACKABLE_TYPES = {
    "assistant/text.delta": "text-chunks",
    "assistant/reasoning.delta": "reasoning-chunks",
}

#: Below this many consecutive deltas a packed row is not worth its own header;
#: Harness packs the same minimum.
_PACK_MIN = 3

#: Upper bound on one packed row, so a single huge batch (a forced flush after a
#: long stream) cannot build a row larger than the reader wants to hold.
_PACK_MAX = 256


def _transcript_documents(batch: tuple[SessionEvent, ...]) -> list[str]:
    """Encode one append batch as transcript rows, packing stream runs."""

    lines: list[str] = []
    group: list[SessionEvent] = []
    extras: dict[str, object] = {}

    def flush_group() -> None:
        if len(group) >= _PACK_MIN and _monotonic_ms(group):
            lines.append(_packed_line(group, extras))
        else:
            # Too short to be worth a row header, or its times go backwards --
            # a packed row only records forward ``dt`` steps, so anything else
            # has to stay one row per event instead of losing the ordering.
            lines.extend(_event_line(event) for event in group)
        group.clear()
        extras.clear()

    for event in batch:
        if _PACKABLE_TYPES.get(event.type) is None or not isinstance(
            event.data.get("text"), str
        ):
            flush_group()
            lines.append(_event_line(event))
            continue
        carried = _chunk_extras(event.data)
        if group and (
            len(group) >= _PACK_MAX
            or group[-1].type != event.type
            or group[-1].run_id != event.run_id
            or carried != extras
        ):
            # A run is only packable while the row stays uniform: one event
            # type, one Run, and metadata that never changes mid-stream.
            flush_group()
        if not group:
            extras.update(carried)
        group.append(event)
    flush_group()
    return lines


def _monotonic_ms(group: list[SessionEvent]) -> bool:
    """Whether a packed row's whole ``dt`` ladder stays non-negative."""

    stamps = [int(round(event.time * 1000)) for event in group]
    return all(later >= earlier for earlier, later in zip(stamps, stamps[1:]))


def _chunk_extras(payload: Mapping[str, object]) -> dict[str, object]:
    """The uniform scalar fields a packed run carries beside its texts."""

    return {key: value for key, value in payload.items() if key != "text"}


def _event_line(event: SessionEvent) -> str:
    document: dict[str, object] = {
        "type": "event",
        "event": session_event_to_dict(event),
    }
    if event.type not in SESSION_EVENT_TYPES:
        # 未知类型必须自证可跳过：读端遇到不认识的类型又没有这个标记时应当拒绝
        # 重建，而不是静默丢掉一个它看不懂的事件。写端认识的类型不必标注。
        document["ignorable"] = True
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _packed_line(group: list[SessionEvent], extras: Mapping[str, object]) -> str:
    first = group[0]
    offsets = [int(round(event.time * 1000)) for event in group]
    # Harness' contract is ``len(texts) == len(dt) + 1``: the first member
    # starts at ``time0`` and each ``dt`` is the step to the next member.
    dt = [offsets[index + 1] - offsets[index] for index in range(len(group) - 1)]
    data: dict[str, object] = {
        "seq0": first.seq,
        "time0": float(first.time),
        "run_id": first.run_id,
        "data": {
            "texts": [str(event.data.get("text") or "") for event in group],
            "dt": dt,
            **dict(extras),
        },
    }
    document = {"type": _PACKABLE_TYPES[first.type], **data}
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _events_from_document(
    document: dict[str, object],
    path: Path,
    session_id: str,
    expected_seq: int,
) -> tuple[SessionEvent, ...]:
    """Rebuild every event one transcript row stands for.

    One row is one event, except for the packed ``*-chunks`` rows Harness writes
    for stream runs: those stand for ``len(texts)`` events whose ``seq`` is
    contiguous from ``seq0`` and whose ``time`` accumulates ``dt``.  A row whose
    type this build does not know must say ``ignorable`` or the transcript is
    rejected rather than silently dropped.
    """

    kind = str(document.get("type") or "")
    if kind in _UNPACKED_TYPES:
        raw = document.get("data")
        if not isinstance(raw, dict):
            raise ValueError(f"invalid packed Session transcript row: {path}")
        texts = raw.get("texts")
        dt = raw.get("dt")
        if (
            not isinstance(texts, list)
            or not isinstance(dt, list)
            or len(texts) != len(dt) + 1
            or not all(isinstance(text, str) for text in texts)
            or not all(isinstance(step, int) for step in dt)
        ):
            raise ValueError(f"invalid packed Session transcript row: {path}")
        event_type = _UNPACKED_TYPES.get(kind, kind)
        extras = {
            key: value
            for key, value in raw.items()
            if key not in {"texts", "dt"}
        }
        seq0 = document.get("seq0")
        time0 = document.get("time0")
        if not isinstance(seq0, int) or not isinstance(time0, (int, float)):
            raise ValueError(f"packed Session transcript row lacks a start: {path}")
        run_id = document.get("run_id")
        events: list[SessionEvent] = []
        elapsed_ms = 0
        for index, text in enumerate(texts):
            if index:
                elapsed_ms += int(dt[index - 1])
            events.append(
                SessionEvent(
                    session_id=session_id,
                    seq=seq0 + index,
                    type=event_type,
                    time=float(time0) + elapsed_ms / 1000,
                    run_id=None if run_id is None else str(run_id),
                    data={"text": text, **extras},
                )
            )
        if seq0 != expected_seq:
            raise ValueError(f"non-contiguous Session transcript: {path}")
        return tuple(events)
    if kind != "event" or not isinstance(document.get("event"), dict):
        if document.get("ignorable") is True:
            return ()
        raise ValueError(f"invalid Session transcript record: {path}")
    raw = document["event"]
    event_type = str(raw.get("type") or "")
    if event_type not in SESSION_EVENT_TYPES:
        if document.get("ignorable") is True:
            return ()
        raise ValueError(
            f"unknown Session transcript event type without ignorable: "
            f"{event_type or '<missing>'} in {path}"
        )
    event = SessionEvent(
        session_id=session_id,
        seq=int(raw["seq"]),
        type=event_type,
        time=float(raw["time"]),
        run_id=None if raw.get("run_id") is None else str(raw.get("run_id")),
        data=dict(raw.get("data") or {}),
    )
    if event.seq != expected_seq:
        raise ValueError(f"non-contiguous Session transcript: {path}")
    return (event,)


#: Packed row type -> the event type it stands for.
_UNPACKED_TYPES = {name: type_ for type_, name in _PACKABLE_TYPES.items()}


def _run_event(event: SessionEvent) -> RunEvent | None:
    data = {key: value for key, value in event.data.items() if key != "_dedupe_key"}
    kind = _EVENT_KINDS.get(event.type)
    if kind is not None:
        return RunEvent(kind, data)
    if event.type == "run/event":
        payload = data.get("payload")
        return RunEvent(
            str(data.get("kind") or ""),
            dict(payload) if isinstance(payload, dict) else {},
        )
    return None


def _group_run_events(
    events: list[SessionEvent],
) -> dict[str, list[RunEvent]]:
    """Group one Session's events by Run for on-demand Run replay."""

    grouped: dict[str, list[RunEvent]] = {}
    for event in events:
        run_id = str(event.run_id or "")
        if not run_id or event.type.startswith("agent/inbox/"):
            continue
        run_event = _run_event(event)
        if run_event is not None:
            grouped.setdefault(run_id, []).append(run_event)
    return grouped


def transcript_run_records(root: Path) -> list[dict[str, object]]:
    """Project Run-shaped search records from the sole Session transcript store."""

    store = SessionLogStore(root)
    for path in sorted(store.root.glob("*/session.jsonl.zstd")):
        session_id, events = store._read_transcript(path)
        store._rebuild_runs(session_id, events)
        store._hydrated_runs[session_id] = _group_run_events(list(events))
    records: list[dict[str, object]] = []
    for projection in sorted(
        store._runs.values(),
        key=lambda item: (item.created_at, item.request_id),
    ):
        run = projection.snapshot(
            store._hydrated_runs.get(projection.session_id, {}).get(
                projection.request_id
            )
        )
        records.append({
            "id": run.request_id,
            "request_id": run.request_id,
            "session_id": run.session_id,
            "status": run.status,
            "request": dict(run.request),
            "events": [
                {"kind": event.kind, "payload": dict(event.payload)}
                for event in run.events
            ],
            "result": None if run.result is None else encode_result(run.result),
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        })
    return records


__all__ = [
    "SessionLogStore",
    "read_session_transcript",
    "transcript_run_records",
]
