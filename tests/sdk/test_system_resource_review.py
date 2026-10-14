import json
from pathlib import Path

import pytest
import yaml

from bianbu_agent.config import RuntimeConfig
from bianbu_agent.inventory import CapabilityKind
from bianbu_agent.server.runtime import _inventory_snapshot
from tests.sdk.api.test_packages import _skill
from tests.sdk.api.test_runs import _api


def _plugin(root: Path, mcps: tuple[str, ...]):
    root.mkdir(parents=True)
    (root / 'plugin.yaml').write_text(yaml.safe_dump({
        'schema_version': 1, 'name': root.name, 'version': '1.0.0',
        'description': 'System resource review fixture.',
    }))
    for name in mcps:
        path = root / 'mcps' / name
        path.mkdir(parents=True)
        (path / 'mcp.json').write_text(json.dumps({'transport': 'stdio', 'command': '/bin/true'}))


def test_user_plugin_override_removes_shadowed_system_members(tmp_path, monkeypatch):
    monkeypatch.setenv('BIANBU_SYSTEM_RESOURCE_ROOT', str(tmp_path / 'system'))
    _plugin(tmp_path / 'system/plugins/vendor.bundle', ('removed',))
    _plugin(tmp_path / 'user/plugins/vendor.bundle', ('current',))
    snapshot = _inventory_snapshot(RuntimeConfig(data_dir=tmp_path / 'user'))
    mcps = {item.source.capability_id for item in snapshot.items if item.source.kind is CapabilityKind.MCP}
    assert mcps == {'vendor.bundle/current'}


@pytest.mark.asyncio
async def test_system_skill_delete_rejected_before_user_backup(tmp_path, monkeypatch):
    monkeypatch.setenv('BIANBU_SYSTEM_RESOURCE_ROOT', str(tmp_path / 'system'))
    source = _skill(tmp_path / 'system/skills')
    original = (source / 'SKILL.md').read_bytes()
    async with _api(tmp_path / 'runtime') as (client, runtime):
        response = await client.delete('/api/v1/packages/skill/review')
        assert response.status_code == 422, response.text
        assert 'system Skill cannot be uninstalled' in response.text
        assert not (runtime.home.skills_dir / 'review').exists()
    assert (source / 'SKILL.md').read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize('upgrade', [False, True])
@pytest.mark.parametrize('unavailable', [False, True])
async def test_failed_plugin_override_restores_system_without_user_copy(tmp_path, monkeypatch, upgrade, unavailable):
    from fastapi import HTTPException
    from bianbu_agent.api import packages
    from tests.sdk.api.test_packages import _plugin as skill_plugin

    monkeypatch.setenv('BIANBU_SYSTEM_RESOURCE_ROOT', str(tmp_path / 'system'))
    original = skill_plugin(tmp_path / 'system/plugins')
    incoming = skill_plugin(tmp_path / 'incoming', version='2.0.0')
    before = (original / 'plugin.yaml').read_bytes()
    async with _api(tmp_path / 'runtime') as (client, runtime):
        async def reject(_):
            if unavailable:
                raise HTTPException(503, 'Worker unavailable')
            raise RuntimeError('activation rejected')
        monkeypatch.setattr(packages, '_reload_active_worker', reject)
        response = await client.post('/api/v1/plugins', json={'source': str(incoming), 'upgrade': upgrade})
        assert response.status_code == (503 if unavailable else 409), response.text
        assert not (runtime.home.plugins_dir / 'vendor.bundle').exists()
        assert runtime.plugins.package_root('vendor.bundle') == original
    assert (original / 'plugin.yaml').read_bytes() == before


def test_failed_install_cleanup_refuses_replaced_user_package(tmp_path, monkeypatch):
    from bianbu_agent.home import AgentHome
    from bianbu_agent.plugins import PluginInstaller
    from tests.sdk.api.test_packages import _plugin as skill_plugin

    monkeypatch.setenv('BIANBU_SYSTEM_RESOURCE_ROOT', str(tmp_path / 'system'))
    skill_plugin(tmp_path / 'system/plugins')
    source = skill_plugin(tmp_path / 'incoming', version='2.0.0')
    installer = PluginInstaller(AgentHome(tmp_path / 'user').prepare(), file_only=True)
    installed = installer.install(source)
    manifest = tmp_path / 'user/plugins/vendor.bundle/plugin.yaml'
    manifest.write_text(manifest.read_text().replace('2.0.0', '3.0.0'))
    with pytest.raises(ValueError, match='changed before installation rollback'):
        installer.discard_install(installed)
    assert manifest.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_user', [False, True])
async def test_failed_skill_override_restores_previous_layer(tmp_path, monkeypatch, existing_user):
    from bianbu_agent.api import packages

    monkeypatch.setenv('BIANBU_SYSTEM_RESOURCE_ROOT', str(tmp_path / 'system'))
    source = _skill(tmp_path / 'system/skills')
    incoming = _skill(tmp_path / 'incoming')
    manifest = incoming / 'SKILL.md'
    manifest.write_text(manifest.read_text().replace('1.0.0', '2.0.0'))
    async with _api(tmp_path / 'runtime') as (client, runtime):
        if existing_user:
            runtime.skills.install(source)
        async def reject(_):
            raise RuntimeError('activation rejected')
        monkeypatch.setattr(packages, '_reload_active_worker', reject)
        response = await client.post('/api/v1/packages/skill', json={'source': str(incoming), 'upgrade': True})
        assert response.status_code == 409, response.text
        assert response.json()['error']['details']['rolled_back'] is True
        assert (runtime.home.skills_dir / 'review').exists() == existing_user
        assert runtime.skills.inspect('review').version == '1.0.0'


@pytest.mark.asyncio
async def test_system_agent_typed_delete_hides_the_package_and_unregisters_identity(tmp_path, monkeypatch):
    from bianbu_agent.agents import AgentPackageManager, AgentSpec, system_paths
    from bianbu_agent.home import AgentHome
    from tests.sdk.api.test_agents import _file_backed_api

    monkeypatch.setattr(system_paths, 'SYSTEM_PROFILE_ROOT', tmp_path / 'profiles')
    async with _file_backed_api(tmp_path) as (client, runtime):
        system_dir = system_paths.system_agent_directory(runtime.home.root)
        spec = AgentSpec(id='com.bianbu.abcdef012345', name='BibitEdgeAssistant')
        AgentPackageManager(AgentHome(system_dir.parent)).create(spec)
        unregistered = []
        async def unregister(agent_id):
            unregistered.append(agent_id)
        monkeypatch.setattr(runtime, 'unregister_logical_agent', unregister)
        response = await client.delete(f'/api/v1/packages/agent/{spec.id}')
        assert response.status_code == 204, response.text
        # The signed package keeps its declaration; the device remembers the removal.
        assert (system_dir / spec.id / 'agent.yaml').is_file()
        assert runtime.agent_packages.inspect(spec.id) is None
        assert (runtime.home.agents_dir / f'.hidden-{spec.id}').is_file()
        assert unregistered == [spec.id]


@pytest.mark.asyncio
async def test_failed_agent_override_registration_restores_system_layer(tmp_path, monkeypatch):
    from dataclasses import replace
    from bianbu_agent.agents import AgentPackageManager, AgentSpec, system_paths
    from bianbu_agent.home import AgentHome
    from tests.sdk.api.test_agents import _file_backed_api

    monkeypatch.setattr(system_paths, 'SYSTEM_PROFILE_ROOT', tmp_path / 'profiles')
    async with _file_backed_api(tmp_path) as (client, runtime):
        system_dir = system_paths.system_agent_directory(runtime.home.root)
        spec = AgentSpec(id='com.bianbu.abcdef012345', name='BibitEdgeAssistant')
        original = AgentPackageManager(AgentHome(system_dir.parent)).create(spec)
        incoming = AgentPackageManager(AgentHome(tmp_path / 'incoming')).create(spec)
        declaration = incoming.root / 'agent.yaml'
        document = yaml.safe_load(declaration.read_text())
        document['agent']['version'] = '2.0.0'
        declaration.write_text(yaml.safe_dump(document))
        runtime.config = replace(runtime.config, security=replace(runtime.config.security, required=True))
        monkeypatch.setenv('BIANBU_RUNTIME_MODE', 'production')
        async def reject(_):
            raise RuntimeError('registration rejected')
        monkeypatch.setattr(runtime, 'register_logical_agent', reject)
        response = await client.post('/api/v1/packages/agent', json={'source': str(incoming.root), 'upgrade': True})
        assert response.status_code == 503, response.text
        assert not (runtime.home.agents_dir / spec.id).exists()
        assert runtime.agent_packages.inspect(spec.id).root == original.root


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_user', [False, True])
@pytest.mark.parametrize('unavailable', [False, True])
async def test_failed_mcp_override_restores_previous_layer(tmp_path, monkeypatch, existing_user, unavailable):
    from fastapi import HTTPException
    from bianbu_agent.api import mcp_servers
    import bianbu_agent.mcp.system as system

    root = tmp_path / 'system/mcps'
    monkeypatch.setattr(system, 'SYSTEM_MCP_ROOT', root)
    source = root / 'mcp.example'
    source.mkdir(parents=True)
    (source / 'mcp.json').write_text(json.dumps({'transport': 'stdio', 'command': 'old-command'}))
    original = (source / 'mcp.json').read_bytes()
    async with _api(tmp_path / 'runtime') as (client, runtime):
        if existing_user:
            runtime.mcps.install(source)
        async def reject(_):
            if unavailable:
                raise HTTPException(503, 'Worker unavailable')
            raise RuntimeError('activation rejected')
        monkeypatch.setattr(mcp_servers, '_reload_active_worker_bounded', reject)
        response = await client.post('/api/v1/mcp-servers', json={'name': 'Example', 'command': 'new-command', 'upgrade': True})
        assert response.status_code == (503 if unavailable else 409), response.text
        assert (runtime.home.mcps_dir / 'mcp.example').exists() == existing_user
        restored = runtime.mcps.inspect('mcp.example')
        assert (restored.root / 'mcp.json').read_bytes() == original
    assert (source / 'mcp.json').read_bytes() == original


@pytest.mark.asyncio
async def test_system_mcp_user_override_delete_is_explicitly_readonly(tmp_path, monkeypatch):
    import bianbu_agent.mcp.system as system

    root = tmp_path / 'system/mcps'
    monkeypatch.setattr(system, 'SYSTEM_MCP_ROOT', root)
    source = root / 'mcp.example'
    source.mkdir(parents=True)
    (source / 'mcp.json').write_text(json.dumps({'transport': 'stdio', 'command': '/bin/true'}))
    async with _api(tmp_path / 'runtime') as (client, runtime):
        runtime.mcps.install(source)
        response = await client.delete('/api/v1/mcp-servers', params={'id': 'mcp.example'})
        assert response.status_code == 409, response.text
        assert 'mcp_managed_by_system' in response.text
        assert (runtime.home.mcps_dir / 'mcp.example').exists()
