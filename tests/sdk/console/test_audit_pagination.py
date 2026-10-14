"""Execute the built-in console's audit loader against paged responses."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]


def test_audit_pages_reset_filter_and_ignore_stale_responses():
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('bianbu_agent/console/web/static/js/console.js', 'utf8');
let _securityAuditPage = {cursor: 0, history: []};
let _securityAuditFilter = 'all', _securityOtherRequest = 1;
const escapeHtml = v => String(v).replaceAll('<', '&lt;');
const securityClockTime = value => String(value ?? '');
const requests = [];
let response = {records: [{seq: 201, target: '<script>'}], snapshot_seq: 201,
                has_more: true, next_cursor: 102};
const securityInvoke = async (op, params) => { requests.push({op, params}); return response; };
eval(source.slice(source.indexOf('async function loadSecurityAuditPage('),
                  source.indexOf('async function loadSecurityOtherView(')));
(async () => {
  const target = {innerHTML: '', textContent: ''};
  await loadSecurityAuditPage('a', target, 1, true);
  assert.equal(requests[0].params.limit, 100);
  assert.equal(requests[0].params.snapshot_seq, undefined);
  assert.ok(target.innerHTML.includes('&lt;script>'));
  _securityAuditPage.history.push(0);
  _securityAuditPage.cursor = 102;
  response = {...response, records: [{seq: 101}], has_more: false, next_cursor: null};
  await loadSecurityAuditPage('a', target, 1, false);
  assert.equal(requests[1].params.snapshot_seq, 201);
  assert.equal(requests[1].params.cursor, 102);
  assert.ok(target.innerHTML.includes('第 2 页'));
  _securityAuditPage.cursor = _securityAuditPage.history.pop();
  await loadSecurityAuditPage('a', target, 1, false);
  assert.equal(requests[2].params.cursor, 0);
  assert.equal(requests[2].params.snapshot_seq, 201);
  _securityAuditFilter = 'deny';
  await loadSecurityAuditPage('b', target, 1, true);
  assert.equal(requests[3].params.agent_id, 'b');
  assert.equal(requests[3].params.filter, 'deny');
  assert.equal(requests[3].params.snapshot_seq, undefined);
  assert.equal(_securityAuditPage.history.length, 0);
  const html = target.innerHTML;
  _securityOtherRequest = 2;
  await loadSecurityAuditPage('a', target, 1, false);
  assert.equal(target.innerHTML, html);
  response = {records: Array(101).fill({seq: 1})};
  await loadSecurityAuditPage('a', target, 2, true);
  assert.ok(target.innerHTML.includes('请更新'));
  assert.ok(!target.innerHTML.includes('<table'));
  assert.ok(requests.every(r => r.op === 'audit.query.v1'));
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    subprocess.run(['node', '-e', script], cwd=ROOT, check=True, capture_output=True, text=True)
