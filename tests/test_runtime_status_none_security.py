from __future__ import annotations

from bianbu_agent.api.contracts import RuntimeWorkersView


def test_runtime_workers_status_accepts_missing_security_details():
    payload = {"worker_count": 1, "workers": [{
        "agent_id": "a", "state": "failed", "instance_id": None,
        "pid": None, "effective_mode": None, "backend": None,
        "sandboxed": False, "generation_id": None, "error": "failed",
        "seccomp": {}, "audit": {},
    }], "runtime": {"status":"CONTROL_READY","role":"control","control_ready":True,"agent_ready":False}, "control": {"state":"running","control_ready":True}}
    view = RuntimeWorkersView.model_validate(payload)
    assert view.workers[0].seccomp == {}
    assert view.workers[0].audit == {}
