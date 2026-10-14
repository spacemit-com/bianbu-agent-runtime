from __future__ import annotations

import asyncio
import json
import sys
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import bianbu_agent.security.security_runtime as security_runtime

from bianbu_agent.security.security_runtime import (
    RuntimeStartRequest,
    SecurityRuntimeUnavailable,
    SecurityRuntimeClient,
)


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("async_client", [False, True])
def test_lazy_client_dependency_failure_bridges_only_control(monkeypatch, managed, async_client):
    def missing_dbus(*args):
        raise ModuleNotFoundError("No module named 'dbus'", name="dbus")
    monkeypatch.setenv("BIANBU_WORKER_MANAGED", "1" if managed else "0")
    monkeypatch.setattr(security_runtime, "_client_module", lambda: SimpleNamespace(
        get_agent_info=missing_dbus))
    calls = []
    def bridge(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"last_seen": 7}')
    monkeypatch.setattr(security_runtime.subprocess, "run", bridge)
    def call():
        if async_client:
            return asyncio.run(SecurityRuntimeClient()._call("get_agent_info", "agent"))
        return security_runtime.security_client_call("get_agent_info", "agent")
    if managed:
        with pytest.raises((ModuleNotFoundError, SecurityRuntimeUnavailable), match="dbus"):
            call()
        assert not calls
    else:
        assert call() == {"last_seen": 7}
        assert len(calls) == 1


def test_configured_client_is_loaded_before_helper_fallback(tmp_path):
    # The deployed client is outside the Runtime venv. A failed initial import
    # must not prevent a native client from loading through its configured path.
    (tmp_path / "bianbu_security_client.py").write_text(
        "def get_agent_info(agent_id):\n"
        "    return {'last_seen': 7}\n"
    )
    result = _run_client_probe({
        "BIANBU_SECURITY_CLIENT_DIR": str(tmp_path),
        "BIANBU_SECURITYD_HOME": "",
        "BIANBU_RUNTIME_MODE": "production",
    })
    assert result.returncode == 0, result.stderr


def _run_client_probe(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """导入安全客户端并调用一个控制面方法，验证依赖解析与回退路径。"""
    root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-c",
         "from bianbu_agent.security.security_runtime import security_client_module; "
         "print(security_client_module().get_agent_info('com.bianbu.test')['last_seen'])"],
        cwd=root, capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": str(root), **env},
    )


def test_client_binary_dependencies_resolve_through_system_site_dirs(tmp_path):
    # The client lives on PYTHONPATH but its dbus-python dependency is shipped by
    # Debian in dist-packages, outside the Runtime venv. Only the missing
    # dependency path may be appended, and only after the client was found.
    client_dir = tmp_path / "client"
    client_dir.mkdir()
    (client_dir / "bianbu_security_client.py").write_text(
        "import bianbu_fake_dbus\n\n\n"
        "def get_agent_info(agent_id):\n"
        "    return {'last_seen': bianbu_fake_dbus.CONV_USED}\n"
    )
    site_dir = tmp_path / "dist-packages"
    site_dir.mkdir()
    (site_dir / "bianbu_fake_dbus.py").write_text("CONV_USED = 7\n")

    missing = _run_client_probe({
        "BIANBU_SECURITY_CLIENT_DIR": str(client_dir),
        "BIANBU_SECURITY_CLIENT_SITE_DIRS": "",
        "BIANBU_SECURITYD_HOME": "",
        "BIANBU_RUNTIME_MODE": "production",
    })
    assert missing.returncode != 0
    assert "bianbu_fake_dbus" in missing.stderr

    resolved = _run_client_probe({
        "BIANBU_SECURITY_CLIENT_DIR": str(client_dir),
        "BIANBU_SECURITY_CLIENT_SITE_DIRS": str(site_dir),
        "BIANBU_SECURITYD_HOME": "",
        "BIANBU_RUNTIME_MODE": "production",
    })
    assert resolved.returncode == 0, resolved.stderr
    assert resolved.stdout.strip() == "7"


@pytest.mark.asyncio
async def test_prepare_start_uses_digest_free_security_contract(monkeypatch):
    policy = {"effective_mode": "sandbox"}
    result = {
        "lease_id": "lease-1", "policy_json": json.dumps(policy),
        "effective_mode": "sandbox", "backend": "bwrap",
        "sandbox_required": True, "expires_at": 123, "reason": "test",
    }
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "bianbu_security_client",
        SimpleNamespace(
            prepare_runtime_start=lambda *args, **kwargs: calls.append((args, kwargs)) or result
        ),
    )
    client = SecurityRuntimeClient(required=True)
    decision = await client.prepare_start(RuntimeStartRequest("agent", "sandbox"))
    assert decision is not None
    assert decision.effective_mode == "sandbox"
    assert decision.policy == policy
    assert calls == [(("agent", "sandbox"), {})]


@pytest.mark.asyncio
async def test_prepare_start_forwards_logical_worker_agent_id(monkeypatch):
    result = {
        "lease_id": "lease-1", "policy_json": "{}",
        "effective_mode": "sandbox", "backend": "bwrap",
        "sandbox_required": True, "expires_at": 123,
    }
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "bianbu_security_client",
        SimpleNamespace(
            prepare_runtime_start=lambda *args, **kwargs: calls.append(
                (args, kwargs)
            ) or result,
        ),
    )

    decision = await SecurityRuntimeClient(required=True).prepare_start(
        RuntimeStartRequest(
            "com.bianbu.runtime", "sandbox",
            worker_agent_id="agent-0fefac549b59",
        )
    )

    assert decision is not None
    assert calls == [(('com.bianbu.runtime', 'sandbox'), {
        'worker_agent_id': 'agent-0fefac549b59',
    })]


@pytest.mark.asyncio
async def test_prepare_start_accepts_policy_without_digest(monkeypatch):
    policy = {"effective_mode": "host"}
    result = {
        "lease_id": "lease-1", "policy_json": json.dumps(policy),
        "effective_mode": "host",
        "backend": "host-process", "sandbox_required": False, "expires_at": 123,
    }
    monkeypatch.setitem(sys.modules, "bianbu_security_client",
                        SimpleNamespace(prepare_runtime_start=lambda *args: result))
    decision = await SecurityRuntimeClient(required=True).prepare_start(
        RuntimeStartRequest("agent"))
    assert decision is not None
    assert decision.policy == policy


@pytest.mark.asyncio
async def test_required_client_fails_closed_when_client_unavailable(monkeypatch):
    monkeypatch.delitem(sys.modules, "bianbu_security_client", raising=False)
    monkeypatch.delenv("BIANBU_SECURITYD_HOME", raising=False)
    with pytest.raises(Exception):
        await SecurityRuntimeClient(required=True).prepare_start(
            RuntimeStartRequest("agent"))


@pytest.mark.asyncio
async def test_syscall_v2_denial_does_not_fallback_to_legacy(monkeypatch):
    calls = []

    def v2(*args):
        calls.append(("v2", args))
        raise SecurityRuntimeUnavailable("Worker syscall event 未通过 Runtime lease 身份校验")

    def legacy(*args):
        calls.append(("legacy", args))
        return {"ok": True}

    monkeypatch.setitem(sys.modules, "bianbu_security_client",
                        SimpleNamespace(record_syscall_interception_v2=v2,
                                       record_syscall_interception=legacy))
    monkeypatch.setattr(security_runtime, "_client_module",
                        lambda: sys.modules["bianbu_security_client"])
    client = SecurityRuntimeClient(required=True)
    with pytest.raises(Exception):
        await client.record_syscall_interception(
            "agent", 42, "riscv64", "finit_module", 273, "strace-seccomp", 1,
            instance_id="instance", lease_id="lease", event_id="event")
    assert [kind for kind, _ in calls] == ["v2"]


@pytest.mark.asyncio
async def test_network_learning_client_forwards_lease_bound_event_and_status(monkeypatch):
    calls = []
    module = SimpleNamespace(
        record_network_learning_event=lambda *args, **kwargs: calls.append(
            ("event", args, kwargs)) or True,
        report_network_learning_collector=lambda *args, **kwargs: calls.append(
            ("status", args, kwargs)) or True,
    )
    monkeypatch.setattr(security_runtime, "_client_module", lambda: module)
    client = SecurityRuntimeClient(required=True)
    event = {
        "event_id": "evt-1", "observed_at": 1000.0,
        "event_type": "connect", "protocol": "tcp",
        "remote_ip": "203.0.113.8", "remote_port": 443,
        "outcome": "success", "backend": "strace", "policy_version": 4,
    }

    assert await client.record_network_learning_event(
        "agent", "worker-1", "lease-1", 42, event) is True
    assert await client.report_network_learning_collector(
        "agent", "worker-1", "lease-1", 42, "degraded", "observer exited") is True
    assert calls == [
        ("event", ("agent", "worker-1", "lease-1", 42, event), {}),
        ("status", ("agent", "worker-1", "lease-1", 42,
                    "degraded", "observer exited"), {}),
    ]


@pytest.mark.asyncio
async def test_stop_instance_failure_records_registered_metric():
    """停止失败必须记进已注册指标，且返回 None 而不是抛出。"""
    from bianbu_agent.server.metrics import RuntimeMetrics

    metrics = RuntimeMetrics()
    client = SecurityRuntimeClient(required=False, metrics=metrics)

    async def unavailable(*args, **kwargs):
        raise RuntimeError("security.runtime.StopInstance 不可用")

    client._call = unavailable
    assert await client.stop_instance("lease-1", "instance-1", "stop") is None
    assert 'runtime_security_stop_total{result="error"} 1' in metrics.render()


@pytest.mark.asyncio
async def test_stop_instance_metric_failure_does_not_mask_cause():
    """指标名未注册时也不能顶掉 stop 路径的真实失败原因。"""
    class RejectingMetrics:
        def inc(self, name, **labels):
            raise ValueError(f"unknown runtime metric: {name}")

    client = SecurityRuntimeClient(required=False, metrics=RejectingMetrics())
    called = []

    async def unavailable(*args, **kwargs):
        called.append(args)
        raise RuntimeError("securityd StopInstance unavailable")

    client._call = unavailable
    assert await client.stop_instance("lease-1", "instance-1", "stop") is None
    assert called == [("stop_runtime_instance", "lease-1", "instance-1", "stop")]


def test_missing_client_reports_the_real_reason(monkeypatch):
    """没装安全客户端时要说人话。

    `except ImportError as first:` 会在 except 块结束时删掉 `first`，
    末尾 `raise ... from first` 于是变成 UnboundLocalError —— 真原因
    「未找到 bianbu_security_client」被一句 Python 内部报错盖掉，
    用户和运维都无从下手（实测 Runtime 启动就是这句）。
    """
    def missing_client():
        raise ModuleNotFoundError(
            "No module named 'bianbu_security_client'",
            name="bianbu_security_client",
        )

    monkeypatch.setattr(security_runtime, "_import_client", missing_client)
    monkeypatch.setattr(security_runtime, "_client_search_dirs", lambda: ())
    monkeypatch.setattr(security_runtime, "_client_dependency_dirs", lambda: ())

    with pytest.raises(SecurityRuntimeUnavailable) as failure:
        security_runtime._client_module()

    assert "未找到 bianbu_security_client" in str(failure.value)
    assert "bianbu_security_client" in str(failure.value.__cause__)
