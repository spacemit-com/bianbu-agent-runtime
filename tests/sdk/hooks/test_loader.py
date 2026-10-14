import json
from pathlib import Path

import pytest

from bianbu_agent.hooks import DirectoryHookLoader, HookLoadContext
from bianbu_agent.inventory.models import CapabilityOrigin


def source(tmp_path, *, name='example.check', priority=100):
    root = tmp_path / name
    root.mkdir()
    (root / 'hook.json').write_text(json.dumps({'schema_version': 1, 'priority': priority,
        'metadata': {'display_name': 'Check'}, 'hooks': {'BeforeRun': [
        {'hooks': [{'type': 'command', 'command': 'printf "{}"'}]}]}}))
    return root


def test_plugin_command_identity_priority_and_display(tmp_path):
    root = source(tmp_path, priority=200)
    hook = DirectoryHookLoader().load(root, origin=CapabilityOrigin.PLUGIN,
        plugin_id='example.plugin', context=HookLoadContext(tmp_path, tmp_path / 'data'))
    assert hook.id == 'example.check'
    assert hook.plugin_id == 'example.plugin'
    assert hook.priority == 200
    assert hook.metadata['display_name'] == 'Check'


def test_standalone_commands_rejected(tmp_path):
    with pytest.raises(ValueError, match='belong to a plugin'):
        DirectoryHookLoader().load(source(tmp_path), context=HookLoadContext(tmp_path, tmp_path))


@pytest.mark.parametrize('extra', ['hook.py', 'requirements.txt', 'hook.toml', 'resources/extra.txt'])
def test_only_configuration_belongs_in_hook_directory(tmp_path, extra):
    root = source(tmp_path)
    path = root / extra
    path.parent.mkdir(exist_ok=True)
    path.write_text('ignored')
    with pytest.raises(ValueError, match='only hook.json'):
        DirectoryHookLoader.inspect_single_file(root)


def test_bytecode_ignored_but_configuration_digest_changes(tmp_path):
    root = source(tmp_path)
    before = DirectoryHookLoader.inspect_single_file(root)
    (root / '__pycache__').mkdir()
    (root / '__pycache__/hook.cpython-314.pyc').write_bytes(b'cache')
    assert DirectoryHookLoader.inspect_single_file(root) == before
    (root / 'hook.json').write_text((root / 'hook.json').read_text().replace('100', '200'))
    assert DirectoryHookLoader.inspect_single_file(root)[1] != before[1]


def test_symlink_and_invalid_identity_rejected(tmp_path):
    root = source(tmp_path)
    link = tmp_path / 'example.link'
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match='symbolic'):
        DirectoryHookLoader.inspect_single_file(link)
    invalid = source(tmp_path, name='UPPER')
    with pytest.raises(ValueError, match='identifier'):
        DirectoryHookLoader.inspect_single_file(invalid)
