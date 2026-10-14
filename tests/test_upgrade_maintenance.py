import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bianbu_agent.server import maintenance


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    path = tmp_path / 'update.json'
    monkeypatch.setattr(maintenance, 'DETAIL', path)
    original = maintenance.os.fstat
    monkeypatch.setattr(maintenance.os, 'fstat', lambda descriptor: SimpleNamespace(
        st_uid=0, st_mode=original(descriptor).st_mode,
    ))
    path.write_text(json.dumps({'state': 'agent_upgrading'}))
    path.chmod(0o644)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize('unit,blocked', [('active', True), ('inactive', False), ('failed', False)])
async def test_live_unit_required(evidence, monkeypatch, unit, blocked):
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(unit.encode(), b'')))
    monkeypatch.setattr(maintenance.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    result = await maintenance.upgrade_snapshot()
    assert result['maintenance'] is blocked
    if not blocked:
        assert result['state'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['succeeded', 'failed', 'cancelled', 'succeeded_with_skips', 'updating'])
async def test_terminal_and_normal_updates_do_not_block(evidence, monkeypatch, state):
    evidence.write_text(json.dumps({'state': state}))
    probe = AsyncMock()
    monkeypatch.setattr(maintenance.asyncio, 'create_subprocess_exec', probe)
    assert not (await maintenance.upgrade_snapshot())['maintenance']
    probe.assert_not_called()


@pytest.mark.asyncio
async def test_untrusted_or_invalid_evidence(evidence):
    evidence.chmod(0o666)
    assert not (await maintenance.upgrade_snapshot())['maintenance']
    evidence.chmod(0o644)
    evidence.write_text('invalid')
    assert not (await maintenance.upgrade_snapshot())['maintenance']
    evidence.unlink()
    assert not (await maintenance.upgrade_snapshot())['maintenance']


def test_write_gate_and_recovery(monkeypatch):
    snapshot = {'maintenance': True, 'state': 'agent_upgrading', 'message': 'upgrading'}
    monkeypatch.setattr(maintenance, 'upgrade_snapshot', AsyncMock(side_effect=lambda: dict(snapshot)))
    app = FastAPI()
    app.add_middleware(maintenance.UpgradeMaintenanceMiddleware)

    @app.get('/api/read')
    async def read():
        return {'ok': True}

    @app.post('/api/write')
    async def write():
        return {'ok': True}

    with TestClient(app) as client:
        assert client.get('/api/read').status_code == 200
        response = client.post('/api/write')
        assert response.status_code == 503
        assert response.json()['error']['code'] == 'runtime_upgrading'
        snapshot['maintenance'] = False
        assert client.post('/api/write').status_code == 200
