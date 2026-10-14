"""YOLO settings must not overwrite the preserved normal-mode policies."""
import re

import pytest
from playwright.async_api import async_playwright, expect
from tests.sdk.console.test_browser_contract import _running_console


def _security_stub(mode: str = 'normal') -> str:
    return f'''async () => {{
        securitySelectedAgent='acceptance';
        window.testMode={mode!r};
        window.securityCalls=[];
        securityInvoke=async (op, params={{}}) => {{
            securityCalls.push(op);
            if(op==='agents.security-mode.get.v1') return {{mode:testMode}};
            if(op==='agents.get.v1') return {{agent_id:'acceptance', capabilities:[{{tool:'terminal',policy:'ask'}}]}};
            if(op==='capabilities.catalog.v1') return {{capabilities:[
                {{tool:'terminal',policy:'ask',default_policy:'ask',framework:'runtime',summary:'运行命令'}},
                {{tool:'read_file',policy:'ask',default_policy:'ask',framework:'runtime',summary:'读取文件'}}
            ]}};
            if(op==='permissions.get.v1') return {{agent_id:'acceptance',read:{{include:[],exclude:[]}},write:{{include:[],exclude:[]}},network:{{mode:'allow_all',domains:[]}},syscall:{{agent_additions:[]}},execution_mode:{{mode:'sandbox',reasons:['read']}}}};
            if(op==='dlp.matrix.v1') return {{rows:[{{pii_type:'EMAIL',action:'mask'}}]}};
            if(op==='dlp.stats.v1') return {{total:0}};
            return {{}};
        }};
        await loadSecurityOtherView('capabilities');
    }}'''


@pytest.mark.asyncio
async def test_yolo_security_controls_preserve_normal_policies(tmp_path):
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
                    page.locator('#security-permission-read-exclude')
                ).to_be_visible()

                # normal：策略可改、DLP 动作可改，且已保存的 mask 仍在。
                await page.evaluate(_security_stub('normal'))
                await expect(
                    page.locator('.security-tool-policy').first
                ).to_be_enabled()
                await page.evaluate("loadSecurityOtherView('dlp')")
                await expect(page.locator('.security-dlp-action')).to_have_value('mask')
                await expect(page.locator('#security-dlp-scan')).to_be_enabled()
            finally:
                await browser.close()
