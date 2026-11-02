"""YOLO settings must not overwrite the preserved safe-mode policies."""
import re

import pytest
from playwright.async_api import async_playwright, expect
from tests.sdk.console.test_browser_contract import _running_console


def _security_stub(mode: str = 'safe') -> str:
    return f'''async () => {{
        securitySelectedAgent='acceptance';
        window.testMode={mode!r};
        window.securityCalls=[];
        securityInvoke=async (op, params={{}}) => {{
            securityCalls.push(op);
            if(op==='agents.list.v1') return {{agents:[{{agent_id:'acceptance',name:'Acceptance',status:'active'}}]}};
            if(op==='agents.security-mode.get.v1') return {{mode:testMode}};
            if(op==='agents.get.v1') return {{agent_id:'acceptance', capabilities:[{{tool:'terminal',policy:'ask'}}]}};
            if(op==='capabilities.catalog.v1') return {{capabilities:[
                {{tool:'terminal',policy:'ask',default_policy:'ask',framework:'runtime',summary:'运行命令'}},
                {{tool:'read_file',policy:'ask',default_policy:'ask',framework:'runtime',summary:'读取文件'}}
            ]}};
            if(op==='permissions.get.v1') return {{agent_id:'acceptance',read:[],write:[],network:{{mode:'allow_all',domains:[]}},syscall:{{agent_additions:[]}},execution_mode:{{mode:'sandbox',reasons:['read']}}}};
            if(op==='dlp.matrix.v1') return {{rows:[{{pii_type:'EMAIL',action:'mask'}}]}};
            if(op==='dlp.stats.v1') return {{total:0}};
            if(op==='audit.query.v1') return {{records:[],snapshot_seq:0,has_more:false,next_cursor:null}};
            return {{}};
        }};
        await loadSecurityOtherView('capabilities');
    }}'''


@pytest.mark.asyncio
async def test_yolo_security_controls_preserve_safe_policies(tmp_path):
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(base_url + '/chat', wait_until='domcontentloaded')
                await page.wait_for_function('() => agentsLoaded')

                # YOLO：能力管理页的工具策略不可编辑，DLP 动作保留但不执行。
                await page.evaluate(_security_stub('yolo'))
                await expect(
                    page.locator('.security-tool-policy').first
                ).to_be_disabled()
                await page.evaluate("loadSecurityOtherView('dlp')")
                for selector in ('.security-dlp-action', '.security-dlp-reset',
                                 '#security-dlp-text', '#security-dlp-scan'):
                    await expect(page.locator(selector)).to_be_disabled()

                # 安全权限设置独立成页，与 YOLO 无关，始终可保存。
                await page.locator('[data-view="security"]').click()
                await page.locator('[data-security-tab="permissions"]').click()
                await expect(
                    page.locator('#security-permission-save')
                ).to_be_enabled()
                await expect(
                    page.locator('#security-permission-read')
                ).to_be_visible()
                await expect(
                    page.locator('#security-permission-write')
                ).to_be_visible()

                # safe：策略可改、DLP 动作可改，且已保存的 mask 仍在。
                await page.evaluate(_security_stub('safe'))
                await expect(
                    page.locator('.security-tool-policy').first
                ).to_be_enabled()
                await page.evaluate("loadSecurityOtherView('dlp')")
                await expect(page.locator('.security-dlp-action')).to_have_value('mask')
                await expect(page.locator('#security-dlp-scan')).to_be_enabled()
                # DLP 只有两种选择：mask 与 allow（不脱敏）。
                option_values = await page.locator(
                    '.security-dlp-action option'
                ).evaluate_all('(nodes) => nodes.map((n) => n.value)')
                assert option_values == ['mask', 'allow']
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_security_pages_explain_modes_without_switch_controls(tmp_path):
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(base_url + '/chat', wait_until='domcontentloaded')
                await page.wait_for_function('() => agentsLoaded')
                await page.evaluate(_security_stub('yolo'))
                await page.locator('[data-view="security"]').click()
                await page.locator('[data-security-tab="capabilities"]').click()
                await expect(page.locator('.security-tool-policy').first).to_be_visible()

                # 能力管理只读展示当前模式，切换按钮已移除。
                await expect(page.locator('#security-capability-mode')).to_have_count(0)
                await expect(page.locator('#security-capability-mode-toggle')).to_have_count(0)
                await expect(
                    page.locator('[data-security-page="capabilities"] .security-page-header')
                ).to_have_count(1)
                card = page.locator('.security-mode-behavior')
                await expect(card).to_have_count(1)
                await expect(card).to_contain_text('安全模式 · sandbox')
                await expect(card).to_contain_text('正常模式 · YOLO host')
                await expect(card).to_contain_text('只读展示')
                await expect(card).to_contain_text('当前')

                # 其余页面也各自说明两模式差异。
                checks = (
                    ('permissions', '#security-panel-permissions'),
                    ('syscalls', '#security-panel-syscalls'),
                    ('dlp', '#security-panel-other'),
                    ('audit', '#security-panel-other'),
                )
                for tab, panel in checks:
                    await page.locator(f'[data-security-tab="{tab}"]').click()
                    await expect(page.locator(f'{panel} .security-mode-behavior')).to_have_count(1)
                    await expect(page.locator('.security-mode-behavior')).to_have_count(1)
                    await expect(
                        page.locator(f'[data-security-page="{tab}"] .security-page-header')
                    ).to_have_count(1)

                await page.evaluate('syncSecuritySyscallPanel("agents"); loadSecurityOtherView("agents")')
                # Agent 页可以切换模式；能力管理页只读。
                await expect(page.locator('.security-agent-mode')).to_have_count(1)
                await expect(page.locator('.security-agent-mode')).to_have_value('yolo')
                await expect(page.locator('.security-agent-mode-badge')).to_have_count(0)
                await expect(page.locator('.security-mode-behavior')).to_have_count(1)
                # 所有页面共用同一套页头结构。
                await expect(page.locator('[data-security-page="agents"] .security-page-header')).to_have_count(1)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_network_scope_uses_explicit_modes_and_validation(tmp_path):
    """The network scope must keep explicit modes instead of inferring one."""

    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(base_url + '/chat', wait_until='domcontentloaded')
                await page.wait_for_function('() => agentsLoaded')
                await page.evaluate('''async () => {
                    securitySelectedAgent = 'acceptance';
                    window.securityCalls = [];
                    const settings = {
                        agent_id: 'acceptance',
                        read: [],
                        write: [],
                        network: {mode: 'deny_all', domains: []},
                        syscall: {agent_additions: []},
                        execution_mode: {mode: 'sandbox', reasons: ['read']},
                    };
                    securityInvoke = async (op, params = {}) => {
                        securityCalls.push([op, params]);
                        if (op === 'agents.list.v1') return {agents: [{agent_id: 'acceptance', name: 'Acceptance', status: 'active'}]};
                        if (op === 'agents.get.v1') return {agent_id: 'acceptance', capabilities: []};
                        if (op === 'permissions.get.v1') return settings;
                        if (op === 'permissions.boundary.v1') return {
                            agent_root: '/home/agent', read_only: [], writable: [],
                            read_only_overrides: [], invisible: [],
                            network: {mode: 'deny_all'}, syscall_filter: true,
                        };
                        if (op === 'agents.security-mode.get.v1') return {mode: 'safe'};
                        return {};
                    };
                }''')
                await page.locator('[data-view="security"]').click()
                await page.locator('[data-security-tab="permissions"]').click()

                deny = page.locator(
                    'input[name="security-permission-network-mode"][value="deny_all"]'
                )
                await expect(deny).to_be_checked()
                await expect(
                    page.locator('#security-permission-network-domains-field')
                ).to_be_hidden()
                await expect(
                    page.locator('#security-permission-network-preview')
                ).to_contain_text('全部拒绝 → 全部拒绝')
                await page.locator('#security-permission-save').click()
                await page.wait_for_function(
                    """() => securityCalls.some(([op, params]) =>
                        op === 'permissions.set-group.v1'
                        && params.group === 'network'
                        && params.mode === 'deny_all')"""
                )
                deny_call = await page.evaluate(
                    """() => securityCalls.find(([op, params]) =>
                        op === 'permissions.set-group.v1'
                        && params.group === 'network')?.[1]"""
                )
                assert deny_call == {
                    'agent_id': 'acceptance',
                    'group': 'network',
                    'mode': 'deny_all',
                    'domains': [],
                }
                await page.evaluate('() => { securityCalls.length = 0; }')

                await page.locator(
                    'input[name="security-permission-network-mode"][value="whitelist"]'
                ).check()
                await expect(
                    page.locator('#security-permission-network-domains-field')
                ).to_be_visible()
                await page.locator('#security-permission-save').click()
                await expect(
                    page.locator('#security-permission-status')
                ).to_have_text('白名单模式至少填写一个域名。')

                await page.locator('#security-permission-network').fill(
                    'https://api.example.com/path'
                )
                await page.locator('#security-permission-save').click()
                await expect(
                    page.locator('#security-permission-status')
                ).to_contain_text('域名不能包含协议、路径或空格')

                await page.locator('#security-permission-network').fill(
                    'api.example.com\n*.example.org'
                )
                await expect(
                    page.locator('#security-permission-network-preview')
                ).to_contain_text('白名单 (2)')
                await page.locator('#security-permission-save').click()
                await page.wait_for_function(
                    """() => securityCalls.some(([op, params]) =>
                        op === 'permissions.set-group.v1'
                        && params.group === 'network'
                        && params.mode === 'whitelist'
                        && params.domains.length === 2)"""
                )
                network_call = await page.evaluate(
                    """() => securityCalls.find(([op, params]) =>
                        op === 'permissions.set-group.v1'
                        && params.group === 'network')?.[1]"""
                )
                assert network_call == {
                    'agent_id': 'acceptance',
                    'group': 'network',
                    'mode': 'whitelist',
                    'domains': ['api.example.com', '*.example.org'],
                }
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_boundary_panel_shows_concrete_host_and_sandbox_paths(tmp_path):
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(base_url + '/chat', wait_until='domcontentloaded')
                await page.wait_for_function('() => agentsLoaded')
                html = await page.evaluate("""() => securityBoundaryPanel({
                    mode: 'sandbox',
                    agent_root: '/home/bianbu/.bianbu-agent',
                    path_mapping: {
                        sandbox: '/home/agent',
                        host: '/home/bianbu/.bianbu-agent',
                    },
                    read_only: [
                        {path: '/usr', source: 'system'},
                        {path: '<agent-root>', source: 'baseline'},
                    ],
                    writable: [
                        {path: '<agent-root>/workspace', source: 'baseline'},
                    ],
                    invisible: [{path: '/root', source: 'baseline'}],
                    network: {mode: 'allow_all'},
                    syscall_filter: false,
                })""")
                assert '/home/bianbu/.bianbu-agent/workspace' in html
                assert '沙箱内 /home/agent/workspace' in html
                assert '专家数据根' not in html
            finally:
                await browser.close()

@pytest.mark.asyncio
async def test_boundary_panel_does_not_invent_sandbox_mapping_for_dsh(tmp_path):
    """dsh 自带沙箱：宿主路径原样可读，界面不能套用 Worker 的 /home/agent 映射。"""
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(base_url + '/chat', wait_until='domcontentloaded')
                await page.wait_for_function('() => agentsLoaded')
                html = await page.evaluate("""() => securityBoundaryPanel({
                    framework: 'harness',
                    mode: 'sandbox',
                    agent_root: '/home/bianbu/.dsh',
                    path_mapping: null,
                    read_only: [{path: '/', source: 'system'}],
                    writable: [
                        {path: '/home/bianbu/Documents/work', source: 'declared'},
                        {path: '/tmp', source: 'baseline'},
                    ],
                    invisible: [],
                    network: {mode: 'allow_all', managed: false},
                    syscall_filter: false,
                    note: 'dsh 沙箱只约束文件写入',
                })""")
                assert '路径映射' not in html
                assert '/home/agent' not in html
                assert '不由本框架的沙箱接管' in html
                assert '/home/bianbu/.dsh' in html
            finally:
                await browser.close()
