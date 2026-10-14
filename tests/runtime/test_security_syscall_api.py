"""Contract tests for Runtime's native syscall security API.

These tests exercise the Runtime bridge (``security.py``) rather than the
standalone Security WebUI.  Every operation must be translated to the
corresponding ``bianbu_security_client`` method and preserve the policy
objects returned by securityd.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from bianbu_agent.api import security


def test_syscall_catalog_operation_forwards_arch(monkeypatch):
    calls = []

    def fake_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return {
            "arch": args[0],
            "syscalls": [
                {"name": "ptrace", "number": 101, "description": "trace process"}
            ],
            "mandatory": ["ptrace"],
        }

    monkeypatch.setattr(security, "_call_client", fake_call)
    result = security._invoke_sync("syscall-catalog.v1", {"arch": "riscv64"})

    assert result["arch"] == "riscv64"
    assert result["mandatory"] == ["ptrace"]
    assert calls == [("get_syscall_catalog", ("riscv64",), {})]


def test_syscall_policy_get_preserves_securityd_snapshot(monkeypatch):
    calls = []
    expected = {
        "agent_id": "agent.demo",
        "arch": "x86_64",
        "mandatory": ["ptrace", "mount"],
        "additions": ["setns"],
        "effective": ["ptrace", "mount", "setns"],
        "policy_version": 7,
        "applied_version": 6,
        "pending_restart": True,
    }
    monkeypatch.setattr(
        security,
        "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args)) or expected,
    )

    result = security._invoke_sync(
        "syscall-policy.get.v1", {"agent_id": "agent.demo", "arch": "x86_64"}
    )

    assert result == expected
    assert calls == [("get_syscall_policy", ("agent.demo", "x86_64"))]


def test_syscall_policy_update_forwards_agent_arch_and_additions(monkeypatch):
    calls = []
    monkeypatch.setattr(
        security,
        "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args))
        or {"policy_version": 8, "additions": ["setns"]},
    )

    result = security._invoke_sync(
        "syscall-policy.agent.update.v1",
        {"agent_id": "agent.demo", "arch": "riscv64", "additions": ["setns"]},
    )

    assert result["policy_version"] == 8
    assert calls == [("set_agent_syscall_policy", ("agent.demo", "riscv64", ["setns"]))]


def test_syscall_policy_reset_forwards_agent_and_arch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        security,
        "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args)) or {"policy_version": 9},
    )

    result = security._invoke_sync(
        "syscall-policy.agent.reset.v1", {"agent_id": "agent.demo", "arch": "x86_64"}
    )

    assert result == {"policy_version": 9}
    assert calls == [("reset_agent_syscall_policy", ("agent.demo", "x86_64"))]


def test_syscall_audit_maps_runtime_lease_events_to_selected_agent(monkeypatch):
    calls = []
    rows = [
        {"agent_id": "com.bianbu.runtime", "instance_id": "agent.demo-abc",
         "syscall": "finit_module"},
        {"agent_id": "other", "instance_id": "other-xyz", "syscall": "mount"},
        {"agent_id": "agent.demo", "instance_id": "", "syscall": "ptrace"},
    ]
    monkeypatch.setattr(
        security, "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args)) or rows,
    )
    result = security._invoke_sync(
        "syscall-audit.events.v1", {"agent_id": "agent.demo", "limit": 100})
    assert [row["syscall"] for row in result] == ["finit_module", "ptrace"]
    assert calls == [("get_syscall_audit_events", ("", "", 100, 0))]


@pytest.mark.parametrize(
    ("operation", "params"),
    [
        ("syscall-policy.get.v1", {"agent_id": "agent.demo"}),
        ("syscall-policy.agent.update.v1", {"agent_id": "agent.demo"}),
        ("syscall-policy.agent.reset.v1", {"agent_id": "agent.demo"}),
    ],
)
def test_syscall_operations_default_to_x86_64(monkeypatch, operation, params):
    calls = []
    monkeypatch.setattr(
        security,
        "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args)) or {},
    )
    security._invoke_sync(operation, params)

    args = calls[0][1]
    if operation == "syscall-policy.agent.update.v1":
        assert args[1] == "x86_64"
    else:
        assert args[-1] == "x86_64"


def test_syscall_operations_propagate_securityd_unavailable(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise HTTPException(503, "securityd unavailable: dbus-python ABI requires helper bridge")

    monkeypatch.setattr(security, "_call_client", unavailable)
    with pytest.raises(HTTPException, match="dbus-python ABI"):
        security._invoke_sync("syscall-catalog.v1", {"arch": "x86_64"})


@pytest.mark.asyncio
async def test_invoke_security_wraps_syscall_failure_in_actionable_envelope(monkeypatch):
    monkeypatch.setattr(
        security,
        "_invoke_sync",
        lambda *_args: (_ for _ in ()).throw(
            HTTPException(503, "securityd unavailable: socket missing")
        ),
    )

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/security-api/invoke",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
    }
    body = (
        b'{"protocolVersion":1,"serviceId":"security",'
        b'"requestId":"syscall-1","operationId":"syscall-catalog.v1",'
        b'"params":{"arch":"x86_64"}}'
    )
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            return {"type": "http.disconnect"}
        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    response = await security.invoke_security(Request(scope, receive))
    assert response["ok"] is False
    assert response["requestId"] == "syscall-1"
    assert response["error"]["code"] == "unavailable"
    assert "socket missing" in response["error"]["message"]
