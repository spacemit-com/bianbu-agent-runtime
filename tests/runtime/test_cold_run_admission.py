"""Cold Worker startup must not hold input admission or cancellation hostage."""
import asyncio
from types import SimpleNamespace

import pytest

from bianbu_agent.security.runtime_process.supervisor import ControlRunManager
from bianbu_agent.security.runtime_process.worker import MemoryRunStore


class ColdWorker:
    def __init__(self):
        self.ready = False
        self.identity = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.dispatched = []
        self.client = self
        self.failure = None

    async def start(self):
        self.started.set()
        await self.release.wait()
        if self.failure:
            raise self.failure
        self.ready = True
        self.identity = SimpleNamespace(generation_id=7)

    async def run_start(self, payload, on_event):
        self.dispatched.append(payload)
        await on_event({'type': 'run.accepted'})
        await on_event({'type': 'run.result', 'result': {
            'request_id': payload['run_id'], 'status': 'completed', 'content': 'Hello!'}})


async def admit(manager, worker):
    pending = asyncio.create_task(manager.run('cold-session', 'hello', agent_id='expert'))
    await asyncio.wait_for(worker.started.wait(), 1)
    # Startup is deliberately still blocked, not merely fast on this machine.
    for _ in range(10):
        if pending.done():
            break
        await asyncio.sleep(0)
    admitted = pending.done()
    if not admitted:
        worker.release.set()
        await pending
        await manager.close()
    assert admitted, 'input admission waited for the cold Worker'
    return await pending


@pytest.mark.asyncio
async def test_cold_run_is_durable_before_worker_ready():
    worker = ColdWorker()
    store = MemoryRunStore()
    manager = ControlRunManager(store, worker)
    try:
        handle = await admit(manager, worker)
        assert (await store.get(handle.id)).request['prompt'] == 'hello'
        assert not worker.ready
        assert not worker.dispatched
        assert (await store.get(handle.id)).events[-1].payload['phase'] == 'starting_agent'
        worker.release.set()
        assert (await asyncio.wait_for(handle.wait(), 1)).content == 'Hello!'
        assert len(worker.dispatched) == 1
    finally:
        worker.release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_cancel_cold_run_never_dispatches_prompt():
    worker = ColdWorker()
    manager = ControlRunManager(MemoryRunStore(), worker)
    try:
        handle = await admit(manager, worker)
        assert await handle.cancel()
        assert (await asyncio.wait_for(handle.wait(), 1)).status == 'cancelled'
        worker.release.set()
        await asyncio.gather(*tuple(manager._tasks.values()))
        assert not worker.dispatched
    finally:
        worker.release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_startup_failure_becomes_durable_run_error():
    worker = ColdWorker()
    worker.failure = RuntimeError('activation failed')
    store = MemoryRunStore()
    manager = ControlRunManager(store, worker)
    try:
        handle = await admit(manager, worker)
        worker.release.set()
        result = await asyncio.wait_for(handle.wait(), 1)
        assert result.status == 'failed'
        assert result.error.message == 'activation failed'
        assert (await store.get(handle.id)).result == result
        assert not worker.dispatched
    finally:
        worker.release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_cancellation_between_dispatch_and_acceptance_is_not_lost():
    worker = ColdWorker()
    worker.ready = True
    sent = asyncio.Event()
    accepted = asyncio.Event()
    cancelled = asyncio.Event()

    async def run_start(payload, on_event):
        sent.set()
        await accepted.wait()
        await on_event({'type': 'run.accepted'})
        await cancelled.wait()
        await on_event({'type': 'run.result', 'result': {
            'request_id': payload['run_id'], 'status': 'cancelled'}})

    async def cancel(run_id):
        cancelled.set()
        return True

    worker.run_start = run_start
    worker.cancel = cancel
    manager = ControlRunManager(MemoryRunStore(), worker)
    try:
        handle = await manager.run('s', 'hello', agent_id='expert')
        await sent.wait()
        assert await handle.cancel()
        assert not cancelled.is_set()
        accepted.set()
        assert (await asyncio.wait_for(handle.wait(), 1)).status == 'cancelled'
        assert cancelled.is_set()
    finally:
        accepted.set()
        cancelled.set()
        await manager.close()


@pytest.mark.asyncio
async def test_shutdown_waits_for_startup_registration_without_cancelling_spawn():
    worker = ColdWorker()
    startup_cancelled = False
    original_start = worker.start

    async def tracked_start():
        nonlocal startup_cancelled
        try:
            await original_start()
        except asyncio.CancelledError:
            startup_cancelled = True
            raise

    worker.start = tracked_start
    manager = ControlRunManager(MemoryRunStore(), worker)
    handle = await admit(manager, worker)
    closing = asyncio.create_task(manager.close())
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert not startup_cancelled, 'shutdown cancelled a backend startup still registering its process'
        assert not closing.done()
        assert (await handle.wait()).status == 'cancelled'
    finally:
        worker.release.set()
        await closing
    assert not worker.dispatched
