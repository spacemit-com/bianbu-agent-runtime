"""Every security mode change must rebuild the Worker for its backend."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from bianbu_agent.api import security


@pytest.mark.asyncio
@pytest.mark.parametrize('old_mode,new_mode', [('safe', 'yolo'), ('yolo', 'safe')])
async def test_mode_switch_rebuilds_idle_sandbox_around_security_update(monkeypatch, old_mode, new_mode):
    events = []
    class Worker:
        ready = True
        async def stop(self, **kwargs):
            events.append('stop')
        async def start(self):
            events.append('start')

    @asynccontextmanager
    async def lock(agent_id):
        events.append('lock')
        yield
        events.append('unlock')

    pool = SimpleNamespace(_supervisors={'a': Worker()}, agent_lock=lock,
                           agent_busy=lambda agent_id: False)
    runtime = SimpleNamespace(worker_supervisor=pool)
    def invoke(operation, params):
        if operation == 'agents.security-mode.get.v1':
            return {'mode': old_mode}
        events.append('set')
        return {'mode': new_mode}
    monkeypatch.setattr(security, '_invoke_sync', invoke)
    result = await security._update_security_mode(runtime, {'agent_id': 'a', 'mode': new_mode})
    assert result['mode'] == new_mode
    assert events == ['lock', 'stop', 'set', 'start', 'unlock']


@pytest.mark.asyncio
async def test_mode_switch_does_not_stop_busy_sandbox(monkeypatch):
    from fastapi import HTTPException
    @asynccontextmanager
    async def lock(agent_id):
        yield
    pool = SimpleNamespace(_supervisors={'a': SimpleNamespace(ready=True)},
                           agent_lock=lock, agent_busy=lambda agent_id: True)
    runtime = SimpleNamespace(worker_supervisor=pool)
    def invoke(operation, params):
        assert operation == 'agents.security-mode.get.v1', 'busy Worker must preserve current mode'
        return {'mode': 'safe'}
    monkeypatch.setattr(security, '_invoke_sync', invoke)
    with pytest.raises(HTTPException) as caught:
        await security._update_security_mode(runtime, {'agent_id': 'a', 'mode': 'yolo'})
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_failed_mode_set_restarts_worker_with_previous_mode(monkeypatch):
    events = []
    class Worker:
        ready = True
        async def stop(self, **kwargs):
            events.append('stop')
        async def start(self):
            events.append('start-old-mode')
    @asynccontextmanager
    async def lock(agent_id):
        yield
    pool = SimpleNamespace(_supervisors={'a': Worker()}, agent_lock=lock,
                           agent_busy=lambda agent_id: False)
    def invoke(operation, params):
        if operation == 'agents.security-mode.get.v1':
            return {'mode': 'safe'}
        events.append('set-failed')
        raise RuntimeError('mode update rejected')
    monkeypatch.setattr(security, '_invoke_sync', invoke)
    with pytest.raises(RuntimeError, match='mode update rejected'):
        await security._update_security_mode(SimpleNamespace(worker_supervisor=pool),
                                             {'agent_id': 'a', 'mode': 'yolo'})
    assert events == ['stop', 'set-failed', 'start-old-mode']


@pytest.mark.asyncio
async def test_failed_rebuild_does_not_report_success_from_mode_api(monkeypatch):
    from starlette.requests import Request
    class Worker:
        ready = True
        async def stop(self, **kwargs):
            pass
        async def start(self):
            raise RuntimeError('namespace startup failed')
    @asynccontextmanager
    async def lock(agent_id):
        yield
    pool = SimpleNamespace(_supervisors={'a': Worker()}, agent_lock=lock,
                           agent_busy=lambda agent_id: False)
    monkeypatch.setattr(security, '_invoke_sync', lambda operation, params: {
        'mode': 'safe' if operation == 'agents.security-mode.get.v1' else 'yolo'})
    import json
    payload = json.dumps({'protocolVersion': 1, 'serviceId': 'security', 'requestId': 'test',
                          'operationId': 'agents.security-mode.update.v1',
                          'params': {'agent_id': 'a', 'mode': 'yolo'}}).encode()
    async def receive():
        return {'type': 'http.request', 'body': payload, 'more_body': False}
    app = SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(worker_supervisor=pool)))
    response = await security.invoke_security(Request({'type': 'http', 'app': app}, receive))
    assert response['ok'] is False
    assert response['error']['retryable'] is True


@pytest.mark.asyncio
async def test_host_to_sandbox_transition_rebuilds_the_worker(monkeypatch):
    """A host process cannot become sandboxed in place, so it must restart."""
    events = []

    class Worker:
        ready = True

        async def stop(self, **kwargs):
            events.append('stop')

        async def start(self):
            events.append('start-sandbox')

    @asynccontextmanager
    async def lock(agent_id):
        yield

    pool = SimpleNamespace(_supervisors={'a': Worker()}, agent_lock=lock,
                           agent_busy=lambda agent_id: False)
    runtime = SimpleNamespace(worker_supervisor=pool)

    def invoke(operation, params):
        if operation == 'agents.security-mode.get.v1':
            return {'mode': 'yolo'}
        events.append('set')
        return {'mode': 'safe'}

    monkeypatch.setattr(security, '_invoke_sync', invoke)
    result = await security._update_security_mode(
        runtime, {'agent_id': 'a', 'mode': 'safe'})
    assert result['mode'] == 'safe'
    assert events == ['stop', 'set', 'start-sandbox']


@pytest.mark.asyncio
async def test_unchanged_mode_does_not_restart_the_worker(monkeypatch):
    events = []

    class Worker:
        ready = True

        async def stop(self, **kwargs):
            events.append('stop')

        async def start(self):
            events.append('start')

    @asynccontextmanager
    async def lock(agent_id):
        yield

    pool = SimpleNamespace(_supervisors={'a': Worker()}, agent_lock=lock,
                           agent_busy=lambda agent_id: False)
    runtime = SimpleNamespace(worker_supervisor=pool)

    def invoke(operation, params):
        if operation == 'agents.security-mode.get.v1':
            return {'mode': 'yolo'}
        events.append('set')
        return {'mode': 'yolo'}

    monkeypatch.setattr(security, '_invoke_sync', invoke)
    await security._update_security_mode(runtime, {'agent_id': 'a', 'mode': 'yolo'})
    assert events == ['set']
