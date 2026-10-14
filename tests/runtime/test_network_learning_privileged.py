from __future__ import annotations
import json, socket, threading
from bianbu_agent.security.network_learning_privileged import PrivilegedNetworkLearningClient

def test_client_registers_and_forwards_event(tmp_path):
    path = str(tmp_path / "collector.sock"); seen = []; ready = threading.Event()
    def server():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.bind(path); s.listen(1); ready.set()
        c, _ = s.accept(); f = c.makefile("r")
        msg = json.loads(f.readline()); assert msg["op"] == "register"
        c.sendall(b'{"ok":true,"op":"register"}\n')
        c.sendall(b'{"event_type":"connect","remote_ip":"1.2.3.4","remote_port":443}\n')
        f.readline(); c.close(); s.close()
    threading.Thread(target=server, daemon=True).start(); ready.wait()
    client = PrivilegedNetworkLearningClient(socket_path=path, event_callback=seen.append)
    assert client.register(instance_id="i", cgroup_path="/cg", agent_id="a", lease_id="l", pid=1)
    for _ in range(20):
        if seen: break
        threading.Event().wait(.01)
    assert seen[0]["remote_port"] == 443
    client.unregister()

def test_client_reports_degraded_when_socket_unavailable(tmp_path):
    states = []; client = PrivilegedNetworkLearningClient(socket_path=str(tmp_path / "missing"), status_callback=lambda s,r: states.append((s,r)))
    assert client.register(instance_id="i", cgroup_path="/cg", agent_id="a", lease_id="l", pid=1) is False
    assert states and states[-1][0] == "degraded"
    client.unregister()


def test_client_reconnects_after_eof_and_preserves_partial_event(tmp_path):
    path = str(tmp_path / 'restart.sock')
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path); server.listen(2); server.settimeout(4)
    states = []; events = []; recovered = threading.Event()
    client = PrivilegedNetworkLearningClient(socket_path=path, timeout=.1,
        status_callback=lambda s, r: states.append(s),
        event_callback=lambda e: (events.append(e), recovered.set()))
    def serve():
        first, _ = server.accept()
        first.recv(4096); first.sendall(b'{"ok":true}\n'); first.close()
        second, _ = server.accept()
        registration = json.loads(second.recv(4096))
        assert registration['instance_id'] == 'i'
        second.sendall(b'{"ok":true}\n{"event_type":"con')
        threading.Event().wait(.05)
        second.sendall(b'nect","remote_port":443}\n')
        second.recv(4096); second.close()
    thread = threading.Thread(target=serve, daemon=True); thread.start()
    try:
        assert client.register(instance_id='i', cgroup_path='/cg', agent_id='a', lease_id='l', pid=1)
        assert recovered.wait(3), 'collector did not reconnect and deliver complete event'
        assert 'degraded' in states and states[-1] == 'active'
        assert events[0]['remote_port'] == 443
    finally:
        client.unregister(); server.close(); thread.join(timeout=1)


def test_client_recovers_when_collector_was_absent_at_registration(tmp_path):
    path = str(tmp_path / 'late.sock'); ready = threading.Event()
    states = []
    client = PrivilegedNetworkLearningClient(socket_path=path, timeout=.1,
        status_callback=lambda s, r: (states.append(s), ready.set() if s == 'active' else None))
    assert not client.register(instance_id='i', cgroup_path='/cg', agent_id='a', lease_id='l', pid=1)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path); server.listen(1); server.settimeout(3)
    try:
        connection, _ = server.accept()
        with connection:
            assert json.loads(connection.recv(4096))['lease_id'] == 'l'
            connection.sendall(b'{"ok":true}\n')
            assert ready.wait(2)
            client.unregister()
            assert not client._thread.is_alive()
        assert states[0] == 'degraded' and states[-1] == 'inactive'
    finally:
        client.unregister(); server.close()
