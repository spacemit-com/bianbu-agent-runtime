"""API 密钥输入栏必须说明「已配置，输入新值可替换」，运行时身份要显示在监控面板。

服务端从不把已保存的密钥回传给浏览器（``ProviderView.api_key_masked`` 只有掩码），
所以输入框永远是空的——placeholder 是唯一能告诉用户「这里已经有一份凭据、输入新值
即替换」的地方。契约参考 DeepSeek Harness 的 ``keyStored``。
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _run(script: str) -> None:
    result = subprocess.run(
        ['node', '-e', script], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# 只取真实的 I18N 字典与 t()，语言由测试直接指定，避免整段 i18n 初始化里的浏览器依赖。
# I18N 和 t() 必须一次 eval：const I18N 只活在那次 eval 的词法作用域里，而 t()
# 闭包引用它。currentLang 留在外面，测试可以在调用之间切换语言。
_I18N_PRELUDE = '''
const fs = require('node:fs');
const source = fs.readFileSync('bianbu_agent/console/web/static/js/console.js', 'utf8');
let currentLang = '__LANG__';
eval(source.slice(source.indexOf('const I18N = {'),
                  source.indexOf('// Resolve language by priority'))
     + source.slice(source.indexOf('function t(key) {'),
                    source.indexOf('const sessionTokenUsageGeneration')));
'''


def _prelude(lang: str) -> str:
    return _I18N_PRELUDE.replace('__LANG__', lang)


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_provider_key_placeholder_reports_an_existing_credential(lang):
    _run(_prelude(lang) + '''
const assert = require('node:assert/strict');
const input = { placeholder: '' };
const document = { getElementById: id => id === 'custom-provider-key' ? input : null };
let editingProviderRecord = null;
eval(source.slice(source.indexOf('function applyProviderKeyHint()'),
                  source.indexOf('function openProviderModal(')));
const configured = currentLang === 'zh' ? '已配置 — 输入新值可替换' : 'Configured — enter a new value to replace';
const missing = currentLang === 'zh' ? '输入 API 密钥' : 'Enter API key';
editingProviderRecord = null;
applyProviderKeyHint();
assert.equal(input.placeholder, missing);
editingProviderRecord = { api_key_masked: 'sk-1234...' };
applyProviderKeyHint();
assert.equal(input.placeholder, configured);
editingProviderRecord = { configured: true };
applyProviderKeyHint();
assert.equal(input.placeholder, configured);
editingProviderRecord = { api_key_masked: '', configured: false };
applyProviderKeyHint();
assert.equal(input.placeholder, missing);
''')


@pytest.mark.parametrize('lang', ['zh', 'en'])
def test_runtime_status_panel_names_the_run_identity_and_data_dir(lang):
    """系统级服务整机一份，面板要说清这份 Runtime 服务的是哪个账号的目录。"""
    _run(_prelude(lang) + '''
const assert = require('node:assert/strict');
const escapeHtml = value => String(value ?? '').replaceAll('<', '&lt;').replaceAll('&', '&amp;');
const elements = {};
const document = { getElementById: id => (elements[id] ||= { textContent: '', innerHTML: '', className: '' }) };
const agentTopStatus = () => ({ outerHTML: '' });
let runtimeStatusPayload = null;
eval(source.slice(source.indexOf('function renderRuntimeStatus(payload)'),
                  source.indexOf('async function loadRuntimeStatusView')));
renderRuntimeStatus({
  worker_count: 0, workers: [],
  control: { pid: 41, state: 'running' },
  runtime: { status: 'AGENT_READY', service_user: 'bianbu', data_dir: '/home/bianbu/.bianbu-agent' },
});
const html = elements['runtime-status-control'].innerHTML;
const userLabel = currentLang === 'zh' ? '运行身份' : 'Runtime user';
const dirLabel = currentLang === 'zh' ? '数据目录' : 'Data directory';
assert.ok(html.includes(userLabel), html);
assert.ok(html.includes(dirLabel), html);
assert.ok(html.includes('bianbu'), html);
assert.ok(html.includes('/home/bianbu/.bianbu-agent'), html);
// 语言切换会重放最后一次 status，面板不能停留在旧语言。
currentLang = currentLang === 'zh' ? 'en' : 'zh';
renderRuntimeStatus(runtimeStatusPayload);
const switched = elements['runtime-status-control'].innerHTML;
assert.ok(switched.includes(currentLang === 'zh' ? '运行身份' : 'Runtime user'), switched);
''')
