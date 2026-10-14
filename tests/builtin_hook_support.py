"""In-process adapter for unit-testing standalone scripts with fake service clients.

Production loads hook.json and executes a process; process integration tests live
in test_builtin_command_scripts.py. This adapter exists only to isolate business
logic in tests that monkeypatch the memory or HTTP SDK.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys

from bianbu_agent.plugins import ContextPatch, HookContribution, HookPoint


def adapt(handler, *, plugin_id='bianbu.security', hook_id='security.before_tool', context=None, priority=100):
    async def invoke(run):
        payload = {name: getattr(run, name) for name in ('run_id', 'session_id', 'agent_id', 'account_id', 'request', 'response', 'status', 'metadata')}
        previous = os.environ.get('PLUGIN_DATA_ROOT')
        try:
            if context is not None:
                os.environ['PLUGIN_DATA_ROOT'] = str(context.data_root)
            result = await handler(payload)
        finally:
            if previous is None:
                os.environ.pop('PLUGIN_DATA_ROOT', None)
            else:
                os.environ['PLUGIN_DATA_ROOT'] = previous
        return ContextPatch(**result) if result is not None else None
    return HookContribution(plugin_id=plugin_id, id=hook_id, point=HookPoint(hook_id.split('.')[-1]), handler=invoke, priority=priority)


class ScriptUnitLoader:
    def load(self, root, *, context, plugin_id=None, **kwargs):
        root = Path(root)
        plugin_root = root.parents[1]
        script = plugin_root / 'scripts' / (root.name.split('.')[-1] + '.py')
        # Copied test fixtures can keep only the Hook configuration.
        if not script.exists():
            script = Path(__file__).resolve().parents[1] / 'resources/plugins' / context.owner_plugin_id / 'scripts' / script.name
        name = 'builtin_script_test_' + context.owner_plugin_id.replace('.', '_') + '_' + script.stem
        spec = importlib.util.spec_from_file_location(name, script)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return adapt(getattr(module, script.stem), plugin_id=plugin_id or context.owner_plugin_id, hook_id=root.name, context=context, priority=json.loads((root / 'hook.json').read_text()).get('priority', 100))
