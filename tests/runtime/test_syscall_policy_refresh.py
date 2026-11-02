"""Saved syscall policies must be applied before the next Run."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import bianbu_agent.security.security_runtime as security_runtime
from bianbu_agent.security.runtime_process import supervisor as supervisor_module
from bianbu_agent.security.runtime_process.supervisor import (
    ControlRunManager,
    WorkerSupervisor,
)
from bianbu_agent.security.runtime_process.worker import MemoryRunStore
from bianbu_agent.security.security_runtime import SecurityRuntimeClient


class PolicyClient:
    def __init__(self, policy, network=None):
        self.policy = policy
        self.network = network or {}
        self.calls = []
        self.network_calls = []

    async def get_syscall_policy(self, agent_id, arch):
        self.calls.append((agent_id, arch))
        return dict(self.policy)

    async def get_network_policy(self, agent_id):
        self.network_calls.append(agent_id)
        return dict(self.network)


class RefreshableWorker:
    """Minimal Worker+RPC double that records policy refreshes and dispatch."""

    def __init__(self):
        self.agent_id = "agent.demo"
        self.ready = True
        self.identity = SimpleNamespace(generation_id=1, sandboxed=True)
        self.client = self
        self.refresh_calls = 0
        self.refresh_result = False
        self.network_refresh_calls = 0
        self.network_refresh_result = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.dispatched = []

    async def refresh_syscall_policy_if_needed(self):
        self.refresh_calls += 1
        if self.refresh_result:
            self.identity = SimpleNamespace(generation_id=2, sandboxed=True)
        return self.refresh_result

    async def refresh_network_policy_if_needed(self):
        self.network_refresh_calls += 1
        return self.network_refresh_result

    async def run_start(self, payload, on_event):
        self.started.set()
        await self.release.wait()
        self.dispatched.append(payload)
        await on_event({"type": "run.accepted"})
        await on_event({"type": "run.result", "result": {
            "request_id": payload["run_id"], "status": "completed",
            "content": "ok",
        }})


def _bare_supervisor(policy):
    supervisor = object.__new__(WorkerSupervisor)
    supervisor.agent_id = "agent.demo"
    supervisor.client = object()
    supervisor.identity = SimpleNamespace(sandboxed=True)
    supervisor._ready = asyncio.Event()
    supervisor._ready.set()
    supervisor.coordinator = SimpleNamespace(
        security_client=PolicyClient(policy))
    calls = []

    async def stop(*, reason="requested", strict=False):
        calls.append(("stop", reason))

    async def start():
        calls.append(("start", ""))
        return supervisor.identity

    supervisor.stop = stop
    supervisor.start = start
    return supervisor, calls


def test_runtime_client_forwards_syscall_policy(monkeypatch):
    seen = []
    monkeypatch.setattr(security_runtime, "_client_module", lambda: SimpleNamespace(
        get_syscall_policy=lambda agent_id, arch: seen.append((agent_id, arch)) or {
            "pending_restart": True,
        },
    ))

    result = asyncio.run(
        SecurityRuntimeClient(required=True).get_syscall_policy(
            "agent.demo", "riscv64"))

    assert result == {"pending_restart": True}
    assert seen == [("agent.demo", "riscv64")]


def test_runtime_client_forwards_network_policy(monkeypatch):
    """The refresh path reads the version through the Runtime security client.

    Regression: WorkerSupervisor looks the getter up with ``getattr`` and skips
    the rebuild when it is missing, so a client without this method silently
    left every saved whitelist stuck on the previous deny_all verdict.
    """

    seen = []
    monkeypatch.setattr(security_runtime, "_client_module", lambda: SimpleNamespace(
        get_network_policy=lambda agent_id: seen.append(agent_id) or {
            "mode": "whitelist", "domains": ["api.deepseek.com"], "version": 17,
        },
    ))

    result = asyncio.run(
        SecurityRuntimeClient(required=True).get_network_policy("agent.demo"))

    assert result == {
        "mode": "whitelist", "domains": ["api.deepseek.com"], "version": 17,
    }
    assert seen == ["agent.demo"]


@pytest.mark.asyncio
async def test_pending_syscall_policy_rebuilds_sandbox_worker(monkeypatch):
    monkeypatch.setattr(supervisor_module, "normalize_arch", lambda: "riscv64")
    supervisor, calls = _bare_supervisor({
        "pending_restart": True,
        "policy_version": 8,
        "applied_policy_version": 7,
    })

    assert await supervisor.refresh_syscall_policy_if_needed() is True
    assert calls == [("stop", "syscall policy changed"), ("start", "")]
    assert supervisor.coordinator.security_client.calls == [
        ("agent.demo", "riscv64")]


@pytest.mark.asyncio
async def test_unchanged_policy_keeps_worker(monkeypatch):
    monkeypatch.setattr(supervisor_module, "normalize_arch", lambda: "riscv64")
    supervisor, calls = _bare_supervisor({
        "pending_restart": False,
        "policy_version": 7,
        "applied_policy_version": 7,
    })

    assert await supervisor.refresh_syscall_policy_if_needed() is False
    assert calls == []


@pytest.mark.asyncio
async def test_host_worker_is_not_rebuilt_for_syscall_policy(monkeypatch):
    """YOLO Workers do not load seccomp, so a policy change must not restart them."""
    monkeypatch.setattr(supervisor_module, "normalize_arch", lambda: "riscv64")
    supervisor, calls = _bare_supervisor({"pending_restart": True})
    supervisor.identity = SimpleNamespace(sandboxed=False)

    assert await supervisor.refresh_syscall_policy_if_needed() is False
    assert calls == []
    assert supervisor.coordinator.security_client.calls == []


@pytest.mark.asyncio
async def test_next_run_refreshes_worker_before_dispatch():
    worker = RefreshableWorker()
    worker.refresh_result = True
    manager = ControlRunManager(MemoryRunStore(), worker)
    try:
        handle = await manager.run("session", "hello", agent_id=worker.agent_id)
        worker.release.set()
        result = await asyncio.wait_for(handle.wait(), 1)
        assert worker.refresh_calls == 1
        assert handle.generation_id == 2
        assert result.status == "completed"
    finally:
        worker.release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_busy_agent_defers_policy_refresh():
    worker = RefreshableWorker()
    manager = ControlRunManager(MemoryRunStore(), worker)
    try:
        first = await manager.run("session-1", "hello", agent_id=worker.agent_id)
        await asyncio.wait_for(worker.started.wait(), 1)
        worker.refresh_result = True

        second = await manager.run("session-2", "again", agent_id=worker.agent_id)
        assert worker.refresh_calls == 1

        worker.release.set()
        await asyncio.wait_for(first.wait(), 1)
        await asyncio.wait_for(second.wait(), 1)
        assert len(worker.dispatched) == 2
    finally:
        worker.release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_changed_network_policy_rebuilds_the_worker():
    """A saved whitelist only takes effect after the address filter is reinstalled.

    Sandbox Workers get it with their network namespace and host Workers through
    the systemd scope's IPAddress* properties, so both must be rebuilt.
    """

    supervisor, calls = _bare_supervisor({})
    supervisor.coordinator.security_client = PolicyClient(
        {}, {"mode": "whitelist", "domains": ["api.deepseek.com"], "version": 17},
    )
    supervisor._network_policy_version = 16

    assert await supervisor.refresh_network_policy_if_needed() is True
    assert calls == [("stop", "network policy changed"), ("start", "")]
    assert supervisor.coordinator.security_client.network_calls == ["agent.demo"]


@pytest.mark.asyncio
async def test_unchanged_network_policy_keeps_the_worker():
    supervisor, calls = _bare_supervisor({})
    supervisor.coordinator.security_client = PolicyClient(
        {}, {"mode": "whitelist", "domains": ["api.deepseek.com"], "version": 17},
    )
    supervisor._network_policy_version = 17

    assert await supervisor.refresh_network_policy_if_needed() is False
    assert calls == []


@pytest.mark.asyncio
async def test_host_worker_is_rebuilt_for_a_network_policy_change():
    supervisor, calls = _bare_supervisor({})
    supervisor.identity = SimpleNamespace(sandboxed=False)
    supervisor.coordinator.security_client = PolicyClient(
        {}, {"mode": "allow_all", "domains": [], "version": 3},
    )
    supervisor._network_policy_version = 2

    assert await supervisor.refresh_network_policy_if_needed() is True
    assert calls == [("stop", "network policy changed"), ("start", "")]


@pytest.mark.asyncio
async def test_next_run_applies_a_changed_network_policy():
    """Saving a whitelist must rebuild the Worker before the next Run starts.

    Otherwise the Run keeps the network verdict the Worker compiled at launch
    and the model connection fails with the previous ``deny_all`` snapshot.
    """

    worker = RefreshableWorker()
    worker.network_refresh_result = True
    manager = ControlRunManager(MemoryRunStore(), worker)
    try:
        handle = await manager.run("session", "hello", agent_id=worker.agent_id)
        worker.release.set()
        result = await asyncio.wait_for(handle.wait(), 1)
        assert worker.refresh_calls == 1
        assert worker.network_refresh_calls == 1
        assert result.status == "completed"
    finally:
        worker.release.set()
        await manager.close()
