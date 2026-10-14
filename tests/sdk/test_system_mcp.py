import json
from pathlib import Path

import httpx
import pytest

from bianbu_agent import BianbuAgentRuntime, RuntimeConfig
from bianbu_agent.inventory import CapabilityKind
from bianbu_agent.server.runtime import _inventory_snapshot
from bianbu_agent.server.app import create_app


def manifest(root):
    root.mkdir(parents=True)
    (root / 'mcp.json').write_text(json.dumps({'display_name': 'System Intent',
        'transport': 'stdio', 'command': '/does-not-exist/intent'}))


@pytest.mark.asyncio
async def test_system_mcp_available_without_worker_and_not_removable(tmp_path, monkeypatch):
    import bianbu_agent.mcp.system as system
    monkeypatch.setattr(system, 'SYSTEM_MCP_ROOT', tmp_path / 'system')
    manifest(system.SYSTEM_MCP_ROOT / 'bibit-intent')
    for name in ('alice', 'bob'):
        runtime = BianbuAgentRuntime(config=RuntimeConfig(data_dir=tmp_path / name),
                                    default_model=None, agent_id='default', role='control')
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(
                app=create_app(runtime, manage_lifespan=False)), base_url='http://test') as client:
                response = await client.get('/api/v1/mcp-servers')
                assert response.status_code == 200
                entry = next(x for x in response.json()['data'] if x['id'] == 'bibit-intent')
                assert entry['removable'] is False
                assert entry['source_kind'] == 'system'
                assert entry['status'] == 'not_loaded'
                response = await client.delete('/api/v1/mcp-servers', params={'id': 'bibit-intent'})
                assert response.status_code == 409
                assert 'mcp_managed_by_system' in response.text
            assert not (runtime.config.data_dir / 'mcps' / 'bibit-intent').exists()
        finally:
            await runtime.close()
    assert (system.SYSTEM_MCP_ROOT / 'bibit-intent' / 'mcp.json').exists()


def test_user_mcp_overrides_system_without_modifying_system(tmp_path, monkeypatch):
    import bianbu_agent.mcp.system as system
    monkeypatch.setattr(system, 'SYSTEM_MCP_ROOT', tmp_path / 'system')
    manifest(system.SYSTEM_MCP_ROOT / 'bibit-intent')
    local = tmp_path / 'user' / 'mcps' / 'bibit-intent'
    manifest(local)
    snapshot = _inventory_snapshot(RuntimeConfig(data_dir=tmp_path / 'user'))
    records = [x for x in snapshot.items if x.source.kind is CapabilityKind.MCP]
    assert len(records) == 1
    assert records[0].source.root == local
    assert not records[0].source.metadata.get('system_managed')
