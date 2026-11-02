"""DLP DENY terminates the Run at every Hook event.

Every event point may veto: a denial never degrades into a metadata flag that
some other Hook has to notice.  YOLO skips the scanner in SecurityBridge
before any policy handler runs; that contract is covered in
test_security_bridge_discovery.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).parents[1] / 'resources/plugins/bianbu.security'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['safe'])
@pytest.mark.parametrize('stage', ['before_run', 'before_model', 'after_context', 'after_model', 'after_tool', 'before_tool'])
async def test_dlp_block_is_not_a_capability_override(monkeypatch, mode, stage):
    saved_modules = {k: v for k, v in sys.modules.items() if k == "lib" or k.startswith("lib.")}
    for key in saved_modules:
        del sys.modules[key]
    sys.path.insert(0, str(ROOT))
    try:
        import lib
        result = SimpleNamespace(decision=SimpleNamespace(value='BLOCK'),
                                 metadata={'dlp_action': 'deny', 'max_risk': 'L2'},
                                 message='synthetic phone blocked', modified_data=None)
        class Bridge:
            available = True
            client = lambda self: None
            hook_point = staticmethod(lambda name: name)
            audit = staticmethod(lambda *args: None)
            degrade = staticmethod(lambda *args: None)
            async def invoke(self, *args, **kwargs):
                return result
        monkeypatch.setattr(lib, 'get_bridge', lambda: Bridge())
        monkeypatch.setattr(lib, 'resolve_agent_id', lambda aid, **kwargs: aid)
        monkeypatch.setattr(lib, 'agent_security_mode_is_yolo', lambda *a, **k: mode == 'yolo')
        monkeypatch.setattr(lib, 'resolve_capability', lambda *a, **k: ('file.read', ''))
        monkeypatch.setattr(lib, 'egress_info', lambda *a: None)
        spec = importlib.util.spec_from_file_location('dlp_test_' + stage, ROOT / 'scripts' / (stage + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        run = dict(run_id='r', session_id='s', agent_id='a', request='13824681357',
                   metadata={'tool': 'read_file', 'arguments': {'path': '13824681357'},
                             'response': '13824681357', 'result': '13824681357'})
        with pytest.raises(PermissionError, match='synthetic phone blocked'):
            await getattr(module, stage)(run)
    finally:
        sys.path.remove(str(ROOT))
        for key in list(sys.modules):
            if key == "lib" or key.startswith("lib."):
                del sys.modules[key]
        sys.modules.update(saved_modules)
