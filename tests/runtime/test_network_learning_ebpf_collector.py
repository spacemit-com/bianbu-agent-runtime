from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _module():
    path = Path(__file__).parents[2] / "scripts" / "network-learning-ebpf-collector.py"
    spec = importlib.util.spec_from_file_location("network_learning_ebpf_collector", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_register_and_unregister_messages_track_worker_identity():
    module = _module()
    collector = module.Collector()
    register = {
        "op": "register",
        "instance_id": "i-1",
        "cgroup_path": "/sys/fs/cgroup/user.slice/worker.scope",
        "agent_id": "agent-a",
        "lease_id": "lease-1",
        "pid": 1234,
    }

    response = collector.handle_message(json.dumps(register))

    assert response == {"ok": True, "op": "register", "instance_id": "i-1"}
    assert collector.registrations["i-1"].agent_id == "agent-a"
    assert collector.handle_message(json.dumps({"op": "unregister", "instance_id": "i-1"})) == {
        "ok": True,
        "op": "unregister",
        "instance_id": "i-1",
    }
    assert collector.registrations == {}


def test_events_are_allow_listed_by_kernel_cgroup_and_normalized():
    module = _module()
    collector = module.Collector()
    collector.handle_message(json.dumps({
        "op": "register", "instance_id": "i-1",
        "cgroup_path": "/sys/fs/cgroup/worker.scope", "agent_id": "a",
        "lease_id": "l", "pid": 99,
    }))

    event = collector.filter_event({
        "pid": 99,
        "cgroup_path": "/sys/fs/cgroup/worker.scope",
        "timestamp": 12.5,
        "protocol": "tcp",
        "remote_ip": "2001:0db8::1",
        "remote_port": 443,
        "retval": 0,
    })
    assert event == {
        "observed_at": 12.5, "event_type": "connect", "protocol": "tcp",
        "remote_ip": "2001:db8::1", "remote_port": 443,
        "outcome": "success", "errno": "", "pid": 99,
        "agent_id": "a", "instance_id": "i-1", "lease_id": "l",
        "backend": "preload",
    }
    assert collector.filter_event({**event, "cgroup_path": "/sys/fs/cgroup/other.scope"}) is None


def test_preload_event_uses_registered_instance_when_child_pid_has_exited():
    module = _module()
    collector = module.Collector()
    collector.handle_message({
        "op": "register", "instance_id": "worker-1",
        "cgroup_path": "/sys/fs/cgroup/worker.scope", "agent_id": "agent",
        "lease_id": "lease", "pid": 123,
    })
    event = collector.filter_event({
        "instance_id": "worker-1", "pid": 999999999,
        "remote_ip": "203.0.113.5", "remote_port": 443, "retval": -1,
    })
    assert event is not None
    assert event["instance_id"] == "worker-1"
    assert event["lease_id"] == "lease"


def test_bpftrace_unavailable_is_reported_as_degraded(monkeypatch):
    module = _module()
    collector = module.Collector()
    monkeypatch.setattr(module.shutil, "which", lambda name: None)

    assert collector.start_bpftrace() is False
    assert collector.status == "degraded"
    assert "bpftrace" in collector.reason


def test_stop_is_graceful_and_idempotent(tmp_path):
    module = _module()
    collector = module.Collector(socket_path=str(tmp_path / "collector.sock"))

    collector.stop()
    collector.stop()

    assert collector.status == "inactive"


def test_normalization_preserves_dns_and_explicit_failure():
    event = _module().Collector.normalize_event({
        'event_type': 'dns_query', 'domain': 'localhost', 'dns_qtype': 'A',
        'outcome': 'failed', 'errno': '-2', 'backend': 'preload',
    })
    assert event['outcome'] == 'failed'
    assert event['errno'] == '-2'
    assert event['domain'] == 'localhost'
    assert event['dns_qtype'] == 'A'
