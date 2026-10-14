"""Host attestation for Hook commands never trusts a caller-supplied PID."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("renewed_parent", [False, True])
async def test_attested_child_gets_parent_identity_until_disconnect(tmp_path, monkeypatch, renewed_parent):
    from bianbu_agent.server.hook_memory_registration import HookMemoryRegistrationServer
    from bianbu_agent.server.memory_registry import _start_time_ticks

    registration = tmp_path / f'{os.getpid()}.json'
    registration.write_text(json.dumps(dict(uid=os.getuid(), gid=os.getgid(), pid_start_time_ticks=_start_time_ticks(os.getpid()), executable_path=str(Path('/proc/self/exe').resolve()), account_id='trusted-account', agent_id='trusted-agent', agent_name='expert', expires_at_ms=9999999999999)))
    if renewed_parent:
        from bianbu_agent.server import memory_registry
        import time
        now = time.time()
        monkeypatch.setenv("BIANBU_AGENT_MEMORY_REGISTRY_DIR", str(tmp_path))
        monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
        monkeypatch.setenv("BIANBU_AGENT_MEMORY_ACCOUNT_ID", "trusted-account")
        registration = memory_registry.register(agent_name="expert")
        lease = memory_registry.MemoryRegistration(registration)
        original_expiry = json.loads(registration.read_text())["expires_at_ms"]
        monkeypatch.setattr(memory_registry.time, "time", lambda: now + 25 * 3600)
        lease.maintain()
        assert original_expiry < time.time() * 1000
        assert lease.status()["state"] == "active"
    server = HookMemoryRegistrationServer(tmp_path / 'identity.sock', worker_pid=os.getpid(), registration_path=registration)
    await server.start()
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', '''import socket, sys
s=socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
s.sendall(b'register\\n')
assert s.recv(100)==b'ok\\n'
print('ready', flush=True)
sys.stdin.readline()
s.close()
''', str(server.path), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 3) == b'ready\n'
        entry = tmp_path / f'{child.pid}.json'
        payload = json.loads(entry.read_text())
        assert payload['account_id'] == 'trusted-account'
        assert payload['agent_name'] == 'expert'
        assert payload['pid_start_time_ticks'] == _start_time_ticks(child.pid)
        child.stdin.close()
        await child.wait()
        for _ in range(20):
            if not entry.exists():
                break
            await asyncio.sleep(0.01)
        assert not entry.exists()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        await server.close()
    assert registration.exists()
    assert not server.path.exists()


@pytest.mark.asyncio
async def test_worker_cannot_register_itself_as_its_own_child(tmp_path):
    from bianbu_agent.server.hook_memory_registration import HookMemoryRegistrationServer
    from bianbu_agent.server.memory_registry import _start_time_ticks

    registration = tmp_path / 'parent.json'
    registration.write_text(json.dumps(dict(uid=os.getuid(), gid=os.getgid(), pid_start_time_ticks=_start_time_ticks(os.getpid()), executable_path=str(Path('/proc/self/exe').resolve()))))
    server = HookMemoryRegistrationServer(tmp_path / 'identity.sock', worker_pid=os.getpid(), registration_path=registration)
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(server.path))
        writer.write(b'register\n')
        await writer.drain()
        assert await reader.readline() == b'denied\n'
        writer.close()
        await writer.wait_closed()
        assert not (tmp_path / f'{os.getpid()}.json').exists()
    finally:
        await server.close()


def parent_registration(tmp_path):
    from bianbu_agent.server.memory_registry import _start_time_ticks
    path = tmp_path / f'{os.getpid()}.json'
    path.write_text(json.dumps(dict(uid=os.getuid(), gid=os.getgid(), pid_start_time_ticks=_start_time_ticks(os.getpid()), executable_path=str(Path('/proc/self/exe').resolve()), account_id='trusted', expires_at_ms=9999999999999)))
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['parent', 'uid', 'gid', 'cgroup', 'executable', 'worker_reused', 'expired'])
async def test_child_attestation_fails_closed(tmp_path, monkeypatch, mismatch):
    from bianbu_agent.server import hook_memory_registration as module
    registration = parent_registration(tmp_path)
    server = module.HookMemoryRegistrationServer(tmp_path / 'identity.sock', worker_pid=os.getpid(), registration_path=registration)
    if mismatch == 'parent':
        monkeypatch.setattr(module, '_parent_pid', lambda pid: os.getpid() + 100)
    elif mismatch == 'cgroup':
        monkeypatch.setattr(module, '_cgroup', lambda pid: 'parent' if pid == os.getpid() else 'foreign')
    elif mismatch == 'executable':
        monkeypatch.setattr(module.os.path, 'samefile', lambda *args: False)
    elif mismatch == 'worker_reused':
        original = module._start_time_ticks
        monkeypatch.setattr(module, '_start_time_ticks', lambda pid: original(pid) + (1 if pid == os.getpid() else 0))
    else:
        payload = json.loads(registration.read_text())
        payload[{'uid': 'uid', 'gid': 'gid', 'expired': 'expires_at_ms'}[mismatch]] = -1
        registration.write_text(json.dumps(payload))
    await server.start()
    try:
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', '''import socket,sys
s=socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
s.sendall(b'register\\n')
sys.stdout.buffer.write(s.recv(100))
''', str(server.path), stdout=asyncio.subprocess.PIPE)
        output, _ = await asyncio.wait_for(child.communicate(), 3)
        assert output == b'denied\n'
        assert not (tmp_path / f'{child.pid}.json').exists()
    finally:
        await server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup', ['server_stop', 'child_crash'])
async def test_registration_is_removed_on_stop_and_crash(tmp_path, cleanup):
    from bianbu_agent.server.hook_memory_registration import HookMemoryRegistrationServer
    server = HookMemoryRegistrationServer(tmp_path / 'identity.sock', worker_pid=os.getpid(), registration_path=parent_registration(tmp_path))
    await server.start()
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', '''import socket, sys
s=socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
s.sendall(b'register\\n')
assert s.recv(100)==b'ok\\n'
print('ready', flush=True)
sys.stdin.readline()
''', str(server.path), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 3) == b'ready\n'
        entry = tmp_path / f'{child.pid}.json'
        assert entry.exists()
        if cleanup == 'server_stop':
            await asyncio.wait_for(server.close(), 2)
        else:
            child.kill()
            await child.wait()
        for _ in range(20):
            if not entry.exists():
                break
            await asyncio.sleep(0.01)
        assert not entry.exists()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        await server.close()


@pytest.mark.asyncio
async def test_actual_command_protocol_waits_for_attested_registration(tmp_path, monkeypatch):
    from bianbu_agent.server.hook_memory_registration import HookMemoryRegistrationServer, command_registration_socket
    from bianbu_agent.hooks.command import _execute
    worker_socket = tmp_path / 'worker.sock'
    server = HookMemoryRegistrationServer(command_registration_socket(worker_socket), worker_pid=os.getpid(), registration_path=parent_registration(tmp_path))
    await server.start()
    monkeypatch.setenv('BIANBU_WORKER_SOCKET', str(worker_socket))
    monkeypatch.setenv('BIANBU_WORKER_LEASE_ID', 'attested-test')
    monkeypatch.setenv('BIANBU_AGENT_MEMORY_REGISTRY_DIR', str(tmp_path))
    plugin = Path(__file__).resolve().parents[2] / 'resources/plugins/bianbu.memory'
    script = tmp_path / 'prove_identity.py'
    script.write_text('''import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.environ['PLUGIN_ROOT']) / 'scripts'))
from protocol import main
async def before_run(request):
    record = json.loads(Path(os.environ['BIANBU_AGENT_MEMORY_REGISTRY_DIR'], str(os.getpid()) + '.json').read_text())
    assert record['account_id'] == 'trusted'
    return {'metadata': {'registered': True}}
main(before_run)
''')
    try:
        result = await _execute(f'exec "${{RUNTIME_PYTHON}}" "{script}"', json.dumps({'schema_version': 1, 'metadata': {}}).encode(), root=plugin, workspace=tmp_path, timeout=3, data_root=tmp_path)
        assert json.loads(result) == {'metadata': {'registered': True}}
        assert list(tmp_path.glob('[0-9]*.json')) == [server.registration_path]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_command_does_not_run_business_logic_when_registration_unavailable(tmp_path, monkeypatch):
    from bianbu_agent.hooks.command import _execute
    monkeypatch.setenv('BIANBU_WORKER_SOCKET', str(tmp_path / 'missing.sock'))
    monkeypatch.setenv('BIANBU_WORKER_LEASE_ID', 'managed')
    monkeypatch.setenv('BIANBU_AGENT_MEMORY_REGISTRY_DIR', str(tmp_path))
    plugin = Path(__file__).resolve().parents[2] / 'resources/plugins/bianbu.memory'
    with pytest.raises(ValueError, match='exited with status'):
        await _execute('exec "${RUNTIME_PYTHON}" "${PLUGIN_ROOT}/scripts/after_run.py"', json.dumps({'schema_version': 1, 'metadata': {'memory_to_record': 'must not write'}}).encode(), root=plugin, workspace=tmp_path, timeout=3, data_root=tmp_path)
    assert not (tmp_path / 'memory.jsonl').exists()


HOLDING_CHILD = '''import socket, sys
s=socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
s.sendall(b'register\\n')
assert s.recv(100)==b'ok\\n'
print('ready', flush=True)
sys.stdin.readline()
s.close()
'''


async def holding_child(server):
    child = await asyncio.create_subprocess_exec(
        sys.executable, '-c', HOLDING_CHILD, str(server.path),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    assert await asyncio.wait_for(child.stdout.readline(), 3) == b'ready\n'
    return child


def production_parent(tmp_path, monkeypatch):
    from bianbu_agent.server import memory_registry
    monkeypatch.setenv('BIANBU_AGENT_MEMORY_REGISTRY_DIR', str(tmp_path))
    monkeypatch.setenv('BIANBU_RUNTIME_MODE', 'production')
    monkeypatch.setenv('BIANBU_AGENT_MEMORY_ACCOUNT_ID', 'trusted-account')
    return memory_registry.register(agent_name='expert')


@pytest.mark.asyncio
async def test_long_lived_child_registration_is_renewed_while_connected(tmp_path, monkeypatch):
    import time
    from bianbu_agent.server import hook_memory_registration as module
    from bianbu_agent.server import memory_registry

    monkeypatch.setattr(memory_registry, 'RENEW_INTERVAL_SECONDS', 0.05, raising=False)
    monkeypatch.setattr(module, 'RETRY_SECONDS', 0.01, raising=False)
    server = module.HookMemoryRegistrationServer(tmp_path / 'identity.sock', worker_pid=os.getpid(), registration_path=production_parent(tmp_path, monkeypatch))
    await server.start()
    child = await holding_child(server)
    try:
        entry = tmp_path / f'{child.pid}.json'
        original = json.loads(entry.read_text())
        deadline = time.monotonic() + 5
        renewed = original
        while time.monotonic() < deadline and renewed['expires_at_ms'] <= original['expires_at_ms']:
            await asyncio.sleep(0.01)
            renewed = json.loads(entry.read_text())
        assert renewed['expires_at_ms'] > original['expires_at_ms']
        assert {k: v for k, v in renewed.items() if k != 'expires_at_ms'} == {
            k: v for k, v in original.items() if k != 'expires_at_ms'}
        child.stdin.close()
        await child.wait()
        for _ in range(50):
            if not entry.exists():
                break
            await asyncio.sleep(0.01)
        assert not entry.exists()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        await server.close()


@pytest.mark.asyncio
async def test_renewal_keeps_a_replaced_child_registration_untouched(tmp_path, monkeypatch):
    from bianbu_agent.server import hook_memory_registration as module
    from bianbu_agent.server import memory_registry

    monkeypatch.setattr(memory_registry, 'RENEW_INTERVAL_SECONDS', 0.05, raising=False)
    monkeypatch.setattr(module, 'RETRY_SECONDS', 0.01, raising=False)
    server = module.HookMemoryRegistrationServer(tmp_path / 'identity.sock', worker_pid=os.getpid(), registration_path=production_parent(tmp_path, monkeypatch))
    await server.start()
    child = await holding_child(server)
    try:
        entry = tmp_path / f'{child.pid}.json'
        foreign = {**json.loads(entry.read_text()), 'account_id': 'other-account'}
        entry.write_text(json.dumps(foreign))
        await asyncio.sleep(0.4)
        assert json.loads(entry.read_text()) == foreign
        child.stdin.close()
        await child.wait()
        await asyncio.sleep(0.1)
        assert json.loads(entry.read_text()) == foreign
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        await server.close()
    assert json.loads(entry.read_text()) == foreign
