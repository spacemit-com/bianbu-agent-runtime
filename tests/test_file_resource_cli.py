"""Public CLI remains a local file-management interface."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ('agent', 'skill', 'plugin', 'mcp', 'model', 'session')

@pytest.mark.parametrize('command', ('init', 'check', 'build', 'install', 'run', 'stop', 'logs', 'status', 'uninstall', 'config', 'process'))
def test_removed_commands_are_rejected(command):
    from bianbu_agent.cli import build_parser
    with pytest.raises(SystemExit) as result:
        build_parser().parse_args([command])
    assert result.value.code == 2

@pytest.mark.parametrize('resource', RESOURCES)
def test_resource_list_without_execution_dependencies(tmp_path, resource):
    config = tmp_path / 'config.yaml'
    config.write_text('data_dir: data\n')
    script = '''
import importlib.abc
import runpy
import sys
class BlockExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'langchain', 'langchain_core', 'langgraph', 'fastapi', 'uvicorn', 'jsonschema'} or fullname.startswith(('bianbu_agent.server', 'bianbu_agent.security.runtime_process')):
            raise AssertionError('file command loaded execution dependency: ' + fullname)
sys.meta_path.insert(0, BlockExecution())
sys.argv = ['bianbu-agent', *sys.argv[1:]]
runpy.run_module('bianbu_agent.cli', run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-c', script, '--config', str(config), resource, 'list'], cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)}, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '[]'


def test_offline_resource_lifecycles(tmp_path):
    """Every action works with execution, network and child processes blocked."""
    import json
    config = tmp_path / 'config.yaml'
    config.write_text('data_dir: data\n')
    script = '''
import importlib.abc
import runpy
import sys
class BlockExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'langchain', 'langchain_core', 'langgraph', 'fastapi', 'uvicorn', 'mcp', 'bianbu_security_client', 'jsonschema'} or fullname.startswith(('bianbu_agent.server', 'bianbu_agent.security.runtime_process', 'bianbu_agent.security.execution_runtime', 'bianbu_agent.security.security_runtime', 'bianbu_agent.security.security_codesign')):
            raise AssertionError('file command loaded execution dependency: ' + fullname)
def audit(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system', 'os.fork', 'os.posix_spawn'}:
        raise AssertionError('file command attempted external action: ' + event)
sys.addaudithook(audit)
sys.meta_path.insert(0, BlockExecution())
sys.argv = ['bianbu-agent', *sys.argv[1:]]
runpy.run_module('bianbu_agent.cli', run_name='__main__')
'''
    def run(*args, code=0):
        result = subprocess.run([sys.executable, '-c', script, '--config', str(config), *args], cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT)}, text=True, capture_output=True, timeout=15)
        assert result.returncode == code, result.stderr
        return json.loads(result.stdout) if result.stdout else None

    agent = run('agent', 'create', 'Example', '--provider', 'local', '--model', 'example')['id']
    assert run('agent', 'list')[0]['id'] == agent
    assert run('agent', 'show', agent)['id'] == agent
    # ``create`` takes a display name, so reporting must accept it too.
    assert run('agent', 'show', 'Example')['id'] == agent
    assert run('agent', 'verify', agent) == {'valid': True}
    assert run('agent', 'verify', 'Example') == {'valid': True}
    assert run('session', 'create', 'example', '--agent', agent, '--title', 'Example')['agent_id'] == agent
    assert run('session', 'list')[0]['id'] == 'example'
    assert run('session', 'show', 'example')['title'] == 'Example'
    assert run('session', 'remove', 'example') == {'deleted': True}
    assert run('agent', 'remove', 'Example') == {'deleted': True}
    assert run('model', 'create', 'local', 'local', 'example')['id'] == 'local'
    assert run('model', 'list')[0]['id'] == 'local'
    assert run('model', 'show', 'local')['id'] == 'local'
    assert run('model', 'remove', 'local') == {'deleted': True}
    run('model', 'bootstrap-chat', code=2)  # no TTY: prompt exits immediately

    for resource, name, file, content in (
        ('skill', 'example.skill', 'SKILL.md', '---\nname: example.skill\ndescription: Offline example\n---\nInstructions\n'),
    ):
        source = tmp_path / 'sources' / name
        source.mkdir(parents=True)
        (source / file).write_text(content)
        run(resource, 'install', str(source))
        assert len(run(resource, 'list')) == 1
        assert run(resource, 'show', name)
        assert run(resource, 'verify', name) == {'valid': True}
        assert run(resource, 'remove', name) == {'deleted': True}
        assert run(resource, 'list') == []

    run(
        'mcp', 'add', 'example', '--command', 'does-not-exist', '--no-verify',
        '--args', '--port', '9000',
    )
    assert run('mcp', 'get', 'example')
    assert run('mcp', 'remove', 'example') == {'deleted': True}


def test_mcp_remote_and_stdio_auth_configuration(tmp_path, capsys, monkeypatch):
    import asyncio, json
    from bianbu_agent.cli import main_async
    config = tmp_path / 'config.yaml'
    config.write_text('data_dir: data\n')
    prefix = ['--config', str(config), 'mcp']
    monkeypatch.setattr('getpass.getpass', lambda _prompt: 'Bearer secret')
    asyncio.run(main_async([*prefix, 'add', 'remote', '--url', 'https://example.test/mcp', '--auth', 'header']))
    remote = json.loads(capsys.readouterr().out)
    assert remote['id'] == 'mcp.remote'
    descriptor = tmp_path / 'data/mcps/mcp.remote/mcp.json'
    document = json.loads(descriptor.read_text())
    assert document == {
        'display_name': 'remote', 'transport': 'streamable-http',
        'url': 'https://example.test/mcp',
        'headers': {'Authorization': 'Bearer ${MCP_REMOTE_API_KEY}'},
        'timeout_seconds': 30.0,
        'startup_timeout_seconds': 120.0,
    }
    with pytest.raises(ValueError, match='only be used with stdio'):
        asyncio.run(main_async([*prefix, 'add', 'bad', '--url', 'https://example.test', '--env', 'X=Y']))


def test_hook_management_is_not_a_resource_command():
    from bianbu_agent.cli import build_parser
    with pytest.raises(SystemExit) as result:
        build_parser().parse_args(['hook', 'list'])
    assert result.value.code == 2


def test_only_resource_and_service_commands_are_exposed():
    import argparse
    from bianbu_agent.cli import build_parser
    parser = build_parser()
    subcommands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    assert set(subcommands.choices) == {*RESOURCES, 'serve', 'worker'}
    assert 'hook' not in subcommands.choices


def test_agent_show_does_not_scan_unrelated_packages(tmp_path, capsys):
    import asyncio
    import json
    from bianbu_agent.cli import main_async
    config = tmp_path / 'config.yaml'
    config.write_text('data_dir: data\n')
    prefix = ['--config', str(config), 'agent']
    asyncio.run(main_async([*prefix, 'create', 'Example', '--provider', 'test', '--model', 'test']))
    agent = json.loads(capsys.readouterr().out)['id']
    broken = tmp_path / 'data/agents/broken.agent'
    broken.mkdir()
    (broken / 'agent.yaml').write_text('invalid: [')
    assert asyncio.run(main_async([*prefix, 'show', agent])) == 0
    assert json.loads(capsys.readouterr().out)['id'] == agent


def test_agent_show_rejects_an_ambiguous_display_name(tmp_path, capsys):
    import asyncio
    from bianbu_agent.cli import main_async
    config = tmp_path / 'config.yaml'
    config.write_text('data_dir: data\n')
    prefix = ['--config', str(config), 'agent']
    for _ in range(2):
        asyncio.run(main_async([*prefix, 'create', 'Twin', '--provider', 'test', '--model', 'test']))
        capsys.readouterr()
    with pytest.raises(ValueError, match='ambiguous'):
        asyncio.run(main_async([*prefix, 'show', 'Twin']))


def test_plugin_members_narrows_the_report_to_one_namespace(tmp_path, capsys):
    import asyncio
    import json
    from bianbu_agent.cli import main_async
    config = tmp_path / 'config.yaml'
    config.write_text('data_dir: data\n')
    source = tmp_path / 'plugin'
    member = source / 'skills' / 'member'
    member.mkdir(parents=True)
    (member / 'SKILL.md').write_text(
        '---\nname: member\ndescription: Fixture member\n---\nInstructions\n'
    )
    (source / 'plugin.yaml').write_text(
        'schema_version: 1\nname: example.plugin\nversion: 1.0.0\n'
        'description: Fixture plugin.\n'
    )
    prefix = ['--config', str(config), 'plugin']
    asyncio.run(main_async([*prefix, 'install', str(source)]))
    capsys.readouterr()
    asyncio.run(main_async([*prefix, 'members', 'example.plugin']))
    members = json.loads(capsys.readouterr().out)
    assert [(item['namespace'], item['local_name']) for item in members] == [
        ('example.plugin', 'member')
    ]
    asyncio.run(main_async([*prefix, 'members', 'other.plugin']))
    assert json.loads(capsys.readouterr().out) == []


def _help_paths():
    import argparse
    from bianbu_agent.cli import build_parser
    def visit(parser, prefix):
        yield prefix
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    yield from visit(child, [*prefix, name])
    return list(visit(build_parser(), []))


@pytest.mark.parametrize('arguments', _help_paths())
def test_all_help_paths_are_independent_of_config_and_runtime(arguments):
    script = '''
import importlib.abc
import runpy
import sys
class BlockConfig(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('bianbu_agent.config', 'bianbu_agent.server', 'bianbu_agent.security', 'bianbu_agent.models')) or fullname.split('.')[0] in {'langchain', 'langchain_core', 'langgraph', 'uvicorn', 'fastapi'}:
            raise AssertionError('help imported: ' + fullname)
sys.meta_path.insert(0, BlockConfig())
sys.argv = ['bianbu-agent', *sys.argv[1:], '--help']
runpy.run_module('bianbu_agent.cli', run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-c', script, *arguments], cwd=ROOT,
                            env={**os.environ, 'PYTHONPATH': str(ROOT)},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout
