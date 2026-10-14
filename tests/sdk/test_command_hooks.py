import asyncio
import json
from pathlib import Path

import pytest

from bianbu_agent.hooks.loader import DirectoryHookLoader
from bianbu_agent.hooks.models import HookLoadContext
from bianbu_agent.inventory.models import CapabilityOrigin
from bianbu_agent.plugins.contributions import HookPoint, RunContext


def load(tmp_path, script, *, event='PreToolUse', timeout=1, on_timeout=None):
    plugin = tmp_path / 'example.commands'
    root = plugin / 'hooks' / 'example.check'
    root.mkdir(parents=True)
    (plugin / 'scripts').mkdir()
    (plugin / 'scripts' / 'check.py').write_text(script)
    handler = {'type': 'command',
               'command': 'python3 "${PLUGIN_ROOT}/scripts/check.py"', 'timeout': timeout}
    if on_timeout is not None:
        handler['on_timeout'] = on_timeout
    (root / 'hook.json').write_text(json.dumps({'schema_version': 1, 'hooks': {
        event: [{'matcher': 'terminal', 'hooks': [handler]}]}}))
    return DirectoryHookLoader().load(root, origin=CapabilityOrigin.PLUGIN,
        plugin_id='example.commands', context=HookLoadContext(
            workspace_root=tmp_path, data_root=tmp_path / 'data'))


@pytest.mark.asyncio
async def test_command_hook_stdin_identity_matching_and_context(tmp_path):
    hook = load(tmp_path, 'import json,sys\np=json.load(sys.stdin)\nassert p["agent_id"] == "expert"\nassert p["tool_input"] == {"command":"pwd"}\nprint(json.dumps({"context_sections":[p["session_id"]]}))')
    ctx = RunContext('run', 'session', 'expert', 'terminal', metadata={'tool':'terminal', 'arguments':{'command':'pwd'}, 'agent_id':'forged'})
    patch = await hook.handler(ctx)
    assert patch.context_sections == ('session',)
    assert await hook.handler(RunContext('r', 's', 'a', 'read_file', metadata={'tool':'read_file'})) is None


@pytest.mark.asyncio
async def test_command_hook_deny_and_timeout_are_closed(tmp_path):
    hook = load(tmp_path, 'print(\'{"decision":"deny","reason":"blocked"}\')')
    with pytest.raises(PermissionError, match='blocked'):
        await hook.handler(RunContext('r','s','a','terminal',metadata={'tool':'terminal'}))


@pytest.mark.asyncio
async def test_gate_hook_timeout_names_hook_and_budget(tmp_path):
    hook = load(tmp_path, 'import time\ntime.sleep(30)\n', timeout=0.4)
    with pytest.raises(PermissionError) as failure:
        await hook.handler(RunContext('r','s','a','terminal',metadata={'tool':'terminal'}))
    message = str(failure.value)
    assert 'Command Hook failed' in message
    assert 'timed out after 0.4s' in message
    assert '[example.commands/example.check]' in message


@pytest.mark.asyncio
async def test_best_effort_gate_hook_timeout_is_skipped(tmp_path):
    hook = load(tmp_path, 'import time\ntime.sleep(30)\n', timeout=0.4, on_timeout='skip')
    patch = await hook.handler(RunContext('r','s','a','terminal',metadata={'tool':'terminal'}))
    assert patch.context_sections == ()
    assert patch.metadata == {}


def test_command_hook_rejects_unknown_on_timeout_value(tmp_path):
    with pytest.raises(ValueError, match='on_timeout'):
        load(tmp_path, 'print("{}")', on_timeout='ignore')


@pytest.mark.asyncio
async def test_command_hook_direct_reply_before_run(tmp_path):
    hook = load(tmp_path, 'print(\'{"direct_reply":"hello","context_sections":["memory"]}\')', event='BeforeRun')
    patch = await hook.handler(RunContext('r','s','a','hello'))
    assert patch.direct_reply == 'hello'
    assert patch.context_sections == ('memory',)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_command_hook_cleans_descendants_on_timeout_or_cancel(tmp_path, cancel):
    pidfile = tmp_path / 'child.pid'
    script = ("import subprocess,time,pathlib\n"
              "child=subprocess.Popen(['sleep','60'])\n"
              f"pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))\n"
              "time.sleep(60)\n")
    hook = load(tmp_path, script, timeout=0.4 if not cancel else 30)
    task = asyncio.create_task(hook.handler(RunContext('r','s','a','terminal',metadata={'tool':'terminal'})))
    async with asyncio.timeout(3):
        while not pidfile.exists():
            await asyncio.sleep(0.01)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else PermissionError):
        await task
    child = int(pidfile.read_text())
    # A zombie is terminated and awaits the OS subreaper, not a running child.
    async with asyncio.timeout(3):
        while True:
            try:
                state = Path(f'/proc/{child}/stat').read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                break
            if state == 'Z':
                break
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_hook_dispatch_propagates_cancellation():
    from tests.sdk.plugins.test_hooks import _generation, _context
    from bianbu_agent.plugins.contributions import HookContribution
    async def cancelled(_):
        raise asyncio.CancelledError()
    generation = _generation((HookContribution('example.commands', HookPoint.BEFORE_TOOL, cancelled),))
    with pytest.raises(asyncio.CancelledError):
        await generation.run_hooks(HookPoint.BEFORE_TOOL, _context())


@pytest.mark.asyncio
@pytest.mark.parametrize('output', ['{"unexpected":true}', '{"decision":"ask"}', '{"direct_reply":"bad"}', 'not json'])
async def test_invalid_gate_output_fails_closed(tmp_path, output):
    hook = load(tmp_path, f'print({output!r})')
    with pytest.raises(PermissionError):
        await hook.handler(RunContext('r','s','a','terminal',metadata={'tool':'terminal'}))


@pytest.mark.asyncio
async def test_plugin_command_scripts_are_frozen_until_plugin_stop(tmp_path):
    from types import SimpleNamespace
    from bianbu_agent.plugins.directory import DirectoryPackagePlugin
    from bianbu_agent.plugins.models import PluginManifest, PluginContributionManifest
    load(tmp_path, 'print(\'{"context_sections":["old"]}\')')
    root = tmp_path / 'example.commands'
    hooks = []
    scope = SimpleNamespace(service_view={}, register_hook=hooks.append)
    data = tmp_path / 'data'; data.mkdir(exist_ok=True)
    manifest = PluginManifest(id='example.commands', version='1.0.0',
        contributions=PluginContributionManifest(hooks=('example.check',)))
    plugin = DirectoryPackagePlugin(manifest, root=root, workspace=tmp_path)
    context = SimpleNamespace(plugin_id=manifest.id, scope=scope, data_dir=data, config={})
    await plugin.setup(context)
    try:
        (root / 'scripts/check.py').write_text('print(\'{"context_sections":["new"]}\')')
        patch = await hooks[0].handler(RunContext('r','s','a','terminal',metadata={'tool':'terminal'}))
        assert patch.context_sections == ('old',)
        frozen = Path(hooks[0].metadata['definition_path'])
        assert frozen.is_file()
    finally:
        await plugin.stop()
    assert not frozen.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [{'large':'x' * (1024 * 1024)}, {'not_json':object()}])
async def test_dispatch_does_not_ignore_precommand_protocol_errors(tmp_path, arguments):
    from tests.sdk.plugins.test_hooks import _generation
    hook = load(tmp_path, 'print(\'{"decision":"allow"}\')')
    generation = _generation((hook,))
    with pytest.raises(PermissionError, match='Command Hook failed'):
        await generation.run_hooks(HookPoint.BEFORE_TOOL,
            RunContext('r','s','a','terminal',metadata={'tool':'terminal','arguments':arguments}))


@pytest.mark.asyncio
async def test_command_metadata_and_plugin_paths(tmp_path):
    hook = load(tmp_path, '''import json,sys,os
p=json.load(sys.stdin)
assert p['metadata']['call_id'] == 'call-1'
assert os.path.isdir(os.environ['PLUGIN_DATA_ROOT'])
assert os.environ['PLUGIN_WORKSPACE_ROOT'] == os.getcwd()
print(json.dumps({'metadata': {'observed': p['metadata']['call_id']}}))
''')
    patch = await hook.handler(RunContext('r', 's', 'a', 'test', metadata={'tool': 'terminal', 'call_id': 'call-1'}))
    assert patch.metadata == {'observed': 'call-1'}


def test_python_hook_factory_is_rejected(tmp_path):
    root = tmp_path / 'example.old'
    root.mkdir()
    (root / 'hook.py').write_text('raise AssertionError("must never import")')
    with pytest.raises(ValueError, match='hook.json'):
        DirectoryHookLoader.inspect_single_file(root)


@pytest.mark.asyncio
async def test_command_structured_security_denial(tmp_path):
    hook = load(tmp_path, '''import json
print(json.dumps({'decision': 'deny', 'reason': 'blocked', 'error': {
 'tool': 'terminal', 'capability': 'terminal', 'policy': 'deny',
 'reason': 'blocked', 'degraded': True, 'terminate_run': True}}))
''')
    with pytest.raises(PermissionError) as captured:
        await hook.handler(RunContext('r', 's', 'a', 'test', metadata={'tool': 'terminal'}))
    assert captured.value.capability == 'terminal'
    assert captured.value.policy == 'deny'
    assert captured.value.should_terminate_run
    assert captured.value.to_metadata()['degraded'] is True


@pytest.mark.asyncio
async def test_cancel_gives_script_time_to_finish_cleanup(tmp_path):
    ready = tmp_path / 'ready'
    cleaned = tmp_path / 'cleaned'
    hook = load(tmp_path, f'''import signal,time,pathlib

def stop(*args):
    time.sleep(.1)
    pathlib.Path({str(cleaned)!r}).write_text('done')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
pathlib.Path({str(ready)!r}).touch()
while True: time.sleep(.01)
''', timeout=30)
    task = asyncio.create_task(hook.handler(RunContext('r','s','a','test',metadata={'tool':'terminal'})))
    async with asyncio.timeout(3):
        while not ready.exists():
            await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.read_text() == 'done'


@pytest.mark.asyncio
async def test_private_state_is_session_scoped_and_never_projected(tmp_path):
    hook = load(tmp_path, '''import json,sys
p=json.load(sys.stdin)
count=p['state'].get('count', 0)+1
print(json.dumps({'state': {'count': count}, 'context_sections': [str(count)]}))
''')
    run = RunContext('r', 's', 'a', 'test', metadata={'tool': 'terminal'})
    assert (await hook.handler(run)).context_sections == ('1',)
    second = await hook.handler(run)
    assert second.context_sections == ('2',)
    assert not second.metadata
    other = RunContext('r2', 'other', 'a', 'test', metadata={'tool': 'terminal'})
    assert (await hook.handler(other)).context_sections == ('1',)


@pytest.mark.asyncio
async def test_each_command_receives_preceding_commands_state(tmp_path):
    hook = load(tmp_path, '''import json,sys
p=json.load(sys.stdin)
x=p['state'].get('x', 0)+1
print(json.dumps({'state': {'x': x}, 'context_sections': [str(x)]}))
''')
    root = tmp_path / 'example.commands/hooks/example.check'
    path = root / 'hook.json'
    definition = json.loads(path.read_text())
    handlers = definition['hooks']['PreToolUse'][0]['hooks']
    handlers.append(dict(handlers[0]))
    path.write_text(json.dumps(definition))
    hook = DirectoryHookLoader().load(root, origin=CapabilityOrigin.PLUGIN,
        plugin_id='example.commands', context=HookLoadContext(tmp_path, tmp_path/'data'))
    result = await hook.handler(RunContext('r','s','a','test', metadata={'tool':'terminal'}))
    assert result.context_sections == ('1', '2')


@pytest.mark.asyncio
@pytest.mark.parametrize('event', ['PreToolUse', 'PostToolUse'])
async def test_real_tool_context_projects_engine_controls_out_of_command_json(tmp_path, event):
    from bianbu_agent.engines.langgraph.plugin_tools import build_plugin_tools
    from bianbu_agent.plugins.contributions import ToolContribution, ToolResult, ToolSpec
    from bianbu_agent.runs.control import RunControl

    hook = load(tmp_path, '''import json,sys
p=json.load(sys.stdin)
assert p['run_id']=='run'
assert p['account_id']=='account'
assert p['tool_input']=={'command':'pwd'}
assert p['prompt']=='把音量调到1'
assert p['request']=='terminal'
assert p['metadata']['hook_ids']==['example.commands/example.check']
assert p['metadata']['custom']=={'trace':'public'}
assert 'run_control' not in p['metadata']
assert 'checkpoint_map' not in p['metadata']
assert not any(key.startswith('__pregel_') for key in p['metadata'])
print('{}')
''', event=event)
    seen = []

    class Hooks:
        async def run_hooks(self, point, context, *, hook_ids):
            if point is hook.point:
                seen.append(point)
                return await hook.handler(context)

    async def handler(_context, arguments):
        assert arguments == {'command': 'pwd'}
        return ToolResult('executed')

    control = RunControl('run', 'session')
    configurable = {'run_id': 'run', 'session_id': 'session', 'agent_id': 'expert',
                    'account_id': 'account', 'run_control': control,
                    'hook_ids': ('example.commands/example.check',),
                    '__pregel_send': lambda *_: None, '__pregel_runtime': object(),
                    'checkpoint_map': {'private': 'checkpoint'},
                    'user_prompt': '把音量调到1', 'custom': {'trace': 'public'}}
    tool = build_plugin_tools([ToolContribution('example.commands', ToolSpec(
        'terminal', 'test', {'type': 'object', 'properties': {'command': {'type': 'string'}}}),
        handler)], hook_runner=Hooks())[0]
    assert await tool.ainvoke({'command': 'pwd'}, config={'configurable': configurable}) == 'executed'
    assert seen == [hook.point]
    assert configurable['run_control'] is control
    assert not control.cancelled.is_set()


def load_named(plugin_id, tmp_path, directory, root_name, script, context,
               *, event='PreToolUse'):
    plugin = tmp_path / directory
    root = plugin / 'hooks' / root_name
    root.mkdir(parents=True)
    (plugin / 'scripts').mkdir(exist_ok=True)
    (plugin / 'scripts' / 'check.py').write_text(script)
    handler = {'type': 'command',
               'command': 'python3 "${PLUGIN_ROOT}/scripts/check.py"', 'timeout': 5}
    (root / 'hook.json').write_text(json.dumps({'schema_version': 1, 'hooks': {
        event: [{'matcher': 'terminal', 'hooks': [handler]}]}}))
    return DirectoryHookLoader().load(root, origin=CapabilityOrigin.PLUGIN,
        plugin_id=plugin_id, context=context)


@pytest.mark.asyncio
async def test_command_state_is_private_to_one_plugin(tmp_path):
    context = HookLoadContext(tmp_path, tmp_path / 'data')
    script = ('import json,sys\np=json.load(sys.stdin)\n'
              'print(json.dumps({"state":{"first":1},'
              '"metadata":{"seen":p["state"] or None}}))')

    first = load_named('first.plugin', tmp_path, 'first', 'first.check', script, context)
    second = load_named('second.plugin', tmp_path, 'second', 'second.check',
        'import json,sys\np=json.load(sys.stdin)\nassert not p["state"], p["state"]\n'
        'print(json.dumps({"state":{"second":2}}))', context)

    run = RunContext('run', 'session', 'agent', 'terminal',
                     metadata={'tool': 'terminal'})
    opening = await first.handler(run)
    # A sibling Plugin in the same Session must not see this state ...
    await second.handler(run)
    # ... while the owning Plugin keeps it across Events.
    again = await first.handler(run)

    assert opening.metadata == {'seen': None}
    assert again.metadata == {'seen': {'first': 1}}


def test_command_hook_rejects_non_boolean_parallel(tmp_path):
    plugin = tmp_path / 'example.commands'
    root = plugin / 'hooks' / 'example.check'
    root.mkdir(parents=True)
    (plugin / 'scripts').mkdir()
    (plugin / 'scripts' / 'check.py').write_text('print("{}")')
    (root / 'hook.json').write_text(json.dumps({'schema_version': 1, 'parallel': 'yes',
        'hooks': {'PreToolUse': [{'hooks': [
            {'type': 'command', 'command': 'python3 x.py'}]}]}}))
    with pytest.raises(ValueError):
        DirectoryHookLoader().load(root, origin=CapabilityOrigin.PLUGIN,
            plugin_id='example.commands',
            context=HookLoadContext(tmp_path, tmp_path / 'data'))

