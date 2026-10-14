from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
import socket

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from bianbu_agent import (
    AgentManifest,
    AgentPackageManager,
    AgentModel,
    AgentSpec,
    BianbuAgentRuntime,
    AgentHome,
    RunEvent,
    RuntimeConfig,
    RuntimePresetsConfig,
    create_app,
    ProviderConfig,
    YAMLModelStore,
)
from bianbu_agent.skills import SkillPackageManager


_PRESET_TOOLS = (
    "skills_list",
    "skill_view",
    "skill_manage",
    "terminal",
    "process",
    "read_file",
    "write_file",
    "search_files",
    "patch",
)


@dataclass
class _Model:
    model: str = "agent-browser"


class _Engine:
    async def stream(self, prompt: str, **_: object):
        yield RunEvent("token", {"text": prompt})
        yield RunEvent("done", {})


def _reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@asynccontextmanager
async def _running_console(
    tmp_path: Path,
    *,
    configured: bool = True,
    include_default_tools: bool = False,
) -> AsyncIterator[tuple[str, AgentHome]]:
    agent = AgentSpec(
        id="com.bianbu.123456789abc",
        name="Starter",
        name_zh="起步专家",
        description="Starter agent",
        soul="Be useful",
        model=AgentModel("browser", "agent-browser") if configured else None,
        tools=("*",),
        skills=("*",),
        metadata={"avatar": "🤖"},
    )
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    providers = YAMLModelStore(home.models_dir)
    await providers.open()
    if configured:
        await providers.upsert_provider(
            ProviderConfig(
                id="browser",
                name="Browser",
                protocol="openai-compatible",
                base_url="https://model.test/v1",
                api_key="secret",
            )
        )
    await providers.close()
    skill_root = tmp_path / "skill-source" / "status-guide"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: status-guide\n"
        "description: Inspect status safely\n"
        "version: 1.0.0\n"
        "---\n\n"
        "Inspect the current status.\n",
        encoding="utf-8",
    )
    SkillPackageManager(home).install(skill_root)
    AgentPackageManager(home).create(agent)
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(
            data_dir=home.root,
            skill_dirs=(home.skills_dir,),
            # The editor locks the runtime presets; devices that ship none show
            # an empty default selection, so the console test declares them.
            presets=(
                RuntimePresetsConfig(tools=_PRESET_TOOLS, skills=("using-apm",))
                if include_default_tools
                else RuntimePresetsConfig()
            ),
        ),
        default_model=_Model(),
        agent_id=agent.id,
        engine_builder=lambda *args: _Engine(),
    )
    port = _reserve_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(sdk),
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}", home
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_agent_editor_owns_no_security_configuration(
    tmp_path: Path,
) -> None:
    """安全配置只在安全控制台维护；专家编辑器不再出现任何相关控件。"""

    async with _running_console(tmp_path, include_default_tools=True) as environment:
        base_url, home = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="agents"]').click()
                await page.locator(
                    '#view-agents button[onclick="openAgentEditor()"]'
                ).first.click()

                for selector in (
                    "#agent-security-panel",
                    "#agent-security-policies",
                    "#agent-security-summary",
                    "#agent-security-levels",
                    "#agent-form-runtime",
                    "#agent-runtime-status",
                ):
                    await expect(page.locator(selector)).to_have_count(0)

                await page.locator("#agent-form-name").fill("Guarded Researcher")
                await page.locator("#agent-form-model-name").fill("agent-browser")
                await page.locator("#agent-editor-save").click()
                await expect(page.locator("#agent-editor-overlay")).to_be_hidden()

                created_roots = [
                    path
                    for path in home.agents_dir.iterdir()
                    if path.is_dir() and path.name != "com.bianbu.123456789abc"
                ]
                assert len(created_roots) == 1
                created = AgentPackageManager(home).inspect(created_roots[0].name)
                assert created is not None
                # 没有安全声明：权限与执行模式都由 securityd 推导。
                assert dict(created.manifest.spec.permissions) == {}
                assert "execution" not in dict(created.manifest.spec.metadata)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_model_less_selected_agent_persistently_guides_setup(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path, configured=False) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                # New chat goes straight to the default Expert draft; the
                # model-setup guidance replaces the old Expert picker detour.
                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                await page.wait_for_function("() => isNewChatDraft()")

                reminder = page.locator("#model-setup-required")
                await expect(reminder).to_be_visible()
                await expect(reminder).to_contain_text("当前 Agent 尚未配置模型")
                await expect(reminder).to_contain_text("添加服务商并填写 API Key")
                await expect(reminder).to_contain_text("为当前 Agent 选择模型")
                await expect(page.locator("#chat-input")).to_be_disabled()

                await page.locator("#model-setup-provider").click()
                await expect(page.locator("#view-models")).to_have_class(
                    re.compile(r"\bactive\b")
                )
                await page.locator('[data-view="chat"]').click()
                await page.locator("#model-setup-agent").click()
                await expect(page.locator("#agent-editor-overlay")).to_be_visible()
                await expect(page.locator("#agent-form-name")).to_have_value(
                    "Starter"
                )
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_agent_editor_saves_without_model_then_guides_setup(
    tmp_path: Path,
) -> None:
    async with _running_console(
        tmp_path,
        configured=True,
        include_default_tools=True,
    ) as environment:
        base_url, home = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="agents"]').click()
                await page.locator(
                    '#view-agents button[onclick="openAgentEditor()"]'
                ).first.click()
                await page.locator("#agent-form-name").fill("待配置专家")
                checked_tools = page.locator(
                    '#agent-form-tools input[type="checkbox"]:checked'
                )
                await expect(checked_tools).to_have_count(9)
                await page.locator("#agent-editor-save").click()
                await expect(page.locator("#agent-editor-overlay")).to_be_hidden()

                card = page.locator(".agent-card", has_text="待配置专家")
                await expect(card).to_be_visible()
                await expect(card.locator(".agent-card-use")).to_be_enabled()
                await card.locator(".agent-card-use").click()
                await expect(page.locator("#model-setup-required")).to_be_visible()
                await expect(page.locator("#chat-input")).to_be_disabled()
            finally:
                await browser.close()

        created = [
            AgentPackageManager(home).inspect(path.name)
            for path in home.agents_dir.iterdir()
            if path.is_dir() and path.name != "com.bianbu.123456789abc"
        ]
        assert len(created) == 1
        assert created[0] is not None
        assert created[0].manifest.spec.model is None
        assert set(created[0].manifest.spec.tools) == {
            "skills_list",
            "skill_view",
            "skill_manage",
            "terminal",
            "process",
            "read_file",
            "write_file",
            "search_files",
            "patch",
        }


@pytest.mark.asyncio
async def test_agent_editor_never_persists_a_declared_execution_mode(tmp_path):
    """重启运行形态由安全设置推导，编辑器保存后不应留下 execution 声明。"""

    async with _running_console(tmp_path) as (base_url, home):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f'{base_url}/chat', wait_until='domcontentloaded')
                await page.locator('[data-view="agents"]').click()
                await page.locator('.agent-card', has_text='起步专家').click()
                await expect(page.locator('#agent-form-runtime')).to_have_count(0)
                await page.locator('#agent-editor-save').click()
                await expect(page.locator('#agent-editor-overlay')).to_be_hidden()

                saved = AgentPackageManager(home).inspect('com.bianbu.123456789abc')
                assert saved is not None
                assert "execution" not in dict(saved.manifest.spec.metadata)

                payload = home.agents_dir / 'com.bianbu.123456789abc' / 'agent.yaml'
                assert 'requested_mode' not in payload.read_text(encoding='utf-8')
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_new_chat_prefers_system_bibit_over_legacy_same_name(tmp_path):
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                async def agents(route):
                    response = await route.fetch()
                    data = await response.json()
                    original = data['data'][0]
                    data['data'] = [
                        {**original, 'id': 'bianbu-agent-default', 'name': 'BibitEdgeAssistant', 'origin': 'user'},
                        {**original, 'id': 'com.bianbu.aaaaaaaaaaaa', 'name': 'BibitEdgeAssistant', 'origin': 'system'},
                    ]
                    await route.fulfill(response=response, json=data)
                await page.route('**/api/v1/agents', agents)
                await page.goto(f'{base_url}/chat', wait_until='networkidle')
                await page.evaluate('enterAgentChooserDraft()')
                assert await page.evaluate('activeAgent.id') == 'com.bianbu.aaaaaaaaaaaa'
                await page.evaluate('startNewChat()')
                assert await page.evaluate('activeAgent.id') == 'com.bianbu.aaaaaaaaaaaa'
            finally:
                await browser.close()
