import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from bianbu_agent.hooks.loader import DirectoryHookLoader
from bianbu_agent.hooks.models import HookLoadContext
from bianbu_agent.inventory.models import CapabilityOrigin
from bianbu_agent.plugins.contributions import RunContext

RESIDENT_SCRIPT = '''import json, os, sys
count = 0
for line in sys.stdin:
    json.loads(line)
    count += 1
    sys.stdout.write(json.dumps({
        "metadata": {"pid": os.getpid(), "count": count},
        "context_sections": [str(count)],
    }) + "\\n")
    sys.stdout.flush()
'''

PREWARM_SCRIPT = '''import json, os, sys
from pathlib import Path
with Path(os.environ["PLUGIN_DATA_ROOT"], "started").open("a") as record:
    record.write(f"{os.getpid()}\\n")
for line in sys.stdin:
    json.loads(line)
    sys.stdout.write(json.dumps({"metadata": {"pid": os.getpid()}}) + "\\n")
    sys.stdout.flush()
'''


def load(tmp_path, script, *, mode='resident', timeout=5, context=None):
    plugin = tmp_path / 'example.commands'
    root = plugin / 'hooks' / 'example.check'
    root.mkdir(parents=True, exist_ok=True)
    (plugin / 'scripts').mkdir(exist_ok=True)
    (plugin / 'scripts' / 'check.py').write_text(script)
    handler = {'type': 'command', 'mode': mode, 'timeout': timeout,
               'command': 'exec "${RUNTIME_PYTHON}" "${PLUGIN_ROOT}/scripts/check.py"'}
    (root / 'hook.json').write_text(json.dumps({'schema_version': 1, 'hooks': {
        'BeforeRun': [{'hooks': [handler]}]}}))
    context = context or HookLoadContext(
        workspace_root=tmp_path, data_root=tmp_path / 'data')
    hook = DirectoryHookLoader().load(
        root, origin=CapabilityOrigin.PLUGIN,
        plugin_id='example.commands', context=context)
    return hook, context


def run(*, session='s'):
    return RunContext('r', session, 'a', 'prompt')


@pytest.mark.asyncio
async def test_resident_hook_reuses_one_process_and_module_state(tmp_path):
    hook, context = load(tmp_path, RESIDENT_SCRIPT)
    try:
        first = await hook.handler(run())
        second = await hook.handler(run())
    finally:
        await context.command_processes.close()
    assert first.metadata['pid'] == second.metadata['pid']
    assert (first.metadata['count'], second.metadata['count']) == (1, 2)


@pytest.mark.asyncio
async def test_prewarm_starts_the_resident_process_before_the_first_call(tmp_path):
    hook, context = load(tmp_path, PREWARM_SCRIPT)
    started = tmp_path / "data" / "started"
    try:
        assert hook.prewarm is not None
        await hook.prewarm()
        for _ in range(100):
            if started.is_file():
                break
            await asyncio.sleep(0.02)
        assert started.is_file()
        first = await hook.handler(run())
        second = await hook.handler(run())
    finally:
        await context.command_processes.close()
    pids = [int(value) for value in started.read_text().split()]
    assert pids == [first.metadata['pid']]
    assert first.metadata['pid'] == second.metadata['pid']


@pytest.mark.asyncio
async def test_prewarm_reuses_an_already_running_process(tmp_path):
    hook, context = load(tmp_path, PREWARM_SCRIPT)
    started = tmp_path / "data" / "started"
    try:
        assert hook.prewarm is not None
        await hook.prewarm()
        await hook.prewarm()
        for _ in range(100):
            if started.is_file():
                break
            await asyncio.sleep(0.02)
        first = await hook.handler(run())
    finally:
        await context.command_processes.close()
    assert started.read_text().split() == [str(first.metadata['pid'])]


@pytest.mark.asyncio
async def test_oneshot_hook_still_starts_a_process_per_call(tmp_path):
    hook, context = load(tmp_path, RESIDENT_SCRIPT, mode='oneshot')
    try:
        first = await hook.handler(run())
        second = await hook.handler(run())
    finally:
        await context.command_processes.close()
    assert first.metadata['pid'] != second.metadata['pid']
    assert (first.metadata['count'], second.metadata['count']) == (1, 1)


@pytest.mark.asyncio
async def test_resident_hook_restarts_after_the_process_exits(tmp_path):
    script = '''import json, os, sys
first = True
for line in sys.stdin:
    json.loads(line)
    sys.stdout.write(json.dumps({"metadata": {"pid": os.getpid()}}) + "\\n")
    sys.stdout.flush()
    if first:
        first = False
        raise SystemExit(3)
'''
    hook, context = load(tmp_path, script)
    try:
        first = await hook.handler(run())
        with pytest.raises(PermissionError, match='Command Hook failed'):
            await hook.handler(run())
        third = await hook.handler(run())
    finally:
        await context.command_processes.close()
    assert first.metadata['pid'] != third.metadata['pid']


@pytest.mark.asyncio
async def test_resident_hook_timeout_kills_the_process(tmp_path):
    script = '''import json, sys, time
for line in sys.stdin:
    json.loads(line)
    time.sleep(30)
'''
    hook, context = load(tmp_path, script, timeout=1)
    try:
        with pytest.raises(PermissionError, match='timed out'):
            await asyncio.wait_for(hook.handler(run()), timeout=5)
    finally:
        await context.command_processes.close()


@pytest.mark.asyncio
async def test_closing_the_pool_stops_resident_processes(tmp_path):
    hook, context = load(tmp_path, RESIDENT_SCRIPT)
    patch = await hook.handler(run())
    pid = int(patch.metadata['pid'])
    assert Path(f'/proc/{pid}').exists()
    await context.command_processes.close()
    assert not Path(f'/proc/{pid}').exists()


@pytest.mark.parametrize('mode', ['resident', 'oneshot'])
def test_hook_manifest_accepts_known_modes(tmp_path, mode):
    load(tmp_path, RESIDENT_SCRIPT, mode=mode)
    from bianbu_agent.hooks.command import inspect_command_hook
    event, commands = inspect_command_hook(
        Path(tmp_path) / 'example.commands/hooks/example.check/hook.json')
    assert event == 'BeforeRun'
    assert commands[0][4] == mode


def test_hook_manifest_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError, match='mode'):
        load(tmp_path, RESIDENT_SCRIPT, mode='daemon')


def test_shipped_command_hooks_run_resident():
    """Every builtin command Hook declares the transport it ships with."""

    root = Path(__file__).resolve().parents[2] / 'resources/plugins'
    manifests = sorted(root.glob('*/hooks/*/hook.json'))
    assert manifests
    for path in manifests:
        data = json.loads(path.read_text())
        for groups in data['hooks'].values():
            for group in groups:
                for handler in group['hooks']:
                    assert handler.get('mode') == 'resident', path


@pytest.mark.asyncio
async def test_cancelling_a_call_still_stops_the_process(tmp_path):
    """A cancelled await must not leave the Hook running.

    The Hook is cancelled while it sleeps, which is exactly what happens when a
    Run is cancelled or a Worker shuts down mid-call.
    """

    script = '''import json, os, sys, time
for line in sys.stdin:
    json.loads(line)
    sys.stdout.write(json.dumps({"metadata": {"pid": os.getpid()}}) + "\\n")
    sys.stdout.flush()
    time.sleep(30)
'''
    hook, context = load(tmp_path, script, timeout=60)
    try:
        task = asyncio.create_task(hook.handler(run()))
        patch = None
        while patch is None:
            await asyncio.sleep(0.05)
            try:
                patch = await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
        pid = int(patch.metadata['pid'])
        task2 = asyncio.create_task(hook.handler(run(session='other')))
        await asyncio.sleep(0.5)
        task2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task2
        for _ in range(60):
            if not Path(f'/proc/{pid}').exists():
                break
            await asyncio.sleep(0.05)
        assert not Path(f'/proc/{pid}').exists()
    finally:
        await context.command_processes.close()


@pytest.mark.asyncio
async def test_idle_processes_are_reaped_without_a_new_call(tmp_path):
    from bianbu_agent.hooks.resident import CommandProcessPool

    script = '''import json, os, sys
for line in sys.stdin:
    json.loads(line)
    sys.stdout.write(json.dumps({"metadata": {"pid": os.getpid()}}) + "\\n")
    sys.stdout.flush()
'''
    context = HookLoadContext(
        workspace_root=tmp_path, data_root=tmp_path / 'data',
        command_processes=CommandProcessPool(idle_seconds=0.05))
    hook, context = load(tmp_path, script, context=context)
    pool_patch = await hook.handler(run())
    pid = int(pool_patch.metadata['pid'])
    assert Path(f'/proc/{pid}').exists()
    try:
        for _ in range(120):
            if not Path(f'/proc/{pid}').exists():
                break
            await asyncio.sleep(0.05)
        assert not Path(f'/proc/{pid}').exists()
    finally:
        await context.command_processes.close()


@pytest.mark.asyncio
async def test_resident_hook_skips_repeated_import_cost(tmp_path):
    """The second call must not pay interpreter start-up again.

    This is the property the whole feature exists for, so it is asserted
    directly: a script that imports a slow module reports the import count.
    """

    script = '''import json, sys, time
time.sleep(0.3)
imports = 1
for line in sys.stdin:
    json.loads(line)
    sys.stdout.write(json.dumps({"metadata": {"imports": imports}}) + "\\n")
    sys.stdout.flush()
'''
    hook, context = load(tmp_path, script, timeout=10)
    try:
        first = await hook.handler(run())
        started = time.monotonic()
        second = await hook.handler(run())
        elapsed = time.monotonic() - started
    finally:
        await context.command_processes.close()
    assert first.metadata['imports'] == 1
    assert second.metadata['imports'] == 1
    assert elapsed < 0.25


@pytest.mark.asyncio
async def test_dead_idle_process_is_replaced_without_a_failure(tmp_path):
    """A Hook killed while idle must not fail the next Run."""

    script = '''import json, os, sys
for line in sys.stdin:
    json.loads(line)
    sys.stdout.write(json.dumps({"metadata": {"pid": os.getpid()}}) + "\\n")
    sys.stdout.flush()
'''
    hook, context = load(tmp_path, script)
    try:
        first = await hook.handler(run())
        pid = int(first.metadata['pid'])
        os.kill(pid, 9)
        for _ in range(60):
            if not Path(f'/proc/{pid}').exists():
                break
            await asyncio.sleep(0.05)
        second = await hook.handler(run())
    finally:
        await context.command_processes.close()
    assert second.metadata['pid'] != pid
