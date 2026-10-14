"""Sandbox network namespaces must be recreated across security mode changes."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from bianbu_agent.api import security


@pytest.mark.asyncio
@pytest.mark.parametrize('old_mode,new_mode', [('normal', 'yolo'), ('yolo', 'normal')])
async def test_mode_switch_rebuilds_idle_sandbox_around_security_update(monkeypatch, old_mode, new_mode):
    events = []
    class Worker:
        record = SimpleNamespace(sandboxed=True)
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
    pool = SimpleNamespace(_supervisors={'a': SimpleNamespace(record=SimpleNamespace(sandboxed=True))},
                           agent_lock=lock, agent_busy=lambda agent_id: True)
    runtime = SimpleNamespace(worker_supervisor=pool)
    def invoke(operation, params):
        assert operation == 'agents.security-mode.get.v1', 'busy Worker must preserve current mode'
        return {'mode': 'normal'}
    monkeypatch.setattr(security, '_invoke_sync', invoke)
    with pytest.raises(HTTPException) as caught:
        await security._update_security_mode(runtime, {'agent_id': 'a', 'mode': 'yolo'})
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_failed_mode_set_restarts_worker_with_previous_mode(monkeypatch):
    events = []
    class Worker:
        record = SimpleNamespace(sandboxed=True)
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
            return {'mode': 'normal'}
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
        record = SimpleNamespace(sandboxed=True)
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
        'mode': 'normal' if operation == 'agents.security-mode.get.v1' else 'yolo'})
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
@pytest.mark.parametrize('mode,observed_mode', [('yolo', 'learning'), ('normal', 'deny_all')])
async def test_mode_api_synchronizes_passive_network_observation(monkeypatch, mode, observed_mode):
    from starlette.requests import Request
    import json
    applied = []
    async def apply(agent_id, network_mode, version):
        applied.append((agent_id, network_mode))
        return {'active_instances': 1, 'collectors_started': int(mode == 'yolo')}
    runtime = SimpleNamespace(apply_network_learning_policy=apply)
    def invoke(operation, params):
        if operation == 'network-policy.get.v1':
            return {'mode': 'deny_all', 'version': 5}
        return {'mode': mode}
    monkeypatch.setattr(security, '_invoke_sync', invoke)
    payload = json.dumps({'protocolVersion': 1, 'serviceId': 'security', 'requestId': 'test',
                          'operationId': 'agents.security-mode.update.v1',
                          'params': {'agent_id': 'a', 'mode': mode}}).encode()
    async def receive():
        return {'type': 'http.request', 'body': payload, 'more_body': False}
    app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))
    response = await security.invoke_security(Request({'type': 'http', 'app': app}, receive))
    assert response['ok'] is True
    assert applied == [('a', observed_mode)]
