"""Exercise the Console session controller with nonterminal Run progress."""
from pathlib import Path
import subprocess


def test_startup_progress_preserves_running_session():
    root = Path(__file__).resolve().parents[2]
    script = """
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = { window: {} };
vm.runInNewContext(fs.readFileSync('bianbu_agent/console/web/static/js/session-runtime.js', 'utf8'), context);
const runtime = new context.window.BianbuSessionRuntime.SessionRuntime();
runtime.updateSummaries([{ id: 's', running: true, active_run_id: 'r' }]);
runtime.observeEvent('s', { type: 'run/progress', run_id: 'r', data: { phase: 'starting_agent' } });
assert.equal(runtime.summary('s').running, true);
assert.equal(runtime.summary('s').active_run_id, 'r');
runtime.observeEvent('s', { type: 'run/completed', run_id: 'r', data: {} });
assert.equal(runtime.summary('s').running, false);
"""
    result = subprocess.run(['node', '-e', script], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_recovery_notice_preserves_history_and_terminal_content():
    root = Path(__file__).resolve().parents[2]
    script = """
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = { window: {} };
vm.runInNewContext(fs.readFileSync('bianbu_agent/console/web/static/js/session-runtime.js', 'utf8'), context);
const model = new context.window.BianbuSessionRuntime.SessionViewModel('s');
const e = (seq, type, data) => ({seq, type, data, run_id: 'r', time: seq});
const events = [e(1, 'assistant/text.delta', {text: 'before'}),
 e(2, 'run/progress', {phase: 'recovering', message: '继续', attempt: 1}),
 e(3, 'assistant/text.delta', {text: 'afterHello 🌏'}),
 e(4, 'run/progress', {phase: 'recovering', message: '再次继续', attempt: 2, discarded_text: 'Hello 🌏'}),
 e(5, 'run/failed', {content: 'beforeafter', error: {message: '恢复失败'}})];
model.installHistory(events.slice(0, -1));
assert.equal(model.snapshot().messages.filter(m => m.role === 'assistant').map(m => m.content).join(''), 'beforeafter');
model.installHistory(events);
let messages = model.snapshot().messages;
assert.equal(messages.filter(m => m.role === 'system').length, 2);
assert.equal(messages.filter(m => m.role === 'assistant').map(m => m.content).join(''), 'beforeafter');
assert.equal(messages[1].kind, 'model_recovery');
assert.equal(messages[1].message, '继续');
assert.equal(messages.at(-1).error.message, '恢复失败');
const before = JSON.stringify(messages);
model.installHistory(events.concat(events));
assert.equal(JSON.stringify(model.snapshot().messages), before);
"""
    result = subprocess.run(['node', '-e', script], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_terminal_run_closes_unfinished_tools_without_changing_other_runs():
    root = Path(__file__).resolve().parents[2]
    script = """
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const context = { window: {} };
vm.runInNewContext(fs.readFileSync('bianbu_agent/console/web/static/js/session-runtime.js', 'utf8'), context);
for (const terminal of ['failed', 'cancelled', 'completed']) {
 const model = new context.window.BianbuSessionRuntime.SessionViewModel('s');
 const e = (seq, type, data = {}, run_id = 'r') => ({seq, type, data, run_id, time: seq});
 model.installHistory([
  e(1, 'tool/call.started', {call_id: 'done'}),
  e(2, 'tool/call.completed', {call_id: 'done', status: 'success', result: 'ok'}),
  e(3, 'tool/call.started', {call_id: 'pending'}),
  e(4, 'tool/call.started', {call_id: 'pending'}, 'other'),
  e(5, 'run/' + terminal, {error: {code: 'security_denied', message: 'Denied'}}),
 ]);
 const messages = model.snapshot().messages;
 const own = messages.find(m => m.run_id === 'r');
 assert.equal(own.tool_calls[0].status, 'success');
 assert.equal(own.tool_calls[1].status, 'error');
 assert.equal(own.tool_calls[1].result, 'Denied');
 assert.equal(messages.find(m => m.run_id === 'other').tool_calls[0].status, 'running');
}
"""
    result = subprocess.run(['node', '-e', script], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
