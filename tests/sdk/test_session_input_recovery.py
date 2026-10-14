import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bianbu_agent.sessions.input import SessionInputBroker


def _store(**overrides):
    """The minimal inbox store the broker reaches through ``runtime.runs``."""

    values = {
        "admit_input": AsyncMock(),
        "has_projection_row": lambda session_id: True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_deleted_agent_does_not_block_other_session_recovery(caplog):
    sessions = [SimpleNamespace(id="old", agent_id="deleted"),
                SimpleNamespace(id="new", agent_id="existing")]

    async def agent(agent_id):
        if agent_id == "deleted":
            raise KeyError(agent_id)
        return SimpleNamespace(id=agent_id)

    runtime = SimpleNamespace(
        sessions=SimpleNamespace(list=AsyncMock(return_value=sessions)),
        agents=SimpleNamespace(get=agent),
        runs=SimpleNamespace(_store=_store()),
    )
    broker = SessionInputBroker(runtime)
    recovered = []

    async def start(session_id):
        if session_id == "old":
            raise RuntimeError("cannot launch deleted Agent")
        recovered.append(session_id)
        return None

    broker._active_run = AsyncMock(return_value=None)
    broker._start_pending = start
    await broker.recover()
    assert recovered == ["new"]
    assert "deleted" in caplog.text and "old" in caplog.text


@pytest.mark.asyncio
async def test_sessions_without_a_projection_row_are_left_to_first_touch(caplog):
    """A lost table must not turn a start into a fold of every Session."""

    caplog.set_level(logging.INFO)
    sessions = [SimpleNamespace(id="row-less", agent_id="default")]
    runtime = SimpleNamespace(
        sessions=SimpleNamespace(list=AsyncMock(return_value=sessions)),
        agents=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace())),
        runs=SimpleNamespace(
            _store=_store(has_projection_row=lambda session_id: False)
        ),
    )
    broker = SessionInputBroker(runtime)
    broker._active_run = AsyncMock(return_value=None)
    broker._start_pending = AsyncMock(return_value=None)

    await broker.recover()

    broker._active_run.assert_not_awaited()
    broker._start_pending.assert_not_awaited()
    assert "row-less" in caplog.text


@pytest.mark.asyncio
async def test_one_agent_is_inspected_once_per_recovery_pass():
    """Answering "does this Agent exist" parses its YAML; 179 Sessions must not."""

    sessions = [
        SimpleNamespace(id=f"session-{index}", agent_id="default")
        for index in range(5)
    ]
    lookup = AsyncMock(return_value=SimpleNamespace())

    runtime = SimpleNamespace(
        sessions=SimpleNamespace(list=AsyncMock(return_value=sessions)),
        agents=SimpleNamespace(get=lookup),
        runs=SimpleNamespace(_store=_store()),
    )
    broker = SessionInputBroker(runtime)
    broker._active_run = AsyncMock(return_value=None)
    broker._start_pending = AsyncMock(return_value=None)

    await broker.recover()

    assert lookup.await_count == 1
    assert broker._start_pending.await_count == 5
