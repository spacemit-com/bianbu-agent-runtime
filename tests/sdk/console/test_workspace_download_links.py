"""Render real Markdown with the shipped renderer and Markdown-It implementation."""
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[3]


def test_markdown_workspace_html_links_become_attachment_downloads():
    script = r'''
const fs = require('fs');
const source = fs.readFileSync('bianbu_agent/console/web/static/js/console.js', 'utf8');
const markdownit = require('./bianbu_agent/console/web/static/vendor/markdown-it/markdown-it.min.js');
const window = {markdownit};
const PUBLIC_API_BASE = '/api/v1';
const getHljs = () => ({ getLanguage: () => false, highlightAuto: x => ({value:x}) });
const begin = source.indexOf('function workspaceHtmlDownloadUrl(');
if (begin < 0) throw new Error('workspace download link mapper is missing');
eval(source.slice(begin, source.indexOf('const md = createMd();', begin)));
const md = createMd();
console.log(JSON.stringify([
  md.render('[下载报告](/home/agent/workspace/AI%20资讯/report.html)'),
  md.render('[报告](./reports/index.htm)'),
  md.render('[报告](workspace/reports/index.html)'),
  md.render('[官方来源](https://example.com/news/report.html)'),
  md.render('[越界](/home/agent/workspace/../secret.html)'),
  md.render('[符号混淆](/home/agent/workspace/%2E%2E/secret.html)'),
  md.render('[本机文件](/etc/report.html)'),
  md.render('`/home/agent/workspace/bibit-ai-news-test/report.html`'),
  md.render('`workspace/bibit-ai-news-test/report.html`'),
  md.render('`cat /home/agent/workspace/report.html`'),
  md.render('`report.html`'),
  md.render('```text\n/home/agent/workspace/report.html\n```'),
  md.render('[`/home/agent/workspace/report.html`](https://example.com/)'),
]));
'''
    result = subprocess.run(['node', '-e', script], cwd=ROOT, check=True, text=True, capture_output=True)
    rendered = json.loads(result.stdout)
    assert '/api/v1/workspace/download?path=AI%20%E8%B5%84%E8%AE%AF%2Freport.html' in rendered[0]
    assert 'download="report.html"' in rendered[0]
    assert '/api/v1/workspace/download?path=reports%2Findex.htm' in rendered[1]
    assert '/api/v1/workspace/download?path=reports%2Findex.html' in rendered[2]
    assert 'href="https://example.com/news/report.html"' in rendered[3]
    assert all('/api/v1/workspace/download' not in value for value in rendered[3:7])
    assert '/api/v1/workspace/download?path=bibit-ai-news-test%2Freport.html' in rendered[7]
    assert '<code>/home/agent/workspace/bibit-ai-news-test/report.html</code></a>' in rendered[7]
    assert '/api/v1/workspace/download?path=bibit-ai-news-test%2Freport.html' in rendered[8]
    assert all('/api/v1/workspace/download' not in value for value in rendered[9:])


import pytest


@pytest.mark.asyncio
async def test_ordinary_reply_inline_html_path_downloads_in_real_console(tmp_path):
    from playwright.async_api import async_playwright, expect
    from tests.sdk.console.test_browser_contract import _running_console, _create_session

    async with _running_console(tmp_path) as (base_url, home):
        report = home.root / 'workspace' / 'news-report' / 'report.html'
        report.parent.mkdir(parents=True, exist_ok=True)
        content = b'<!doctype html><html><script>window.download_script_ran=true;</script><h1>AI news</h1></html>'
        report.write_bytes(content)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(accept_downloads=True)
                sid = (await _create_session(page, base_url))['id']
                await page.add_init_script("localStorage.setItem('bianbu_session_id', " + json.dumps(sid) + ")")
                await page.goto(base_url + '/chat', wait_until='domcontentloaded')
                await page.locator('#chat-input').fill('报告：`/home/agent/workspace/news-report/report.html`')
                await page.locator('#send-btn').click()
                link = page.locator('.bot-message-group a[download="report.html"]').last
                await expect(link).to_be_visible()
                async with page.expect_download() as download_info:
                    await link.click()
                download = await download_info.value
                assert download.suggested_filename == 'report.html'
                saved = tmp_path / 'downloaded.html'
                await download.save_as(saved)
                assert saved.read_bytes() == content
                assert await page.evaluate('typeof window.download_script_ran') == 'undefined'
                await page.reload(wait_until='domcontentloaded')
                await expect(page.locator('.bot-message-group a[download="report.html"]').last).to_be_visible()
            finally:
                await browser.close()
