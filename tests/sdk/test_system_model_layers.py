from dataclasses import replace
from pathlib import Path

import pytest

from bianbu_agent.models.file_store import ModelConfig, YAMLModelStore, _SecureModelRecordDirectory, _encode
from bianbu_agent.models.file_resolver import YAMLModelResolver
from bianbu_agent.agents import AgentModel


def system_provider(tmp_path, monkeypatch):
    monkeypatch.setenv('BIANBU_SYSTEM_RESOURCE_ROOT', str(tmp_path / 'system'))
    records = _SecureModelRecordDirectory(tmp_path / 'system/models')
    config = ModelConfig(id='custom-provider-test', provider='custom:test', model='credential', base_url='http://127.0.0.1:8000/v1', parameters={'record_kind': 'custom_provider', 'custom_id': 'test', 'api_key': 'fixture-key'})
    records.create(config.id, _encode(config))
    records.path(config.id).chmod(0o644)
    return records, config


@pytest.mark.asyncio
async def test_system_models_read_and_user_updates_do_not_write_system(tmp_path, monkeypatch):
    system, config = system_provider(tmp_path, monkeypatch)
    original = system.path(config.id).read_bytes()
    store = YAMLModelStore(tmp_path / 'user/models')
    await store.open()
    assert await store.get(config.id) == config
    assert [item.id for item in await store.list()] == [config.id]
    changed = replace(config, base_url='http://127.0.0.1:9000/v1')
    await store.update(changed)
    assert await store.get(config.id) == changed
    assert system.path(config.id).read_bytes() == original
    assert (tmp_path / 'user/models/custom-test/provider.yaml').stat().st_mode & 0o777 == 0o600
    resolver = YAMLModelResolver(tmp_path / 'user/models')
    assert resolver.resolve_agent_model(AgentModel('custom:test', 'demo')).base_url == changed.base_url
    assert await store.delete(config.id) is True
    assert await store.get(config.id) is None
    assert [item.id for item in await store.list()] == []
    assert system.path(config.id).read_bytes() == original
    marker = tmp_path / 'user/models/.hidden' / config.id
    assert marker.is_file()
    assert marker.stat().st_mode & 0o777 == 0o600
    assert await store.delete(config.id) is False
    # Re-creating the Provider is how a device takes the packaged default back.
    await store.create(config)
    assert await store.get(config.id) == config
    assert not marker.exists()
    await store.close()


@pytest.mark.asyncio
async def test_system_model_permissions_reject_writable_package_records(tmp_path, monkeypatch):
    system, config = system_provider(tmp_path, monkeypatch)
    system.path(config.id).chmod(0o666)
    store = YAMLModelStore(tmp_path / 'user/models')
    with pytest.raises(Exception, match='permissions unsafe'):
        await store.open()
        await store.get(config.id)


@pytest.mark.asyncio
async def test_failed_system_pair_edit_removes_user_overrides(tmp_path, monkeypatch):
    system, provider = system_provider(tmp_path, monkeypatch)
    capability = ModelConfig(id='capability-test', provider='custom:test', model='old', parameters={'capability': 'test'})
    system.create(capability.id, _encode(capability))
    system.path(capability.id).chmod(0o644)
    before = {path: path.read_bytes() for path in system.root.rglob('*.yaml')}
    store = YAMLModelStore(tmp_path / 'user/models')
    await store.open()
    original_create = store._records.user.create

    def fail_after_first_write(record_id, payload):
        original_create(record_id, payload)
        raise OSError('simulated write failure')

    monkeypatch.setattr(store._records.user, 'create', fail_after_first_write)
    with pytest.raises(OSError, match='simulated write failure'):
        await store.upsert_project_capability(capability='test', model='new')
    assert not list((tmp_path / 'user/models').rglob('*.yaml'))
    assert await store.get(capability.id) == capability
    assert {path: path.read_bytes() for path in before} == before
    await store.close()
