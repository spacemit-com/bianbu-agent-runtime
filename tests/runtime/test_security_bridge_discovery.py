from __future__ import annotations

import importlib
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).parents[2]
PLUGIN_ROOT = PROJECT_ROOT / "resources/plugins/bianbu.security"


def _bridge_module():
    # Other plugin tests use the generic top-level name ``lib``. Give discovery
    # its own package identity so test order cannot select another plugin.
    from types import ModuleType
    package = '_security_bridge_discovery_lib'
    if package not in sys.modules:
        module = ModuleType(package)
        module.__path__ = [str(PLUGIN_ROOT / 'lib')]
        sys.modules[package] = module
    return importlib.import_module(package + '.bridge')


def test_security_bridge_discovers_installed_securityd_after_invalid_override(
    monkeypatch, tmp_path: Path,
) -> None:
    bridge = _bridge_module()
    installed = tmp_path / "usr/lib/bianbu-securityd"
    (installed / "securityd").mkdir(parents=True)
    (installed / "securityd/interceptor.py").write_text("", encoding="utf-8")

    monkeypatch.setenv("BIANBU_SECURITYD_HOME", str(tmp_path / "runtime-source"))
    monkeypatch.setattr(bridge, "_DEFAULT_HOMES", (str(installed),))

    assert bridge._discover_home() == installed


def test_security_bridge_declares_installed_securityd_location() -> None:
    bridge = _bridge_module()

    assert "/usr/lib/bianbu-securityd" in bridge._DEFAULT_HOMES


def test_security_bridge_uses_helper_when_native_client_unavailable(monkeypatch) -> None:
    bridge = _bridge_module()
    instance = bridge.SecurityBridge.__new__(bridge.SecurityBridge)
    instance._sc = None

    real_import = __import__

    def blocked(name, *args, **kwargs):
        if name == "bianbu_security_client":
            raise ModuleNotFoundError("No module named 'dbus'")
        return real_import(name, *args, **kwargs)

    calls = []
    monkeypatch.setattr("builtins.__import__", blocked)
    monkeypatch.setattr(
        "bianbu_agent.security.security_runtime.security_client_call",
        lambda method, *args, **kwargs: calls.append((method, args, kwargs)) or 1,
    )

    client = instance.client()
    assert client.check_permission("agent", "system.execute", "") == 1
    assert calls == [("check_permission", ("agent", "system.execute", ""), {})]


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['input', 'pre_model_call', 'post_model_call',
                                   'pre_tool_call', 'post_tool_call', 'output'])
async def test_yolo_bridge_records_without_invoking_policy_handlers(monkeypatch, stage):
    from types import SimpleNamespace
    bridge = _bridge_module()
    mode = 'yolo'
    records = []
    def policy(*args, **kwargs):
        pytest.fail('YOLO must not invoke local policy/DLP handlers')
    instance = bridge.SecurityBridge.__new__(bridge.SecurityBridge)
    instance.available = True
    instance._registry = SimpleNamespace(invoke=policy)
    instance._dlp_ctx = {}
    instance._uds_transport = None
    instance._sc = SimpleNamespace(
        get_agent_security_mode=lambda aid: {'mode': mode},
        audit_log=lambda *args: records.append(args),
    )
    # The response type belongs to the separately packaged Security library.
    monkeypatch.setitem(sys.modules, 'securityd.interceptor', SimpleNamespace(
        InterceptResult=lambda **kwargs: SimpleNamespace(
            decision=SimpleNamespace(value='ALLOW'), modified_data=None, **kwargs)))
    result = await instance.invoke(stage, 'expert', {'text': '13824681357'})
    assert result.decision.value == 'ALLOW'
    assert result.modified_data is None
    assert result.metadata['mode'] == 'yolo'
    assert records and records[0][0] == 'expert'
    assert 'mode=yolo' in records[0][-1]
