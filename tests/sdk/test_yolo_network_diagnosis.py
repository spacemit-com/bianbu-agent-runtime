"""A live mode switch must not diagnose stale normal policy as a YOLO denial."""
import pytest

from bianbu_agent.runs.manager import _agent_network_policy
from bianbu_agent.security.security_errors import classify_run_exception
from bianbu_agent.security import security_runtime


@pytest.mark.parametrize('managed', [False, True])
def test_yolo_connection_failure_ignores_normal_policy_then_restores(monkeypatch, managed):
    mode = 'yolo'
    policy_reads = []
    policy = {'agent_id': 'agent.demo', 'mode': 'deny_all', 'domains': []}
    import json
    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setenv('BIANBU_WORKER_MANAGED', '1' if managed else '0')
    monkeypatch.setenv('BIANBU_WORKER_NETWORK_POLICY', json.dumps(policy))

    class Client:
        def get_agent_security_mode(self, agent_id):
            return {'mode': mode}

        def get_network_policy(self, agent_id):
            policy_reads.append(mode)
            return policy

    monkeypatch.setattr(security_runtime, 'security_client_module', lambda: Client())
    effective = _agent_network_policy('agent.demo')
    result = classify_run_exception(
        ConnectionError('Connection error.'), agent_id='agent.demo',
        provider_base_url='https://api.example.com/v1', network_policy=effective,
    )
    assert policy_reads == []
    assert effective is None
    assert result is None or result['code'] != 'network_policy_blocked'
    mode = 'normal'
    assert _agent_network_policy('agent.demo') == policy
