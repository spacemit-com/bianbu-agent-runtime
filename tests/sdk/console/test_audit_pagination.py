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
let _securityAuditAttention = true, _securityAuditCategory = '';
let _securityAuditOutcome = '', _securityAuditRange = 'all';
let _securityOtherRequest = 1;
const escapeHtml = v => String(v).replaceAll('<', '&lt;');
const securityClockTime = value => String(value ?? '');
const requests = [];
const row = (seq, extra = {}) => ({seq, ts: '2026-10-23T09:00:00.000+00:00',
  title: '<script>', category_label: '想做什么', outcome_label: '已拒绝',
  outcome_group: 'blocked', severity: 'critical', severity_label: '需要处理',
  technical: {seq, op_type: 'check_permission', target: '<script>', result: 'deny'},
  ...extra});
let response = {records: [row(201)], snapshot_seq: 201, has_more: true,
                next_cursor: 102, summary: {total: 1, attention: 1,
                by_category: {identity: 0, decision: 1, confirmation: 0,
                              actor: 0, change: 0, anomaly: 0}}};
const modeCard = {innerHTML: ''};
const securityModeBehaviorCard = (tab, mode) => `mode:${tab}:${mode}`;
const securityPageHeader = ({title, actions}) => `header:${title}${actions || ''}`;
const securityPage = (tab, mode, header, body) => `${header}mode:${tab}:${mode}${body}`;
const securityKnownMode = () => '';
const securityRememberMode = () => {};
const securityInvoke = async (op, params) => {
  requests.push({op, params});
  if (op === 'agents.security-mode.get.v1') return {mode: 'safe'};
  return response;
};
const auditRequests = () => requests.filter(r => r.op === 'audit.view.v1');
eval(source.slice(source.indexOf('// AUDIT VIEW HELPERS'),
                  source.indexOf('async function loadSecurityOtherView(')));
(async () => {
  const target = {innerHTML: '', textContent: ''};
  await loadSecurityAuditPage('a', target, 1, true);
  assert.equal(auditRequests()[0].params.limit, 100);
  assert.equal(auditRequests()[0].params.snapshot_seq, undefined);
  assert.equal(auditRequests()[0].params.attention, true);
  assert.ok(target.innerHTML.includes('mode:audit:safe'));
  assert.ok(target.innerHTML.includes('&lt;script>'));
  assert.ok(target.innerHTML.includes('需要你关注'));
  assert.ok(target.innerHTML.includes('技术详情'));
  assert.ok(!target.innerHTML.includes('<table'));
  _securityAuditPage.history.push(0);
  _securityAuditPage.cursor = 102;
  response = {...response, records: [row(101)], has_more: false, next_cursor: null};
  await loadSecurityAuditPage('a', target, 1, false);
  assert.equal(auditRequests()[1].params.snapshot_seq, 201);
  assert.equal(auditRequests()[1].params.cursor, 102);
  assert.ok(target.innerHTML.includes('第 2 页'));
  _securityAuditPage.cursor = _securityAuditPage.history.pop();
  await loadSecurityAuditPage('a', target, 1, false);
  assert.equal(auditRequests()[2].params.cursor, 0);
  assert.equal(auditRequests()[2].params.snapshot_seq, 201);
  _securityAuditAttention = false;
  _securityAuditCategory = 'identity';
  _securityAuditOutcome = 'blocked';
  _securityAuditRange = '7d';
  await loadSecurityAuditPage('b', target, 1, true);
  assert.equal(auditRequests()[3].params.agent_id, 'b');
  assert.equal(auditRequests()[3].params.category, 'identity');
  assert.equal(auditRequests()[3].params.outcome_group, 'blocked');
  assert.equal(auditRequests()[3].params.attention, undefined);
  assert.match(auditRequests()[3].params.from, /\+00:00$/);
  assert.match(auditRequests()[3].params.to, /\+00:00$/);
  assert.equal(auditRequests()[3].params.snapshot_seq, undefined);
  assert.equal(_securityAuditPage.history.length, 0);
  const html = target.innerHTML;
  _securityOtherRequest = 2;
  await loadSecurityAuditPage('a', target, 1, false);
  assert.equal(target.innerHTML, html);
  response = {records: Array(101).fill({seq: 1})};
  await loadSecurityAuditPage('a', target, 2, true);
  assert.ok(target.innerHTML.includes('请更新'));
  assert.ok(!target.innerHTML.includes('security-audit-list'));
  assert.ok(!target.innerHTML.includes('<table'));
  assert.ok(target.innerHTML.includes('mode:audit:safe'));
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    subprocess.run(['node', '-e', script], cwd=ROOT, check=True, capture_output=True, text=True)
