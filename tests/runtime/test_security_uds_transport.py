"""bianbu.security 插件的安全身份传递与未映射策略默认值。"""
from __future__ import annotations


def _load_uds_transport():
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parents[2]
            / "resources/plugins/bianbu.security/lib/uds_transport.py")
    spec = importlib.util.spec_from_file_location("_cc_uds_transport", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    """未映射工具默认 allow；生产模式下环境变量不能放宽（allow 即默认值）。"""
    monkeypatch.setenv("BIANBU_SECURITY_UNMAPPED", "allow")
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    mapping = _load_mapping(monkeypatch)
    assert mapping.unmapped_policy() == "allow"
    assert mapping.unmapped_decision() == 0
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


def test_unmapped_policy_defaults_to_allow(monkeypatch):
    monkeypatch.delenv("BIANBU_SECURITY_UNMAPPED", raising=False)
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    mapping = _load_mapping(monkeypatch)
    assert mapping.unmapped_policy() == "allow"
    assert mapping.unmapped_decision() == 0


def test_unmapped_policy_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY_UNMAPPED", "maybe")
    mapping = _load_mapping(monkeypatch)
    assert mapping.unmapped_policy() == "allow"
