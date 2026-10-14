from __future__ import annotations

import asyncio
import json
import os

import pytest


def test_development_runtime_does_not_register_without_explicit_service_switch(
    monkeypatch, tmp_path
) -> None:
    from bianbu_agent.server.memory_registry import register

    monkeypatch.setenv("BIANBU_AGENT_MEMORY_REGISTRY_DIR", str(tmp_path))
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "development")
    monkeypatch.delenv("BIANBU_AGENT_MEMORY_REGISTRATION", raising=False)

    assert register() is None
    assert tuple(tmp_path.iterdir()) == ()


def test_managed_development_runtime_registers_the_runtime_agent_id(
    monkeypatch, tmp_path
) -> None:
    from bianbu_agent.server.memory_registry import register, unregister

    monkeypatch.setenv("BIANBU_AGENT_MEMORY_REGISTRY_DIR", str(tmp_path))
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "development")
    monkeypatch.setenv("BIANBU_AGENT_MEMORY_REGISTRATION", "1")
    monkeypatch.setenv("BIANBU_AGENT_MEMORY_ACCOUNT_ID", "runtime-account")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")

    path = register(agent_name="bianbu-agent-default")
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["account_id"] == "runtime-account"
    assert payload["agent_name"] == "bianbu-agent-default"
    assert payload["agent_id"] == "com.bianbu.agent"
    assert "allow_runtime_context" not in payload

    unregister(path)
    assert not path.exists()


def test_register_can_use_host_pid_for_sandbox_worker(
    monkeypatch, tmp_path
) -> None:
    from bianbu_agent.server import memory_registry

    monkeypatch.setenv("BIANBU_AGENT_MEMORY_REGISTRY_DIR", str(tmp_path))
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv("BIANBU_AGENT_MEMORY_ACCOUNT_ID", "runtime-account")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")
    monkeypatch.setattr(memory_registry, "_start_time_ticks", lambda pid: 12345)

    path = memory_registry.register(agent_name="default", pid=4242)
    assert path == tmp_path / "4242.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pid_start_time_ticks"] == 12345
    assert payload["account_id"] == "runtime-account"
    assert payload["agent_name"] == "default"
    assert payload["agent_id"] == "com.bianbu.agent"

    memory_registry.unregister(path)
    assert not path.exists()



def test_registration_identity_matches_the_shared_memory_lookup(
    monkeypatch, tmp_path
) -> None:
    """The tool adapter resolves its call identity from the same environment.

    memoryd compares the two, so the registry entry and the host context the
    tools send must be produced by one lookup (K3 2026-09-22: the tools sent a
    placeholder agent name and every memory write was refused).
    """

    from bianbu_agent import memory_identity
    from bianbu_agent.server.memory_registry import register, unregister

    monkeypatch.setenv("BIANBU_AGENT_MEMORY_REGISTRY_DIR", str(tmp_path))
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv(memory_identity.ACCOUNT_ENV, "bianbu")
    monkeypatch.setenv(memory_identity.AGENT_NAME_ENV, "com.bianbu.ee2b4775b346")
    monkeypatch.setenv("BIANBU_AGENT_ID", "com.bianbu.agent")

    path = register()
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    unregister(path)

    assert (payload["account_id"], payload["agent_name"]) == (
        memory_identity.env_account_id(),
        memory_identity.env_agent_name(),
    )
    assert (payload["account_id"], payload["agent_name"]) == (
        "bianbu",
        "com.bianbu.ee2b4775b346",
    )


@pytest.fixture
def registration(monkeypatch, tmp_path):
    from bianbu_agent.server import memory_registry as module
    monkeypatch.setenv('BIANBU_AGENT_MEMORY_REGISTRY_DIR', str(tmp_path))
    monkeypatch.setenv('BIANBU_RUNTIME_MODE', 'production')
    clock = [1_800_000_000.0]
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    path = module.register(agent_name='test-agent')
    return module, path, clock


def test_registration_renewal_keeps_identity_valid_beyond_original_ttl(registration, monkeypatch):
    module, path, clock = registration
    original = json.loads(path.read_text())
    lease = module.MemoryRegistration(path)
    monkeypatch.setenv('BIANBU_AGENT_ID', 'different-agent')
    for _ in range(25):
        clock[0] += 3601
        lease.maintain()
    renewed = json.loads(path.read_text())
    assert renewed['expires_at_ms'] > clock[0] * 1000 > original['expires_at_ms']
    assert {k: v for k, v in renewed.items() if k != 'expires_at_ms'} == {
        k: v for k, v in original.items() if k != 'expires_at_ms'}
    assert path.stat().st_mode & 0o777 == 0o640
    assert lease.status()['state'] == 'active'


def test_registration_does_not_write_on_each_health_probe(registration):
    module, path, clock = registration
    lease = module.MemoryRegistration(path)
    inode = path.stat().st_ino
    clock[0] += 5
    lease.maintain()
    assert path.stat().st_ino == inode


@pytest.mark.parametrize('replacement', ['pid_reused', 'pid_dead', 'record_changed', 'record_replaced', 'missing'])
def test_registration_refuses_stale_identity(registration, monkeypatch, replacement):
    module, path, clock = registration
    lease = module.MemoryRegistration(path)
    original = path.read_text()
    if replacement == 'pid_reused':
        monkeypatch.setattr(module, '_start_time_ticks', lambda pid: -1)
    elif replacement == 'pid_dead':
        def gone(pid):
            raise ProcessLookupError(pid)
        monkeypatch.setattr(module, '_start_time_ticks', gone)
    elif replacement == 'record_changed':
        data = json.loads(original)
        data['agent_instance_id'] = 'new-owner'
        path.write_text(json.dumps(data))
    elif replacement == 'record_replaced':
        other = path.with_suffix('.replacement')
        other.write_text(original)
        other.replace(path)
    else:
        path.unlink()
    before = path.read_text() if path.exists() else None
    clock[0] += 3601
    lease.maintain()
    assert lease.status()['state'] == 'degraded'
    assert (path.read_text() if path.exists() else None) == before
    if replacement in {'record_changed', 'record_replaced'}:
        lease.close()
        assert path.read_text() == before


def test_registration_write_failure_retries_without_partial_file(registration, monkeypatch, caplog):
    module, path, clock = registration
    lease = module.MemoryRegistration(path)
    original = path.read_bytes()
    replace = module.os.replace
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(module.os, 'replace', fail)
    clock[0] += 3601
    lease.maintain()
    assert path.read_bytes() == original
    assert lease.status()['state'] == 'degraded'
    assert 'disk unavailable' in caplog.text
    assert list(path.parent.iterdir()) == [path]
    monkeypatch.setattr(module.os, 'replace', replace)
    clock[0] += 61
    lease.maintain()
    assert json.loads(path.read_text())['expires_at_ms'] > json.loads(original)['expires_at_ms']
    assert lease.status()['state'] == 'active'


def test_registration_close_cannot_recreate_file(registration):
    module, path, clock = registration
    lease = module.MemoryRegistration(path)
    lease.close()
    clock[0] += 90000
    lease.maintain()
    assert not path.exists()
    assert lease.status()['state'] == 'stopped'


@pytest.mark.asyncio
async def test_standalone_maintenance_stops_before_unregister(registration):
    module, path, clock = registration
    lease = module.MemoryRegistration(path)
    stop = asyncio.Event()
    clock[0] += 3601
    task = asyncio.create_task(lease.run(stop, interval=0.001))
    for _ in range(100):
        if json.loads(path.read_text())['expires_at_ms'] > clock[0] * 1000 + 23 * 3600000:
            break
        await asyncio.sleep(.001)
    else:
        pytest.fail('standalone registration was not renewed')
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    lease.close()
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy", [True, False])
async def test_supervisor_healthy_worker_renews_and_unregisters(registration, tmp_path, healthy):
    from types import SimpleNamespace
    from bianbu_agent.config import RuntimeConfig, RuntimeControlConfig, RuntimeProcessConfig
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor
    module, path, clock = registration
    original = json.loads(path.read_text())
    supervisor = WorkerSupervisor(RuntimeConfig(data_dir=tmp_path, runtime=RuntimeProcessConfig(
        control=RuntimeControlConfig(heartbeat_interval_seconds=.001))), agent_id='test-agent')
    supervisor._memory_registration_path = path
    supervisor._memory_registration = module.MemoryRegistration(path)
    supervisor.identity = SimpleNamespace(instance_id="test", pid=os.getpid(),
        effective_mode="host", backend="host", sandboxed=False, generation_id="test")
    supervisor._ready.set()
    clock[0] += 3601
    class Client:
        async def health(self):
            supervisor._closed = True
            return {'status': 'running' if healthy else 'failed'}
        async def shutdown(self):
            pass
    supervisor.client = Client()
    await supervisor._health_loop()
    if healthy:
        assert json.loads(path.read_text())['expires_at_ms'] > original['expires_at_ms']
        assert supervisor.status_snapshot()['memory_registration']['state'] == 'active'
    else:
        assert json.loads(path.read_text()) == original
    await supervisor.stop()
    assert not path.exists()
    supervisor._memory_registration_path = None
