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
        'parallel': True,
        'hooks': {'BeforeRun': [{'hooks': [
            {'type': 'command', 'command': 'true'}]}]},
    }
    data.update(change)
    return data


@pytest.mark.parametrize('waits_for', [
    'bianbu.security/*', [''] , [1], ['*' * 513], ['*'] * 65,
])
def test_command_configuration_rejects_invalid_waits_for(tmp_path, waits_for):
    path = tmp_path / 'hook.json'
    path.write_text(json.dumps(_manifest(waits_for=waits_for)))
    with pytest.raises(ValueError):
        inspect_command_hook(path)


def test_command_configuration_requires_parallel_for_waits_for(tmp_path):
    path = tmp_path / 'hook.json'
    path.write_text(json.dumps(_manifest(parallel=False, waits_for=['bianbu.security/*'])))
    with pytest.raises(ValueError, match='waits_for requires parallel'):
        inspect_command_hook(path)


def test_command_configuration_publishes_waits_for(tmp_path):
    path = tmp_path / 'hook.json'
    path.write_text(json.dumps(_manifest(waits_for=['bianbu.security/*'])))
    event, commands, waits_for = inspect_command_hook(path)
    assert event == 'BeforeRun'
    assert len(commands) == 1
    assert waits_for == ('bianbu.security/*',)


SHIPPED_HOOKS = Path(__file__).resolve().parents[3] / 'resources/plugins'


def test_shipped_command_hooks_declare_a_loadable_manifest():
    manifests = sorted(SHIPPED_HOOKS.glob('*/hooks/*/hook.json'))
    assert manifests
    for path in manifests:
        event, commands, waits_for = inspect_command_hook(path)
        assert event and commands
        assert isinstance(waits_for, tuple)


def test_intent_device_hook_waits_for_the_security_decision_only():
    """The device-side Hook must overlap best-effort Hooks, not queue behind them."""

    event, _, waits_for = inspect_command_hook(
        SHIPPED_HOOKS / 'bianbu.bibit.intent/hooks/bibit-intent.before_run/hook.json'
    )
    assert event == 'BeforeRun'
    assert waits_for == ('bianbu.security/*',)
