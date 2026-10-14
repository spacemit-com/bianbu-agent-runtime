"""Tool approval parks the Run in-process instead of saving a checkpoint."""

from __future__ import annotations

import asyncio

import pytest

from bianbu_agent.engines.langgraph.engine import _cancellable_graph_stream
from bianbu_agent.runs.control import RunControl


def test_request_approval_publishes_before_it_waits() -> None:
    async def scenario() -> None:
        control = RunControl(request_id="run-1", session_id="session-a")
        waiter = asyncio.create_task(
            control.request_approval({"approval_id": "a-1", "actions": []})
        )
        payload = await asyncio.wait_for(
            control.wait_approval_request(), timeout=1
        )

        assert payload == {"approval_id": "a-1", "actions": []}
        assert control.approval_pending() is True

        assert control.resolve_approval([{"type": "approve"}]) is True
        assert await asyncio.wait_for(waiter, timeout=1) == [{"type": "approve"}]
        assert control.approval_pending() is False

    asyncio.run(scenario())


def test_a_resolved_approval_cannot_be_resolved_twice() -> None:
    async def scenario() -> None:
        control = RunControl(request_id="run-1", session_id="session-a")
        waiter = asyncio.create_task(
            control.request_approval({"approval_id": "a-1", "actions": []})
        )
        await asyncio.wait_for(control.wait_approval_request(), timeout=1)

        assert control.resolve_approval([{"type": "approve"}]) is True
        assert control.resolve_approval([{"type": "reject"}]) is False
        assert await asyncio.wait_for(waiter, timeout=1) == [{"type": "approve"}]

    asyncio.run(scenario())


def test_only_one_approval_can_be_pending_on_a_run() -> None:
    async def scenario() -> None:
        control = RunControl(request_id="run-1", session_id="session-a")
        waiter = asyncio.create_task(
            control.request_approval({"approval_id": "a-1", "actions": []})
        )
        await asyncio.wait_for(control.wait_approval_request(), timeout=1)

        with pytest.raises(RuntimeError):
            await control.request_approval({"approval_id": "a-2", "actions": []})

        control.resolve_approval([{"type": "approve"}])
        await asyncio.wait_for(waiter, timeout=1)

    asyncio.run(scenario())


def test_the_run_stream_surfaces_an_approval_while_the_graph_is_parked() -> None:
    """The graph produces nothing while parked, so the stream must carry it."""

    async def scenario() -> None:
        control = RunControl(request_id="run-1", session_id="session-a")

        async def parked():
            decisions = await control.request_approval(
                {"approval_id": "a-1", "actions": [{"name": "terminal"}]}
            )
            yield {"decisions": decisions}

        stream = _cancellable_graph_stream(parked().__aiter__(), control)
        iterator = stream.__aiter__()

        event = await asyncio.wait_for(anext(iterator), timeout=1)
        assert event.kind == "approval_required"
        assert event.payload["approval_id"] == "a-1"

        assert control.resolve_approval([{"type": "approve"}]) is True
        assert await asyncio.wait_for(anext(iterator), timeout=1) == {
            "decisions": [{"type": "approve"}]
        }

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(iterator), timeout=1)

    asyncio.run(scenario())


def test_the_run_stream_still_ends_on_cancellation() -> None:
    async def scenario() -> None:
        control = RunControl(request_id="run-1", session_id="session-a")

        async def parked():
            await control.request_approval({"approval_id": "a-1", "actions": []})
            yield "never"

        stream = _cancellable_graph_stream(parked().__aiter__(), control)
        iterator = stream.__aiter__()
        await asyncio.wait_for(anext(iterator), timeout=1)

        await control.cancel()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(iterator), timeout=1)

    asyncio.run(scenario())


def test_approval_middleware_honours_the_when_predicate() -> None:
    """``ApprovalMiddleware`` must not depend on the running LangChain's hook.

    The riscv64 lock resolves LangChain 1.2.18, whose middleware calls
    ``self._should_interrupt(...)`` without defining it; inheriting that hook
    from 1.3.x is not an option because 1.3.x is not the version loaded there.
    The middleware therefore owns the decision: no predicate means interrupt,
    and a predicate reads the tool call off the request.
    """

    from bianbu_agent.engines.langgraph.approval import ApprovalMiddleware

    seen: list[object] = []

    def when(request: object) -> bool:
        seen.append(getattr(request, "tool_call", None))
        return False

    middleware = ApprovalMiddleware(
        interrupt_on={
            "always": {"allowed_decisions": ["approve", "reject"]},
            "gated": {"allowed_decisions": ["approve", "reject"], "when": when},
        }
    )
    always = {"name": "always", "args": {}, "id": "call-1"}
    gated = {"name": "gated", "args": {}, "id": "call-2"}

    assert middleware._should_interrupt(
        always, middleware.interrupt_on["always"], {}, None
    ) is True
    assert middleware._should_interrupt(
        gated, middleware.interrupt_on["gated"], {}, None
    ) is False
    assert seen == [gated]
