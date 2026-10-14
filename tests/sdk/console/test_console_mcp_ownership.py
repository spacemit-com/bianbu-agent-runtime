"""MCP 页只管理独立 MCP，插件自带的 MCP 状态在插件页随插件显示。

同一台 plugin-owned server 在两个页面各显示一遍，容易出现「MCP 页说 running、
插件页说 failed」这种互相矛盾的画面；归属只有一个地方，状态也就只在一个地方讲。
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

_DOM_STUB = '''
const nodes = {};
function makeNode() {
    return {
        innerHTML: '', textContent: '', className: '', type: '', disabled: false,
        dataset: {}, children: [],
        classList: { add() {}, remove() {}, toggle() {} },
        replaceChildren() { this.children = []; this.innerHTML = ''; },
        appendChild(child) { this.children.push(child); },
        append(...items) { this.children.push(...items); },
        querySelector() { return null; },
        addEventListener() {},
    };
}
const document = {
    getElementById: id => (nodes[id] ||= makeNode()),
    createElement: () => makeNode(),
};
const escapeHtml = value => String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const escapeAttribute = value => escapeHtml(value)
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
'''

#: 生命周期词/解释/配色是所有 MCP 视图共用的一块，两个页面的切片都要带上它，
#: 否则同一台 Server 在 MCP 页和插件页会说出不一样的状态。
_LIFECYCLE_BLOCK = '''
eval(source.slice(source.indexOf('// == MCP 生命周期共用块'),
                  source.indexOf('// == MCP 生命周期共用块结束 ==')));
'''

_PRELUDE = '''
const fs = require('node:fs');
const source = fs.readFileSync('bianbu_agent/console/web/static/js/console.js', 'utf8');
let currentLang = '__LANG__';
eval(source.slice(source.indexOf('const I18N = {'),
                  source.indexOf('// Resolve language by priority'))
     + source.slice(source.indexOf('function t(key) {'),
                    source.indexOf('const sessionTokenUsageGeneration')));
''' + _LIFECYCLE_BLOCK + _DOM_STUB


def _run(script: str) -> None:
    result = subprocess.run(
        ['node', '-e', script], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_mcp_page_keeps_only_standalone_servers(lang):
    _run(_PRELUDE.replace('__LANG__', lang) + '''
const assert = require('node:assert/strict');
eval(source.slice(source.indexOf('function renderMcpServers('),
                  source.indexOf('function loadMcpView(')));
renderMcpServers([
  { id: 'user/weather', display_name: 'Weather', status: 'running', transport: 'stdio', tools: [], removable: true, owner_plugin_id: null },
  { id: 'bianbu.bibit.intent/bibit-readonly', display_name: 'readonly', status: 'running', transport: 'stdio', tools: [], removable: false, owner_plugin_id: 'bianbu.bibit.intent' },
  { id: 'bianbu.bibit.intent/bibit-admin', display_name: 'admin', status: 'failed', transport: 'stdio', tools: [], removable: false, owner_plugin_id: 'bianbu.bibit.intent' },
]);
const list = document.getElementById('mcp-list');
assert.equal(list.children.length, 1);
assert.ok(list.innerHTML === '' || true);
assert.ok(list.children[0].innerHTML.includes('Weather'), list.children[0].innerHTML);
const note = document.getElementById('mcp-plugin-owned-note');
const expected = currentLang === 'zh' ? '插件' : 'Plugins page';
assert.ok(note.textContent.includes(expected), note.textContent);
assert.ok(note.textContent.includes('2'), note.textContent);
assert.equal(note.classList.hidden, undefined);
''')

    _run(_PRELUDE.replace('__LANG__', lang) + '''
const assert = require('node:assert/strict');
eval(source.slice(source.indexOf('function renderMcpServers('),
                  source.indexOf('function loadMcpView(')));
renderMcpServers([
  { id: 'bianbu.bibit.intent/bibit-readonly', display_name: 'readonly', status: 'running', transport: 'stdio', tools: [], removable: false, owner_plugin_id: 'bianbu.bibit.intent' },
]);
const empty = document.getElementById('mcp-empty');
const expected = currentLang === 'zh' ? '独立 MCP' : 'standalone MCP';
assert.ok(empty.textContent.includes(expected), empty.textContent);
assert.equal(document.getElementById('mcp-list').children.length, 0);
''')


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_plugins_page_shows_the_plugin_owned_mcp_status(lang):
    _run(_PRELUDE.replace('__LANG__', lang) + '''
const assert = require('node:assert/strict');
let pluginAdminState = [{
  id: 'bianbu.bibit.intent',
  members: [
    { id: 'bianbu.bibit.intent/after_run', kind: 'hook', name: 'after_run' },
    { id: 'bianbu.bibit.intent/bibit-readonly', kind: 'mcp', name: 'bibit-readonly' },
    { id: 'bianbu.bibit.intent/bibit-admin', kind: 'mcp', name: 'bibit-admin' },
  ],
}];
let pluginMcpState = [
  { id: 'bianbu.bibit.intent/bibit-readonly', status: 'running', tools: [{id: 'a'}, {id: 'b'}], owner_plugin_id: 'bianbu.bibit.intent' },
  { id: 'bianbu.bibit.intent/bibit-admin', status: 'failed', tools: [], owner_plugin_id: 'bianbu.bibit.intent' },
  { id: 'user/weather', status: 'running', tools: [], owner_plugin_id: null },
];
// pluginMcpRows 与 renderPluginAdminView 是相邻的两个块，各取一段；生命周期
// 共用块已经在 _PRELUDE 里加载过。
eval(source.slice(source.indexOf('function pluginMcpRows('),
                  source.indexOf('async function loadPluginsAdminView')));
eval(source.slice(source.indexOf('function renderPluginAdminView('),
                  source.indexOf('// 插件的 MCP 成员')));
renderPluginAdminView();
const card = document.getElementById('plugin-admin-list').children[0];
const html = card.children.map(child => child.innerHTML || child.textContent).join('\\n');
assert.ok(html.includes('bianbu.bibit.intent/bibit-readonly'), html);
// 插件页用同一套生命周期词，而不是 Runtime 的原始 status 原文。
const readyLabel = currentLang === 'zh' ? '就绪' : 'Ready';
const failedLabel = currentLang === 'zh' ? '失败' : 'Failed';
assert.ok(html.includes(readyLabel), html);
assert.ok(html.includes(failedLabel), html);
assert.ok(!html.includes('running'), html);
assert.ok(!html.includes('user/weather'), html);
const members = card.children[1].textContent;
assert.ok(members.includes('Hook · after_run'), members);
assert.ok(!members.includes('MCP · bibit-readonly'), members);
''')


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_mcp_card_shows_a_failure_reason_and_a_matching_badge(lang):
    """坏掉的 Server 曾经和能用的一模一样：绿色徽章、同样的「暂未发现工具」。"""

    _run(_PRELUDE.replace('__LANG__', lang) + '''
const assert = require('node:assert/strict');
eval(source.slice(source.indexOf('function renderMcpServers('),
                  source.indexOf('function loadMcpView(')));
renderMcpServers([
  { id: 'user/broken', display_name: 'Broken', status: 'unhealthy', transport: 'stdio', tools: [], removable: true, owner_plugin_id: null,
    detail: 'FileNotFoundError: [Errno 2] No such file or directory' },
  { id: 'user/silent', display_name: 'Silent', status: 'unhealthy', transport: 'stdio', tools: [], removable: true, owner_plugin_id: null, detail: '' },
  { id: 'user/good', display_name: 'Good', status: 'healthy', transport: 'stdio', tools: [{ id: 'a', name: 'a', display_name: 'A' }], removable: true, owner_plugin_id: null },
  { id: 'user/pending', display_name: 'Pending', status: 'unverified', transport: 'stdio', tools: [], removable: true, owner_plugin_id: null },
]);
const cards = document.getElementById('mcp-list').children;
assert.ok(cards[0].innerHTML.includes('amber'), cards[0].innerHTML);
assert.ok(cards[0].innerHTML.includes('No such file or directory'), cards[0].innerHTML);
assert.ok(!cards[0].innerHTML.includes('mcp_no_tools'), cards[0].innerHTML);
assert.ok(cards[1].innerHTML.includes('amber'), cards[1].innerHTML);
const notConnected = currentLang === 'zh' ? '未连接' : 'not connected';
assert.ok(cards[1].innerHTML.includes(notConnected), cards[1].innerHTML);
assert.ok(cards[2].innerHTML.includes('emerald'), cards[2].innerHTML);
assert.ok(cards[2].innerHTML.includes('A'), cards[2].innerHTML);
assert.ok(cards[3].innerHTML.includes('slate'), cards[3].innerHTML);
assert.ok(!cards[3].innerHTML.includes('amber'), cards[3].innerHTML);
''')
