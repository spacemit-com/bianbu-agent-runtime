"""Run runtime contracts and file-backed persistence."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from bianbu_agent.runs.events import RunEvent
from bianbu_agent.runs.models import StoredRun


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = int(getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must not be negative")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class RunError:
    """一次 Run 失败的机器可读描述。

    ``code`` 是客户端可以分支的稳定标识；``message`` 是给人看的原因；
    ``retryable`` 表示「不做任何改动直接重试是否可能成功」，由 Runtime 判定，
    客户端不再按 code 自己猜；``details`` 是定位与处置所需的结构化字段。
    """

    code: str
    message: str
    details: Mapping[str, object] = field(default_factory=dict)
    retryable: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        if not str(self.message).strip():
            # 空消息会让客户端只剩一个看不懂的 code（空消息异常如
            # ``TimeoutError()`` 的 ``str()`` 就是空串）。这里只是兜底，
            # 根因仍在分类器：它必须给出可读原因。
            object.__setattr__(self, "message", str(self.code))


@dataclass(frozen=True, slots=True)
class RunResult:
    request_id: str
    status: str
    content: str = ""
    usage: Usage | None = None
    error: RunError | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


ControlRouter = Callable[[str, Mapping[str, object]], object | Awaitable[object]]
CommittedCallback = Callable[[], None]


class RunHandle:
    def __init__(
        self,
        *,
        request_id: str,
        session_id: str,
        store,
        control: ControlRouter | None = None,
        agent_id: str = "",
        generation_id: int = 0,
        on_committed: CommittedCallback | None = None,
    ) -> None:
        self.request_id = str(request_id)
        self.session_id = str(session_id)
        self.agent_id = str(agent_id)
        self.generation_id = int(generation_id)
        self._store = store
        self._control = control
        self._on_committed = on_committed
        self._queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
        self._finished = asyncio.Event()
        self._result: RunResult | None = None

    @property
    def id(self) -> str:
        return self.request_id

    async def start(self, request: dict[str, object]) -> None:
        await self._store.start(self.request_id, self.session_id, request)
        if self._on_committed is not None:
            self._on_committed()

    @classmethod
    async def restore(cls, stored, *, store) -> RunHandle:
        request = dict(getattr(stored, "request", {}) or {})
        handle = cls(
            request_id=stored.request_id,
            session_id=stored.session_id,
            store=store,
            agent_id=str(request.get("agent_id") or ""),
            generation_id=int(request.get("generation_id") or 0),
        )
        for event in stored.events:
            await handle._queue.put(event)
        result = stored.result
        if result is None:
            result = RunResult(
                request_id=stored.request_id,
                status="failed",
                error=RunError(
                    code="run_interrupted",
                    message="Run was interrupted before a terminal result was persisted",
                ),
            )
            await store.finish(stored.request_id, result)
        handle._result = result
        handle._finished.set()
        await handle._queue.put(None)
        return handle

    async def publish(self, event: RunEvent) -> None:
        if self._finished.is_set():
            raise RuntimeError("Run is already finished")
        await self._store.append(self.request_id, event)
        if self._on_committed is not None:
            self._on_committed()
        await self._queue.put(event)

    async def finish(self, result: RunResult) -> None:
        if result.request_id != self.request_id:
            raise ValueError("RunResult request_id does not match RunHandle")
        if self._finished.is_set():
            return
        try:
            await self._store.finish(self.request_id, result)
        except KeyError:
            # The owning Session was deleted while this Run was still
            # streaming, so there is no projection left to persist a terminal
            # result into. Finish locally: the streaming task must unwind
            # instead of surfacing an unretrieved KeyError.
            logger.warning(
                "Run %s finished after its Session was deleted; "
                "the terminal result is not persisted", self.request_id,
            )
        if self._on_committed is not None:
            self._on_committed()
        self._result = result
        self._finished.set()
        await self._queue.put(None)

    def __aiter__(self) -> AsyncIterator[RunEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[RunEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event

    async def wait(self) -> RunResult:
        await self._finished.wait()
        assert self._result is not None
        return self._result

    async def cancel(self) -> bool:
        return await self._route("cancel", {})

    async def approve(self, approval_id: str) -> bool:
        return await self._route("approve", {"approval_id": approval_id})

    async def reject(self, approval_id: str, *, reason: str = "") -> bool:
        return await self._route(
            "reject", {"approval_id": approval_id, "reason": reason}
        )

    async def answer(self, question_id: str, answer: str) -> bool:
        return await self._route(
            "answer", {"question_id": question_id, "answer": answer}
        )

    async def _route(self, action: str, payload: Mapping[str, object]) -> bool:
        return bool(await self._route_value(action, payload))

    async def _route_value(
        self,
        action: str,
        payload: Mapping[str, object],
    ) -> object:
        if self._control is None:
            return False
        value = self._control(action, payload)
        return await value if inspect.isawaitable(value) else value


def encode_result(result: RunResult) -> dict[str, object]:
    return {
        "request_id": result.request_id,
        "status": result.status,
        "content": result.content,
        "usage": (
            None
            if result.usage is None
            else {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
                "metadata": dict(result.usage.metadata),
            }
        ),
        "error": None if result.error is None else encode_error(result.error),
        "metadata": dict(result.metadata),
    }


def encode_error(error: RunError) -> dict[str, object]:
    """错误对象的线上形状。

    终态 ``run/failed`` 与失败过程中的诊断事件共用这一份编码，避免两个
    发布点各写一遍、字段逐渐分叉。
    """

    return {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
        "details": dict(error.details),
    }


def decode_result(payload: Mapping[str, object]) -> RunResult:
    error_payload = payload.get("error")
    usage_payload = payload.get("usage")
    # Worker 是不可信输入的一侧：只有明确的布尔才当作判定，其余（缺失、字符串、
    # 数字）一律退回「可重试」这一保守默认，避免畸形载荷把不可重试说成可重试。
    raw_retryable = (
        error_payload.get("retryable") if isinstance(error_payload, Mapping) else None
    )
    error = (
        RunError(
            code=error_payload["code"],
            message=error_payload["message"],
            details=error_payload.get("details", {}),
            retryable=raw_retryable if isinstance(raw_retryable, bool) else True,
        )
        if isinstance(error_payload, Mapping)
        else None
    )
    return RunResult(
        request_id=str(payload["request_id"]),
        status=str(payload["status"]),
        content=str(payload.get("content", "")),
        usage=(
            Usage(
                input_tokens=int(usage_payload.get("input_tokens", 0)),
                output_tokens=int(usage_payload.get("output_tokens", 0)),
                total_tokens=int(usage_payload.get("total_tokens", 0)),
                metadata=usage_payload.get("metadata", {}),
            )
            if isinstance(usage_payload, Mapping)
            else None
        ),
        error=error,
        metadata=payload.get("metadata", {}),
    )


__all__ = [
    "RunError",
    "RunHandle",
    "RunResult",
    "StoredRun",
    "Usage",
]
