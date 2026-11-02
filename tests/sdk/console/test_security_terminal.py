"""Security terminal errors remain visible beside interrupted tool history."""
import pytest
from playwright.async_api import async_playwright, expect
from tests.sdk.console.test_browser_contract import _running_console


@pytest.mark.asyncio
async def test_security_error_survives_incremental_tool_bubble_update(tmp_path):
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(base_url + '/chat', wait_until='domcontentloaded')
                await page.wait_for_function('() => agentsLoaded')
                await page.evaluate('''() => {
                    const message = {steps: [{type: 'tool', tool_call_id: 't', name: 'read', status: 'running'}]};
                    const bubble = createBotMessageEl('', new Date(), 'r', message);
                    messagesDiv.appendChild(bubble);
                    message.steps[0].status = 'error';
                    message.error = {code: 'security_denied', message: 'Denied', details: {
                        tool: 'read_file<img src=x onerror=alert(1)>', policy: 'deny'}};
                    patchBotMessageElement(bubble, message, new Date());
                    patchBotMessageElement(bubble, message, new Date());
                }''')
                await expect(page.locator('.agent-tool-step.tool-failed')).to_have_count(1)
                await expect(page.locator('.agent-tool-step.tool-streaming')).to_have_count(0)
                await expect(page.locator('.security-denied-group')).to_have_count(1)
                await expect(page.locator('.security-denied-group')).to_contain_text('read_file')
                await expect(page.locator('.security-denied-group')).to_contain_text('deny')
                await expect(page.locator('.security-denied-group img[src="x"]')).to_have_count(0)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_unregister_action_confirms_and_refreshes(tmp_path):
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(base_url + '/chat', wait_until='domcontentloaded')
                await page.wait_for_function('() => agentsLoaded')
                await page.evaluate('''async () => {
                    window.unregisterCalls=[];
                    window.listCalls=0;
                    securityInvoke = async (op, params={}) => {
                        if(op === 'agents.list.v1') { listCalls += 1; return {agents:[{agent_id:'acceptance',name:'Acceptance'}]}; }
                        if(op === 'agents.unregister.v1') unregisterCalls.push(params.agent_id);
                        return {ok:true};
                    };
                    window.pauseAlerts=[]; window.alert=text=>pauseAlerts.push(text);
                    window.confirm=text=>{ pauseAlerts.push(text); return true; };
                    await loadSecurityOtherView('agents');
                    document.querySelector('.security-agent-unregister').click();
                }''')
                await page.wait_for_function('() => pauseAlerts.length > 0')
                assert await page.evaluate('unregisterCalls') == ['acceptance']
                assert await page.evaluate('listCalls') == 2
                assert '注销' in (await page.evaluate('pauseAlerts[0]'))
                assert '不可撤销' in (await page.evaluate('pauseAlerts[0]'))
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_tool_policy_change_persists_and_refreshes(tmp_path):
    """能力管理页每个工具一条策略，改动走 capabilities.update.v1。"""
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(base_url + '/chat', wait_until='domcontentloaded')
                await page.wait_for_function('() => agentsLoaded')
                await page.evaluate('''() => {
                    window.policyCalls=[];
                    securitySelectedAgent='acceptance';
                    securityInvoke = async (op, params={}) => {
                        if(op==='agents.list.v1') return {agents:[{agent_id:'acceptance',name:'Acceptance'}]};
                        if(op==='capabilities.update.v1') policyCalls.push([params.tool, params.policy]);
                        if(op==='agents.security-mode.get.v1') return {mode:'safe'};
                        if(op==='agents.get.v1') return {agent_id:'acceptance', capabilities:[{tool:'terminal',policy:'ask'}]};
                        if(op==='capabilities.catalog.v1') return {capabilities:[
                            {tool:'terminal',policy:'ask',default_policy:'ask',framework:'runtime',summary:'运行命令'},
                            {tool:'read_file',policy:'ask',default_policy:'ask',framework:'runtime',summary:'读取文件'}
                        ]};
                        if(op==='permissions.get.v1') return {agent_id:'acceptance',read:[],write:[],network:{mode:'allow_all',domains:[]},syscall:{mandatory:[],agent_additions:[]},custom:{}};
                        return {};
                    };
                }''')
                await page.locator('[data-view="security"]').click()
                await page.locator('[data-security-tab="capabilities"]').click()
                await page.wait_for_selector(".security-tool-policy[data-tool=\"terminal\"]")
                terminal = page.locator('.security-tool-policy[data-tool="terminal"]')
                await expect(terminal).to_have_value('ask')
                await terminal.select_option('allow')
                await page.wait_for_function('() => policyCalls.length > 0')
                assert await page.evaluate('policyCalls') == [['terminal', 'allow']]
            finally:
                await browser.close()
