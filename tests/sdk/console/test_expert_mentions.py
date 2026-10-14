from pathlib import Path
import asyncio
import re

import pytest
from playwright.async_api import async_playwright, expect

from bianbu_agent.agents import AgentSpec
from tests.sdk.console.test_browser_contract import (
    _running_console, _create_session, _list_sessions,
)


@pytest.mark.asyncio
async def test_mention_arrows_choose_candidate_without_leaving_composer(tmp_path):
    writer = AgentSpec(id="writer", name="Writer")
    unavailable = AgentSpec(id="middle", name="Unavailable")
    async with _running_console(tmp_path, (writer, unavailable)) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => agentsLoaded")
                await page.evaluate("createSessionForAgent('default'); closeChatAgentMenu()")
                await page.evaluate("agentsState.find(agent => agent.id === 'middle').available = false; inputHistory.push('old input')")
                field = page.locator("#chat-input")
                await field.fill("hello @")
                options = page.locator("#chat-agent-menu .chat-agent-option:not(:disabled)")
                await expect(options).to_have_count(2)
                await expect(page.locator('#chat-agent-menu .chat-agent-option:disabled')).to_have_count(1)
                await field.press("ArrowDown")
                await expect(field).to_be_focused()
                await expect(options.nth(1)).to_have_class(re.compile(r"\bkeyboard-active\b"))
                await field.press("ArrowUp")
                await expect(options.nth(0)).to_have_class(re.compile(r"\bkeyboard-active\b"))
                await field.press("ArrowUp")
                await expect(field).to_have_value("hello @")
                await field.press("ArrowDown")
                await field.press("Enter")
                assert await page.evaluate("activeAgent.id") == "writer"
                await expect(field).to_have_value("hello ")
                assert await _list_sessions(page, base_url) == []
                await field.fill("@Wri")
                await field.press("ArrowDown")
                await field.press("Tab")
                await expect(field).to_have_value("")
                await field.fill("@no-such-expert")
                await field.press("ArrowDown")
                await field.press("ArrowUp")
                await field.press("Enter")
                await expect(field).to_have_value("@no-such-expert")
                await field.press("Escape")
                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
            finally:
                await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 900}, {"width": 390, "height": 844}])
async def test_mention_picker_is_above_composer_without_opening_history(tmp_path, viewport):
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (writer,)) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport=viewport)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                # A fresh chat opens on the built-in Expert with the composer
                # ready; the picker is not part of the new-chat flow.
                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                await page.wait_for_function("() => isNewChatDraft()")
                await page.evaluate("closeSessionPanel()")
                input_box = page.locator("#chat-input")
                await input_box.fill("hello @Wri")
                menu = page.locator("#chat-agent-menu")
                await expect(menu).to_be_visible()
                await expect(menu).to_contain_text("Writer")
                await expect(page.locator("#session-panel")).to_be_hidden()
                menu_box = await menu.bounding_box()
                composer = await input_box.bounding_box()
                assert menu_box["y"] + menu_box["height"] <= composer["y"]
                assert abs(menu_box["x"] - composer["x"]) <= 2
                assert abs(menu_box["width"] - composer["width"]) <= 2
                assert menu_box["y"] >= 0
                await menu.locator(".chat-agent-option").filter(has_text="Writer").click()
                await expect(input_box).to_have_value("hello ")
                assert await page.evaluate("activeAgent.id") == "writer"
                assert await _list_sessions(page, base_url) == []
                await expect(menu).to_be_hidden()

                # The ordinary history entry retains its original anchor.
                await page.evaluate("openSessionPanel(); openAgentSessionNavigator()")
                await expect(page.locator(".session-new-chat-control #chat-agent-menu")).to_be_visible()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_bibit_default_and_cross_expert_mention_preserve_binding(tmp_path: Path):
    bibit = AgentSpec(id="com.bianbu.a1b2c3d4e5f6", name="BibitEdgeAssistant")
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (bibit, writer)) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                old = await _create_session(page, base_url, agent_id="writer", title="Writer history")
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => agentsLoaded")
                await expect(page.locator("#active-agent-bar")).to_contain_text("BibitEdgeAssistant")
                await expect(
                    page.locator("#active-agent-bar .active-agent-mention-hint")
                ).to_contain_text("输入 @ 切换专家")
                assert len(await _list_sessions(page, base_url)) == 1

                await page.evaluate("sid => switchSession(sid)", old["id"])
                await expect(page.locator("#active-agent-bar")).to_contain_text("Writer")
                await page.locator("#chat-input").fill("hello @Bibit")
                await expect(page.locator("#chat-agent-menu")).to_be_visible()
                await page.locator("#chat-agent-menu .chat-agent-option").filter(has_text="BibitEdgeAssistant").click()
                await expect(page.locator("#chat-input")).to_have_value("hello ")
                assert await page.evaluate("sessionId") is None
                assert await page.evaluate("isNewChatDraft()") is True
                assert len(await _list_sessions(page, base_url)) == 1
                await page.locator("#send-btn").click()
                await page.wait_for_function("() => sessionId && !isNewChatDraft()")
                records = await _list_sessions(page, base_url)
                assert len(records) == 2
                assert next(item for item in records if item["id"] == old["id"])["agent_id"] == "writer"
                assert next(item for item in records if item["id"] != old["id"])["agent_id"] == bibit.id

                current = await page.evaluate("sessionId")
                await page.locator("#chat-input").fill("@Bibit")
                await expect(page.locator("#chat-agent-menu")).to_be_visible()
                await page.locator("#chat-input").press("Enter")
                assert await page.evaluate("sessionId") == current
                assert len(await _list_sessions(page, base_url)) == 2
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_delayed_initial_restore_does_not_replace_user_selected_draft(tmp_path: Path):
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            release = asyncio.Event()
            entered = asyncio.Event()
            try:
                page = await browser.new_page()

                async def delayed_restore(route):
                    entered.set()
                    await release.wait()
                    await route.fulfill(status=200, content_type="application/json", body='{"data":[],"page":{"has_more":false}}')

                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                await page.route("**/api/v1/sessions?page=1&page_size=100", delayed_restore)
                await page.evaluate("() => { sessionId = 'missing-session'; window.pendingRestore = restorePersistedSession(sessionId); }")
                await asyncio.wait_for(entered.wait(), 5)
                await page.wait_for_function("() => agentsLoaded")
                await page.evaluate("createSessionForAgent('default')")
                await page.locator("#chat-input").fill("draft to preserve")
                async with page.expect_response("**/api/v1/sessions?page=1&page_size=100"):
                    release.set()
                await page.evaluate("window.pendingRestore")
                assert await page.evaluate("activeAgent?.id") == "default"
                assert await page.evaluate("isNewChatDraft()") is True
                await expect(page.locator("#chat-input")).to_have_value("draft to preserve")
            finally:
                release.set()
                await browser.close()


@pytest.mark.asyncio
async def test_dismissing_expert_picker_does_not_cancel_running_session(tmp_path: Path):
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (writer,)) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                cancelled = []
                page.on("request", lambda request: cancelled.append(request.url)
                        if request.url.endswith("/cancel") else None)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => agentsLoaded")
                await page.evaluate("createSessionForAgent('default')")
                await page.locator("#chat-input").fill("followup hold gate")
                await page.locator("#send-btn").click()
                await expect(page.locator(".bot-message-group").last).to_contain_text("FOLLOWUP_HOLD_READY")
                await page.locator("#chat-input").fill("@Writer")
                await expect(page.locator("#chat-agent-menu")).to_be_visible()
                await page.locator("#chat-input").press("Escape")
                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                assert cancelled == []
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_expert_mention_rescans_after_chinese_composition(tmp_path: Path):
    writer = AgentSpec(id="writer", name="Writer", name_zh="作家")
    async with _running_console(tmp_path, (writer,)) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => agentsLoaded")
                await page.evaluate("createSessionForAgent('default')")
                await page.locator("#chat-input").fill("@")
                await page.locator("#chat-input").evaluate("""input => {
                    input.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}));
                    input.value = '@作';
                    input.setSelectionRange(2, 2);
                    input.dispatchEvent(new InputEvent('input', {bubbles:true, isComposing:true}));
                    input.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true, data:'作'}));
                }""")
                await expect(page.locator("#chat-agent-menu .chat-agent-option")).to_have_count(1)
                await expect(page.locator("#chat-agent-menu")).to_be_visible()
                await page.locator("#chat-input").press("Enter")
                await expect(page.locator("#active-agent-name")).to_have_text("作家")
                await expect(page.locator("#chat-input")).to_have_value("")
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_new_chat_defaults_to_bibit_expert_and_switches_via_mention(tmp_path: Path):
    """New chat starts on the built-in Expert; "@" is how users switch."""
    bibit = AgentSpec(id="com.bianbu.a1b2c3d4e5f6", name="BibitEdgeAssistant")
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (bibit, writer)) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                previous = await _create_session(
                    page, base_url, agent_id="writer", title="Writer history"
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => agentsLoaded")
                await page.evaluate("sid => switchSession(sid)", previous["id"])
                await page.wait_for_function("() => activeAgent?.id === 'writer'")

                # Clicking the Expert bar inserts "@" instead of opening a page.
                await page.locator("#active-agent-bar").click()
                await expect(page.locator("#chat-agent-menu")).to_be_visible()
                await expect(page.locator("#chat-input")).to_be_focused()
                assert await page.evaluate("() => chatInput.value") == "@"
                await page.locator("#chat-input").press("Escape")
                await page.locator("#chat-input").fill("")

                await page.evaluate("() => openSessionPanel()")
                await page.locator("#history-new-chat-btn").click()

                # The fresh chat always belongs to the built-in Expert.
                await page.wait_for_function(
                    "() => isNewChatDraft() && newChatDraft.agent.id === 'com.bianbu.a1b2c3d4e5f6'"
                )
                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                await expect(page.locator("#active-agent-bar")).to_contain_text(
                    "BibitEdgeAssistant"
                )
                assert len(await _list_sessions(page, base_url)) == 1

                # Switching Experts stays inside the composer.
                await page.locator("#chat-input").fill("@Writer")
                await expect(page.locator("#chat-agent-menu")).to_be_visible()
                await page.locator("#chat-agent-menu .chat-agent-option").filter(
                    has_text="Writer"
                ).click()
                await expect(page.locator("#chat-input")).to_have_value("")
                assert await page.evaluate("() => newChatDraft?.agent?.id") == "writer"
                await expect(page.locator("#active-agent-bar")).to_contain_text("Writer")
                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                assert len(await _list_sessions(page, base_url)) == 1
            finally:
                await browser.close()
