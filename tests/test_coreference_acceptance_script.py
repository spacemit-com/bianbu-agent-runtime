import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / 'scripts/acceptance/coreference_runtime.py'
spec = importlib.util.spec_from_file_location('coreference_acceptance', PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_run_ignores_previous_completed_event(monkeypatch):
    calls = iter([
        {'data': {'accepted': True, 'run_id': 'new'}},
        {'data': [{'type': 'run/completed', 'run_id': 'old', 'data': {'content': 'old answer'}}]},
        {'data': [{'type': 'run/completed', 'run_id': 'new', 'data': {'content': 'new answer', 'status': 'completed'}}]},
    ])
    monkeypatch.setattr(mod, 'request_json', lambda *a: {'data': {'running': False}} if a[1] == 'GET' and not a[2].endswith('/events') else next(calls))
    monkeypatch.setattr(mod.time, 'sleep', lambda _: None)
    assert mod.run_chat('http://local', 'session', 'question', 2)['content'] == 'new answer'


def test_approval_is_not_automatically_granted(monkeypatch):
    calls = []
    def request(base, method, path, payload, timeout):
        calls.append((method, path))
        if path.endswith('/prompt'):
            return {'data': {'accepted': True, 'run_id': 'new'}}
        if method == 'GET' and not path.endswith('/events'):
            return {'data': {'running': False, 'pending_interaction': 'approval'}}
        if '/approvals/' in path:
            raise RuntimeError('unsafe automatic approval')
        return {'data': [{'type': 'approval/requested', 'run_id': 'new', 'data': {'approval_id': 'approval'}}]}
    monkeypatch.setattr(mod, 'request_json', request)
    with pytest.raises(RuntimeError, match='approval'):
        mod.run_chat('http://local', 'session', 'question', 2)
    assert not any('/approvals/' in path for _, path in calls)


def test_failed_run_cannot_reuse_old_answer(monkeypatch):
    calls = iter([
        {'data': {'accepted': True, 'run_id': 'new'}},
        {'data': [{'type': 'run/completed', 'run_id': 'old', 'data': {'content': 'old'}},
                  {'type': 'run/failed', 'run_id': 'new', 'data': {'error': 'failure'}}]},
    ])
    monkeypatch.setattr(mod, 'request_json', lambda *a: {'data': {'running': False}} if a[1] == 'GET' and not a[2].endswith('/events') else next(calls))
    with pytest.raises(RuntimeError, match='failed'):
        mod.run_chat('http://local', 'session', 'question', 2)


def test_heuristic_is_not_acceptance_verdict():
    result = mod.classify({'expected_referent': 'curl', 'expected_action_or_answer': 'install curl'},
                          'curl is wrong. The answer is SQLite.')
    assert result['passed'] is None
    assert result['review_required'] is True


def test_history_assistant_fixture_is_not_silently_ignored(monkeypatch):
    monkeypatch.setattr(mod, 'create_session', lambda *a: pytest.fail('must validate before creating sessions'))
    with pytest.raises(ValueError, match='assistant'):
        mod.process_case('http://local', 'agent', {'id': 'x', 'history': [{'user': 'choose', 'assistant': 'curl'}]}, 2)


def test_report_preserves_execution_error_and_does_not_claim_accuracy(monkeypatch, tmp_path):
    import json
    import sys
    gold = tmp_path / 'gold.jsonl'
    gold.write_text('{"id":"x"}\n{"id":"y"}\n')
    output = tmp_path / 'report.json'
    def process(base, agent, case, timeout):
        if case['id'] == 'x':
            raise RuntimeError('offline')
        return {'id': 'y', 'passed': None, 'review_required': True, 'heuristic_match': True}
    monkeypatch.setattr(mod, 'process_case', process)
    monkeypatch.setattr(sys, 'argv', ['collect', '--goldset', str(gold), '--agent-id', 'isolated', '--output', str(output)])
    assert mod.main() == 2
    report = json.loads(output.read_text())
    assert len(report['results']) == 2
    assert report['execution_errors'] == 1
    assert report['accuracy'] is None and report['threshold_pass'] is None


def test_history_answers_are_saved(monkeypatch):
    monkeypatch.setattr(mod, 'create_session', lambda *a: 's')
    monkeypatch.setattr(mod, 'run_chat', lambda base, sid, prompt, timeout: {'content': 'live history', 'run_id': 'r'})
    row = mod.process_case('http://local', 'agent', {'id': 'x', 'history': [{'user': 'select curl'}]}, 2)
    assert row['history_runs'][0]['user'] == 'select curl'
    assert row['history_runs'][0]['run']['content'] == 'live history'
