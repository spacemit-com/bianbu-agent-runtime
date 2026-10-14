import pytest

from bianbu_agent import RunControlRegistry


@pytest.mark.asyncio
async def test_control_registry_cancels_request_and_session_once() -> None:
    registry = RunControlRegistry()
    first = await registry.register("run-1", "session-1")
    second = await registry.register("run-2", "session-1")

    assert await registry.cancel_request("run-1") is True
    assert await registry.cancel_request("run-1") is False
    assert first.cancelled.is_set()
    assert await registry.cancel_session("session-1") == 1
    assert second.cancelled.is_set()


@pytest.mark.asyncio
async def test_control_registry_rejects_duplicate_run_id() -> None:
    registry = RunControlRegistry()
    await registry.register("run-1", "session-1")

    with pytest.raises(ValueError, match="request already active: run-1"):
        await registry.register("run-1", "session-2")
