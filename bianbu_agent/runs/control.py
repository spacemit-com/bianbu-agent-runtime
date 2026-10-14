from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from collections.abc import AsyncIterator, Iterator
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

from bianbu_agent.sessions.inbox import SessionInboxMessage


StepInputLoader = Callable[[], Awaitable[tuple[SessionInboxMessage, ...]]]


@dataclass(slots=True)
class RunControl:
    request_id: str
    session_id: str
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    _step_input_wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    _step_input_loader: StepInputLoader | None = field(default=None, repr=False)
    _step_input_revision: int = 0
    step_input_admission_open: bool = True
    _control_gate: asyncio.Lock = field(default_factory=asyncio.Lock)

    _model_deadline: asyncio.Timeout | None = field(default=None, repr=False)
    _model_timeout: float = field(default=0.0, repr=False)
    _model_deadline_pauses: int = 0
    model_recovery: dict[str, int] = field(default_factory=dict, repr=False)

    # 人在环审批的进程内通道：中间件把请求放上 outbox 后等 future，Run 流的读取侧
    # 从 outbox 取到请求并 yield `approval_required`，用户决定到达时 resolve future。
    # 因为决定在同一 Run 内解析，恢复图状态不需要 checkpoint。
    _approval_outbox: asyncio.Queue[dict[str, object]] = field(
        default_factory=asyncio.Queue
    )
    _approval_future: asyncio.Future[list[dict[str, object]]] | None = field(
        default=None, repr=False
    )

    @asynccontextmanager
    async def model_deadline(self, seconds: float) -> AsyncIterator[asyncio.Timeout]:
        """Watch model inactivity, including progress hidden from the UI."""
        async with asyncio.timeout(seconds) as deadline:
            self._model_deadline = deadline
            self._model_timeout = seconds
            try:
                if self._model_deadline_pauses:
                    deadline.reschedule(None)
                yield deadline
            finally:
                self._model_deadline = None
                self._model_timeout = 0.0

    def model_progress(self) -> None:
        deadline = self._model_deadline
        if (
            deadline is not None
            and not deadline.expired()
            and not self._model_deadline_pauses
        ):
            deadline.reschedule(
                asyncio.get_running_loop().time() + self._model_timeout
            )

    @contextmanager
    def pause_model_deadline(self) -> Iterator[None]:
        """Exclude independently bounded internal work from model inactivity."""
        self._model_deadline_pauses += 1
        deadline = self._model_deadline
        if deadline is not None and not deadline.expired():
            deadline.reschedule(None)
        try:
            yield
        finally:
            self._model_deadline_pauses -= 1
            self.model_progress()

    async def notify_step_input(self) -> None:
        """Wake this Run after Session persistence admits steering."""

        async with self._control_gate:
            if not self.step_input_admission_open or self.cancelled.is_set():
                raise RuntimeError("Run is no longer accepting step input")
            self._step_input_revision += 1
            self._step_input_wakeup.set()

    def bind_step_input_loader(self, loader: StepInputLoader) -> None:
        """Bind the Session-owned durable inbox reader for this Run."""

        if self._step_input_loader is not None:
            raise RuntimeError("step input loader is already bound")
        self._step_input_loader = loader

    async def load_step_input(self) -> tuple[SessionInboxMessage, ...]:
        loader = self._step_input_loader
        return () if loader is None else await loader()

    async def capture_step_input(self) -> tuple[SessionInboxMessage, ...]:
        """Capture one durable FIFO boundary without clearing newer wakes."""

        revision = self._step_input_revision
        batch = await self.load_step_input()
        async with self._control_gate:
            if revision == self._step_input_revision:
                self._step_input_wakeup.clear()
        return batch

    async def refresh_step_input_notification(self) -> bool:
        """Synchronize the wake edge with the current durable inbox."""

        revision = self._step_input_revision
        pending = bool(await self.load_step_input())
        async with self._control_gate:
            if pending:
                self._step_input_wakeup.set()
            elif revision == self._step_input_revision:
                self._step_input_wakeup.clear()
        return pending

    def step_input_pending(self) -> bool:
        return self._step_input_wakeup.is_set() and not self.cancelled.is_set()

    def clear_step_input_notification(self) -> None:
        self._step_input_wakeup.clear()

    async def open_step_input_admission(self) -> None:
        async with self._control_gate:
            if not self.cancelled.is_set():
                self.step_input_admission_open = True

    async def close_step_input_admission(self) -> None:
        async with self._control_gate:
            self.step_input_admission_open = False

    def should_wake_for_step_input(self) -> bool:
        return self.step_input_pending()

    async def request_approval(
        self,
        payload: Mapping[str, object],
    ) -> list[dict[str, object]]:
        """Publish one approval request and wait for the user's decisions.

        The Run stays alive while waiting -- the graph is blocked inside its
        approval middleware -- so resuming needs no saved checkpoint: the
        decision arrives on this very Run.
        """

        if self._approval_future is not None:
            raise RuntimeError("an approval is already pending on this Run")
        future: asyncio.Future[list[dict[str, object]]] = (
            asyncio.get_running_loop().create_future()
        )
        self._approval_future = future
        self._approval_outbox.put_nowait(dict(payload))
        try:
            return await future
        finally:
            self._approval_future = None

    async def wait_approval_request(self) -> dict[str, object]:
        """The next approval request the Run stream has to surface."""

        return dict(await self._approval_outbox.get())

    def publish_approval(self, payload: Mapping[str, object]) -> None:
        """Put an unread request back; used when a graph item won the race."""

        self._approval_outbox.put_nowait(dict(payload))

    def approval_pending(self) -> bool:
        return self._approval_future is not None and not self._approval_future.done()

    def resolve_approval(
        self,
        decisions: Sequence[Mapping[str, object]],
    ) -> bool:
        """Hand the user's decisions to the waiting middleware."""

        future = self._approval_future
        if future is None or future.done():
            return False
        future.set_result([dict(decision) for decision in decisions])
        return True

    async def cancel(self) -> bool:
        """Request ordinary Run cancellation independently of any follow-up."""

        async with self._control_gate:
            if self.cancelled.is_set():
                return False
            self.step_input_admission_open = False
            self.cancelled.set()
            return True



class RunControlRegistry:
    """Concurrency-safe indexes for active runs."""

    def __init__(self) -> None:
        self._by_request: dict[str, RunControl] = {}
        self._by_session: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def register(self, request_id: str, session_id: str) -> RunControl:
        control = RunControl(request_id=request_id, session_id=session_id)
        async with self._lock:
            if request_id in self._by_request:
                raise ValueError(f"request already active: {request_id}")
            self._by_request[request_id] = control
            self._by_session.setdefault(session_id, set()).add(request_id)
        return control

    async def complete(self, request_id: str) -> None:
        async with self._lock:
            control = self._by_request.pop(request_id, None)
            if control is None:
                return
            requests = self._by_session.get(control.session_id)
            if requests is None:
                return
            requests.discard(request_id)
            if not requests:
                self._by_session.pop(control.session_id, None)

    async def cancel_request(self, request_id: str) -> bool:
        async with self._lock:
            control = self._by_request.get(request_id)
        return False if control is None else await control.cancel()

    async def cancel_session(self, session_id: str) -> int:
        async with self._lock:
            controls = [
                self._by_request[request_id]
                for request_id in self._by_session.get(session_id, set())
            ]
        return sum([await control.cancel() for control in controls])

__all__ = [
    "RunControl",
    "RunControlRegistry",
]
