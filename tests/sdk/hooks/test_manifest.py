import json
from pathlib import Path
import pytest
from bianbu_agent.hooks.command import inspect_command_hook


@pytest.mark.parametrize('change', [
    {'schema_version': 2}, {'schema_version': True}, {'priority': 'high'},
    {'metadata': []}, {'entrypoint': 'hook:create_hook'}, {'hooks': {}},
])
def test_command_configuration_rejects_invalid_schema(tmp_path, change):
    data = {'schema_version': 1, 'hooks': {'BeforeRun': [{'hooks': [
        {'type': 'command', 'command': 'true'}]}]}}
    data.update(change)
    path = tmp_path / 'hook.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        inspect_command_hook(path)


def _manifest(**change):
    data = {
        'schema_version': 1,
        'hooks': {'BeforeRun': [{'hooks': [
            {'type': 'command', 'command': 'true'}]}]},
    }
    data.update(change)
    return data


@pytest.mark.parametrize('key', ['parallel', 'waits_for'])
def test_command_configuration_has_no_scheduling_keys(tmp_path, key):
    """Every Hook already runs concurrently; scheduling is not plugin configurable."""

    path = tmp_path / 'hook.json'
    path.write_text(json.dumps(_manifest(**{key: True if key == 'parallel' else ['*']})))
    with pytest.raises(ValueError):
        inspect_command_hook(path)


SHIPPED_HOOKS = Path(__file__).resolve().parents[3] / 'resources/plugins'


def test_shipped_command_hooks_declare_a_loadable_manifest():
    manifests = sorted(SHIPPED_HOOKS.glob('*/hooks/*/hook.json'))
    assert manifests
    for path in manifests:
        event, commands = inspect_command_hook(path)
        assert event and commands
        data = json.loads(path.read_text())
        assert 'parallel' not in data and 'waits_for' not in data
