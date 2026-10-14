import asyncio
from pathlib import Path

import pytest

from bianbu_agent import RunControl, RunError, RunEvent, RunHandle, RunResult
from bianbu_agent.runs.stores.sqlite import SQLiteRunStore


@pytest.mark.asyncio
async def test_run_handle_streams_events_controls_and_persists_result(
    tmp_path: Path,
) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    calls = []

    async def control(action, payload):
        calls.append((action, payload))
        return True

    handle = RunHandle(
        request_id="run-1",
        session_id="session-1",
        store=store,
        control=control,
    )
    await handle.start({"prompt": "secret"})
    await handle.publish(RunEvent("run_started", {"request_id": "run-1"}))
    await handle.publish(RunEvent("token", {"text": "hello"}))
    await handle.finish(RunResult(request_id="run-1", status="completed", content="hello"))

    assert [event.kind async for event in handle] == ["run_started", "token"]
    assert (await handle.wait()).content == "hello"
    assert await handle.cancel() is True
    assert await handle.approve("approval-1") is True
    assert await handle.reject("approval-2", reason="unsafe") is True
    assert await handle.answer("question-1", "yes") is True
    assert [item[0] for item in calls] == [
        "cancel",
        "approve",
        "reject",
        "answer",
    ]
    restored = await store.get("run-1")
    assert restored is not None
    assert restored.result == await handle.wait()
    assert [event.kind for event in restored.events] == ["run_started", "token"]
    await store.close()


@pytest.mark.asyncio
async def test_run_handle_failed_result_is_stable_for_multiple_waiters(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    handle = RunHandle(request_id="run-2", session_id="session-1", store=store)
    await handle.start({})
    result = RunResult(
        request_id="run-2",
        status="failed",
        error=RunError(code="engine_failed", message="boom"),
    )
    await handle.finish(result)
    assert await handle.wait() == result
    assert await handle.wait() == result
    await store.close()


@pytest.mark.asyncio
async def test_run_handle_notifies_only_after_each_durable_commit(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    await store.open()
    observations: list[tuple[int, bool]] = []

    def committed() -> None:
        async def observe() -> None:
            stored = await store.get("run-signals")
            assert stored is not None
            observations.append((len(stored.events), stored.result is not None))

        pending.append(asyncio.create_task(observe()))

    pending: list[asyncio.Task[None]] = []
    handle = RunHandle(
        request_id="run-signals",
        session_id="session-1",
        store=store,
        on_committed=committed,
    )
    await handle.start({})
    await asyncio.gather(*pending)
    pending.clear()
    await handle.publish(RunEvent("token", {"text": "now"}))
    await asyncio.gather(*pending)
    pending.clear()
    await handle.finish(RunResult(request_id="run-signals", status="completed"))
    await asyncio.gather(*pending)

    assert observations == [(0, False), (1, False), (1, True)]
    await store.close()


@pytest.mark.asyncio
async def test_run_control_cancel_remains_available_without_a_followup() -> None:
    """Making cancellation depend on a queue would leave approval waits unstoppable."""

    control = RunControl(request_id="run-cancel", session_id="session-cancel")

    assert await control.cancel() is True
    assert control.cancelled.is_set()


@pytest.mark.asyncio
async def test_run_control_coalesces_step_input_notifications() -> None:
    control = RunControl(request_id="run-steer", session_id="session-steer")

    assert not control.step_input_pending()
    await control.notify_step_input()
    await control.notify_step_input()

    assert control.step_input_pending()


@pytest.mark.asyncio
async def test_run_control_can_notify_again_after_boundary_clear() -> None:
    """Clearing one consumed edge must not disable a later steer wake."""

    control = RunControl(request_id="run-step", session_id="session-step")
    await control.notify_step_input()
    control.clear_step_input_notification()
    assert not control.step_input_pending()

    await control.notify_step_input()

    assert control.step_input_pending()


@pytest.mark.asyncio
async def test_run_control_rejects_step_input_notification_after_cancel() -> None:
    control = RunControl(request_id="run-step", session_id="session-step")
    await control.cancel()

    with pytest.raises(RuntimeError, match="no longer accepting"):
        await control.notify_step_input()
