"""bianbu.security 插件的安全身份传递与未映射策略默认值。"""
from __future__ import annotations

import sys
import types
from enum import Enum


def _load_uds_transport():
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parents[2]
            / "resources/plugins/bianbu.security/lib/uds_transport.py")
    spec = importlib.util.spec_from_file_location("_cc_uds_transport", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_fake_securityd(monkeypatch):
    """Expose just enough of securityd's DLP modules to load the plugin."""
    action_cls = Enum("DLPAction", {
        "ALLOW": "allow", "ASK": "ask", "DENY": "deny", "MASK": "mask",
    }, type=str)
    pii_cls = Enum("PIIType", {"NAME": "name"}, type=str)

    class ScanResult:
        def __init__(self, findings=None, masked_text="", mask_map=None):
            self.findings = list(findings or [])
            self.masked_text = masked_text
            self.mask_map = dict(mask_map or {})

    securityd = types.ModuleType("securityd")
    securityd.__path__ = []
    dlp = types.ModuleType("securityd.dlp")
    dlp.__path__ = []
    policy_store = types.ModuleType("securityd.dlp.policy_store")
    policy_store.DLPAction = action_cls
    pii_scanner = types.ModuleType("securityd.dlp.pii_scanner")
    pii_scanner.PIIType = pii_cls
    pii_scanner.ScanResult = ScanResult
    securityd.dlp = dlp
    dlp.policy_store = policy_store
    dlp.pii_scanner = pii_scanner
    for name, module in {
        "securityd": securityd,
        "securityd.dlp": dlp,
        "securityd.dlp.policy_store": policy_store,
        "securityd.dlp.pii_scanner": pii_scanner,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return action_cls, pii_cls


def test_hybrid_policy_store_uses_deny_action(monkeypatch):
    """DLP gaps must resolve to DENY without touching the removed BLOCK name."""
    actions, pii = _install_fake_securityd(monkeypatch)

    class UnavailableTransport:
        def call(self, request):
            raise RuntimeError("securityd down")

    mod = _load_uds_transport()
    store = mod.HybridDLPPolicyStore(object(), UnavailableTransport())
    assert store.get_action("agent", pii.NAME) is actions.DENY


def test_mask_subset_keeps_deny_types(monkeypatch):
    actions, pii = _install_fake_securityd(monkeypatch)

    mod = _load_uds_transport()
    scanner = mod.HybridPIIScanner.__new__(mod.HybridPIIScanner)
    finding = types.SimpleNamespace(pii_type=pii.NAME, start=0, end=4)
    scanner.scan_for_agent = lambda agent_id, text: types.SimpleNamespace(
        findings=[finding])
    kept: list = []

    def mask(text, findings):
        kept.extend(findings)
        return types.SimpleNamespace(masked_text="****", mask_map={})

    scanner._mask_findings = mask

    class Store:
        def get_action(self, agent_id, pii_type):
            return actions.DENY

    result = scanner.scan_mask_subset_for_agent("agent", "name", Store())
    assert kept == [finding]
    assert result.findings == [finding]


def test_managed_worker_uds_requests_carry_security_agent_id(monkeypatch):
    """受管 Worker 的 UDS 请求必须带 Runtime 安全身份，否则 securityd lease 校验必失败。"""
    monkeypatch.setenv("BIANBU_WORKER_MANAGED", "1")
    monkeypatch.setenv("BIANBU_WORKER_INSTANCE_ID", "agent-inst")
    monkeypatch.setenv("BIANBU_WORKER_LEASE_ID", "lease-1")
    monkeypatch.setenv("BIANBU_WORKER_SECURITY_AGENT_ID", "com.bianbu.runtime")
    mod = _load_uds_transport()
    seen: list[dict] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def call(self, request):
            seen.append(dict(request))
            return {"ok": True}

    transport = mod.SecurityUdsTransport(client_factory=FakeClient)
    transport.call({"op": "dlp.scan", "agent_id": "com.bianbu.agent",
                    "_worker_security_agent_id": "forged"})
    assert seen[0]["_worker_instance_id"] == "agent-inst"
    assert seen[0]["_worker_lease_id"] == "lease-1"
    # 调用方字段不能覆盖 supervisor 注入的身份
    assert seen[0]["_worker_security_agent_id"] == "com.bianbu.runtime"
    assert seen[0]["agent_id"] == "com.bianbu.agent"


def test_unmanaged_worker_uds_requests_carry_no_lease_identity(monkeypatch):
    monkeypatch.delenv("BIANBU_WORKER_MANAGED", raising=False)
    mod = _load_uds_transport()
    seen: list[dict] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def call(self, request):
            seen.append(dict(request))
            return {"ok": True}

    mod.SecurityUdsTransport(client_factory=FakeClient).call({"op": "ping"})
    assert "_worker_security_agent_id" not in seen[0]


def _load_mapping(monkeypatch):
    import importlib.util
    from pathlib import Path

    # 强制走插件内的本地回退实现（不依赖 securityd 是否可导入）
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("securityd"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    path = (Path(__file__).resolve().parents[2]
            / "resources/plugins/bianbu.security/lib/mapping.py")
    spec = importlib.util.spec_from_file_location("_cc_mapping", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(builtins, "__import__", real_import)
    return module


def test_unmapped_allow_override_is_ignored_in_production(monkeypatch):
    """未映射工具默认 ask；生产模式下环境变量不能把它放宽成 allow。"""
    monkeypatch.setenv("BIANBU_SECURITY_UNMAPPED", "allow")
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    mapping = _load_mapping(monkeypatch)
    assert mapping.unmapped_policy() == "ask"
    assert mapping.unmapped_decision() == 1
    assert not hasattr(mapping, "DEFAULT_ALLOW")
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "development")
    assert mapping.unmapped_policy() == "allow"
    assert mapping.unmapped_decision() == 0


def test_unmapped_override_tightens_policy_in_development(monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY_UNMAPPED", "deny")
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "development")
    mapping = _load_mapping(monkeypatch)
    assert mapping.unmapped_policy() == "deny"
    assert mapping.unmapped_decision() == 2


def test_unmapped_policy_defaults_to_ask(monkeypatch):
    monkeypatch.delenv("BIANBU_SECURITY_UNMAPPED", raising=False)
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    mapping = _load_mapping(monkeypatch)
    assert mapping.unmapped_policy() == "ask"
    assert mapping.unmapped_decision() == 1


def test_unmapped_policy_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY_UNMAPPED", "maybe")
    mapping = _load_mapping(monkeypatch)
    assert mapping.unmapped_policy() == "ask"
