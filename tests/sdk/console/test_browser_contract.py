from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
import socket
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect
from starlette.responses import JSONResponse, StreamingResponse

from bianbu_agent import (
    AgentSpec,
    AgentModel,
    AgentManifest,
    AgentPackageManager,
    BianbuAgentRuntime,
    AgentHome,
    RunEvent,
    RuntimeConfig,
    create_app,
    ProviderConfig,
    YAMLModelStore,
)
from bianbu_agent.capture.llm import ProviderCaptureStore
from bianbu_agent.plugins.models import PluginHealth
from bianbu_agent.mcp.client import MCPClient


@dataclass
class _Model:
    model: str = "browser-contract"


class _Engine:
    def __init__(
        self,
        parallel_started: list[str] | None = None,
        parallel_release: asyncio.Event | None = None,
    ) -> None:
        self.parallel_started = parallel_started
        self.parallel_release = parallel_release

    async def _park_approval(self, control, payload):
        """Publish one approval and stay parked until the Run answers it.

        A tool approval no longer ends the Run: the decision is handed back to
        the same Run, so the stream has to remain alive while it waits.
        """

        parking = asyncio.ensure_future(control.request_approval(payload))
        await asyncio.sleep(0)
        yield RunEvent("approval_required", payload)
        await parking

    async def stream(self, prompt: str | None, **kwargs: object):
        assert prompt is not None
        if isinstance(prompt, (list, tuple)):
            contents = [item.content for item in prompt]
            if len(contents) == 1:
                prompt = contents[0]
            elif "race target" in contents:
                yield RunEvent("token", {"text": "RACE_TARGET_DONE"})
                yield RunEvent("done", {})
                return
            elif "queued target slow" in contents:
                yield RunEvent("token", {"text": "TARGET_STREAM_STARTED"})
                await asyncio.sleep(0.25)
                yield RunEvent("token", {"text": " TARGET_STREAM_DONE"})
                yield RunEvent("done", {})
                return
            else:
                yield RunEvent("token", {"text": " | ".join(contents)})
                yield RunEvent("done", {})
                return
        if "title recovery gate" in prompt and "Generate a concise conversation title" in prompt:
            async for event in self._park_approval(
                kwargs["control"],
                {"approval_id": "title-gate", "actions": []},
            ):
                yield event
            return
        if "attachment followup gate" in prompt:
            yield RunEvent("token", {"text": "ATTACHMENT_GATE_READY"})
            await asyncio.sleep(2.0)
            yield RunEvent("token", {"text": "ATTACHMENT_GATE_DONE"})
            yield RunEvent("done", {})
            return
        if "running timer refresh gate" in prompt:
            yield RunEvent("token", {"text": "RUNNING_TIMER_READY"})
            await asyncio.sleep(3.0)
            yield RunEvent("token", {"text": " RUNNING_TIMER_DONE"})
            yield RunEvent("done", {})
            return
        if "followup approval gate" in prompt:
            yield RunEvent("token", {"text": "FOLLOWUP_APPROVAL_READY"})
            async for event in self._park_approval(
                kwargs["control"],
                {
                    "approval_id": "followup-approval",
                    "actions": [{"name": "wait", "arguments": {}}],
                },
            ):
                yield event
            return
        if "followup hold gate" in prompt:
            yield RunEvent("token", {"text": "FOLLOWUP_HOLD_READY"})
            await kwargs["control"].cancelled.wait()
            return
        if "followup immediate gate" in prompt:
            yield RunEvent("token", {"text": "FOLLOWUP_IMMEDIATE_READY"})
            await kwargs["control"].followup_available.wait()
            yield RunEvent("done", {})
            return
        if "queued target slow" in prompt:
            yield RunEvent("token", {"text": "TARGET_STREAM_STARTED"})
            await asyncio.sleep(0.25)
            yield RunEvent("token", {"text": " TARGET_STREAM_DONE"})
            yield RunEvent("done", {})
            return
        parallel_match = re.search(r"parallel session ([ABC])", prompt)
        if parallel_match:
            label = parallel_match.group(1)
            yield RunEvent("token", {"text": f"START-{label}"})
            if self.parallel_started is not None and self.parallel_release is not None:
                self.parallel_started.append(label)
                if len(self.parallel_started) == 3:
                    self.parallel_release.set()
                await self.parallel_release.wait()
            await asyncio.sleep(0.1)
            yield RunEvent("token", {"text": f" DONE-{label}"})
            yield RunEvent("done", {})
            return
        if "distinct minimax thinking" in prompt:
            yield RunEvent("reasoning", {"text": "private plan", "round": 1})
            await asyncio.sleep(1.0)
            yield RunEvent("token", {"text": "public answer"})
            yield RunEvent("done", {})
            return
        if "ordered live assistant stream" in prompt:
            yield RunEvent("reasoning", {"text": "plan one", "round": 1})
            await asyncio.sleep(0.75)
            yield RunEvent("token", {"text": "answer one"})
            await asyncio.sleep(1.0)
            yield RunEvent(
                "tool_start",
                {
                    "call_id": "lookup-1",
                    "name": "lookup",
                    "arguments": {"query": "live"},
                },
            )
            await asyncio.sleep(0.25)
            yield RunEvent(
                "tool_end",
                {"call_id": "lookup-1", "result": "tool result"},
            )
            yield RunEvent("reasoning", {"text": "plan two", "round": 2})
            await asyncio.sleep(1.0)
            yield RunEvent("token", {"text": "answer two"})
            yield RunEvent("done", {})
            return
        if "terminal reasoning done" in prompt:
            yield RunEvent("reasoning", {"text": "done-only plan", "round": 1})
            return
        if "terminal reasoning failure" in prompt:
            yield RunEvent("reasoning", {"text": "failed plan", "round": 1})
            raise RuntimeError("reasoning stream failed")
        yield RunEvent("token", {"text": prompt})
        yield RunEvent("done", {})


def _reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@asynccontextmanager
async def _running_console(
    tmp_path: Path,
    extra_agents: tuple[AgentSpec, ...] = (),
    stalled_title_bodies: int = 0,
    mcp_health_started: asyncio.Event | None = None,
    mcp_health_gate: asyncio.Event | None = None,
    mcp_health_started_at: list[float] | None = None,
    mcp_catalog_started: asyncio.Event | None = None,
    mcp_catalog_gate: asyncio.Event | None = None,
    mcp_health_check_error: str | None = None,
    parallel_started: list[str] | None = None,
    parallel_release: asyncio.Event | None = None,
    role: str = "worker",
) -> AsyncIterator[tuple[str, AgentHome]]:
    default = AgentSpec(
        id="default",
        name="Default",
        model=AgentModel("browser", "browser-contract"),
        mcps=("mcp.browser-contract",) if mcp_health_gate is not None else (),
    )
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    provider_store = YAMLModelStore(home.models_dir)
    await provider_store.open()
    await provider_store.upsert_provider(
        ProviderConfig(
            id="browser",
            name="Browser",
            protocol="openai-compatible",
            base_url="https://model.test/v1",
            api_key="secret",
        )
    )
    await provider_store.close()
    packages = AgentPackageManager(home, protected_ids={default.id})
    packages.create(default)
    for agent in extra_agents:
        packages.create(
            agent
            if isinstance(agent.model, AgentModel)
            else replace(
                agent,
                model=AgentModel("browser", "browser-contract"),
            )
        )
    if mcp_health_gate is not None:
        async def no_mcp_tools(_self: MCPClient):
            return ()

        mcp_client_patcher = patch.object(MCPClient, "list_tools", no_mcp_tools)
        mcp_client_patcher.start()
        mcp_start_patcher = patch.object(MCPClient, "start", no_mcp_tools)
        mcp_start_patcher.start()
        package = home.mcps_dir / "mcp.browser-contract"
        # Extension roots are created on demand by the manager that writes
        # them, so a fresh Home has no ``mcps/`` yet.
        package.mkdir(parents=True)
        (package / "mcp.json").write_text(
            json.dumps({
                "display_name": "Browser contract",
                "transport": "stdio",
                "command": "fake",
            }),
            encoding="utf-8",
        )
    sdk = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=home.root),
        default_model=_Model(),
        agent_id=default.id,
        engine_builder=lambda *args: _Engine(parallel_started, parallel_release),
        role=role,
    )
    if mcp_health_gate is not None:
        async def controlled_mcp_health():
            if mcp_health_started_at is not None:
                mcp_health_started_at.append(asyncio.get_running_loop().time())
            if mcp_health_started is not None:
                mcp_health_started.set()
            await mcp_health_gate.wait()
            generation = sdk.plugin_runtime.current_generation
            return {
                server.runtime_id: PluginHealth.healthy()
                for server in generation.mcps.values()
            }

        sdk.plugin_runtime.health = controlled_mcp_health
    port = _reserve_port()
    app = create_app(sdk)
    if mcp_catalog_gate is not None:
        @app.middleware("http")
        async def wait_for_mcp_catalog(request, call_next):
            if (
                request.method == "GET"
                and request.url.path == "/api/v1/mcp-servers"
            ):
                if mcp_catalog_started is not None:
                    mcp_catalog_started.set()
                await mcp_catalog_gate.wait()
            return await call_next(request)

    if mcp_health_check_error is not None:
        async def rejected_mcp_health_check():
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "code": "mcp_health_unavailable",
                        "message": mcp_health_check_error,
                        "details": {},
                    }
                },
            )

        app.add_api_route(
            "/api/v1/mcp-health-checks",
            rejected_mcp_health_check,
            methods=["POST"],
        )
        app.router.routes.insert(0, app.router.routes.pop())
    if stalled_title_bodies:
        stalled_body_calls = 0

        async def stalled_title_snapshot():
            nonlocal stalled_body_calls
            stalled_body_calls += 1
            if stalled_body_calls <= stalled_title_bodies:
                async def body():
                    yield b'{"data":'
                    await asyncio.sleep(60)

                return StreamingResponse(body(), media_type="application/json")
            return JSONResponse({
                "data": {
                    "id": "stalled-title-body",
                    "session_id": "stalled-title-owner",
                    "agent_id": "default",
                    "status": "failed",
                    "content": "",
                    "usage": None,
                    "error": {"code": "test_failure", "message": "finished"},
                    "pending_actions": [],
                    "created_at": 1,
                    "updated_at": 2,
                }
            })

        app.add_api_route(
            "/api/v1/runs/stalled-title-body",
            stalled_title_snapshot,
            methods=["GET"],
        )
        app.router.routes.insert(0, app.router.routes.pop())

    server = uvicorn.Server(
        uvicorn.Config(
            app,
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
        if mcp_health_gate is not None:
            mcp_health_gate.set()
        if mcp_catalog_gate is not None:
            mcp_catalog_gate.set()
        server.should_exit = True
        await task
        if mcp_health_gate is not None:
            mcp_client_patcher.stop()
            mcp_start_patcher.stop()


async def _open_agent_chooser(page) -> None:
    """Reveal the Expert picker from the history panel for explicit browsing."""
    menu = page.locator("#chat-agent-menu")
    if await menu.is_visible():
        return
    panel = page.locator("#session-panel")
    if await panel.evaluate("element => element.classList.contains('hidden')"):
        await page.locator("#session-toggle-btn").click()
    await expect(panel).not_to_have_class(re.compile(r"\bhidden\b"))
    await page.evaluate("() => openAgentSessionNavigator()")


async def _select_default_agent(page) -> None:
    await page.wait_for_function("() => agentsLoaded")
    await page.evaluate("() => createSessionForAgent('default')")


async def _prepare_default_draft(page) -> None:
    await _select_default_agent(page)
    await page.evaluate("() => prepareConversationSend()")


async def _create_session(
    page,
    base_url: str,
    *,
    agent_id: str = "default",
    title: str = "",
) -> dict[str, object]:
    response = await page.request.post(
        f"{base_url}/api/v1/sessions",
        data={"agent_id": agent_id, "title": title},
    )
    assert response.status == 201
    return (await response.json())["data"]


async def _create_run(
    page,
    base_url: str,
    *,
    session_id: str,
    prompt: str,
    agent_id: str | None = None,
) -> dict[str, object]:
    response = await page.request.post(
        f"{base_url}/api/v1/sessions/{session_id}/prompt",
        data={
            "message_id": f"test-{abs(hash(prompt))}",
            "content": prompt,
        },
    )
    assert response.status == 202
    accepted = (await response.json())["data"]
    run_id = str(accepted["run_id"])
    _TEST_RUN_SESSIONS[run_id] = session_id
    return {"id": run_id, "session_id": session_id, "status": "running"}


_TEST_RUN_SESSIONS: dict[str, str] = {}


async def _wait_for_run(
    page,
    base_url: str,
    run_id: str,
    *,
    session_id: str | None = None,
) -> dict[str, object]:
    session_id = session_id or _TEST_RUN_SESSIONS[run_id]
    for _ in range(200):
        response = await page.request.get(
            f"{base_url}/api/v1/sessions/{session_id}"
        )
        assert response.ok
        snapshot = (await response.json())["data"]
        if snapshot.get("active_run_id") != run_id:
            events = await _session_events(page, base_url, session_id)
            terminal = next(
                (
                    event for event in reversed(events)
                    if event.get("run_id") == run_id
                    and event.get("type") in {
                        "run/completed", "run/failed", "run/cancelled"
                    }
                ),
                None,
            )
            if terminal is not None:
                return {
                    "id": run_id,
                    "status": str(terminal["data"]["status"]),
                    "content": str(terminal["data"].get("content") or ""),
                }
        await asyncio.sleep(0.01)
    raise AssertionError(f"Run did not finish: {run_id}")


async def _wait_for_followups(
    page,
    base_url: str,
    run_id: str,
    *,
    status: str | None = None,
    count: int | None = None,
) -> list[dict[str, object]]:
    for _ in range(300):
        response = await page.request.get(
            f"{base_url}/api/v1/runs/{run_id}/followups"
        )
        assert response.ok
        rows = (await response.json())["data"]
        if count is not None and len(rows) != count:
            await asyncio.sleep(0.01)
            continue
        if status is not None and (
            not rows or any(row["status"] != status for row in rows)
        ):
            await asyncio.sleep(0.01)
            continue
        return rows
    raise AssertionError(
        f"Follow-ups did not reach status={status!r}, count={count!r}: {rows!r}"
    )


async def _list_sessions(page, base_url: str) -> list[dict[str, object]]:
    response = await page.request.get(
        f"{base_url}/api/v1/sessions?page=1&page_size=100"
    )
    assert response.ok
    return (await response.json())["data"]


async def _session_events(page, base_url: str, session_id: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    before_seq: int | None = None
    while True:
        query = "?max_messages=50"
        if before_seq is not None:
            query += f"&before_seq={before_seq}"
        response = await page.request.get(
            f"{base_url}/api/v1/sessions/{session_id}/events{query}"
        )
        assert response.ok
        payload = await response.json()
        page_events = payload["data"]
        events = [*page_events, *events]
        if not payload.get("has_more") or not page_events:
            return events
        before_seq = int(payload["base_seq"])


async def _open_active_run(page, base_url: str, *, prompt: str) -> tuple[str, str]:
    session = await _create_session(page, base_url, title="Active")
    session_id = str(session["id"])
    run = await _create_run(
        page,
        base_url,
        session_id=session_id,
        prompt=prompt,
    )
    run_id = str(run["id"])
    await page.add_init_script(
        """(() => {
            const sessionId = %s;
            const runId = %s;
            localStorage.setItem('bianbu_session_id', sessionId);
            localStorage.setItem('bianbu_active_runs', JSON.stringify({[sessionId]: runId}));
        })()""" % (json.dumps(session_id), json.dumps(run_id))
    )
    await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
    await page.wait_for_function(
        "runId => activeRequestId === runId", arg=run_id
    )
    return session_id, run_id


@pytest.mark.asyncio
async def test_reasoning_and_answer_use_distinct_streaming_views(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                session_id = str((await _create_session(page, base_url))["id"])
                await page.add_init_script(
                    "localStorage.setItem('bianbu_session_id', "
                    f"{json.dumps(session_id)})"
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator("#chat-input").fill("distinct minimax thinking")
                await page.locator("#send-btn").click()

                thinking = page.locator(".agent-thinking-step").last
                answer = page.locator(".answer-content").last
                await expect(thinking).to_contain_text("private plan")
                await expect(answer).to_have_text("public answer")
                await expect(thinking.locator(".thinking-full")).to_be_hidden()
                await expect(thinking).not_to_contain_text("public answer")
                await expect(answer).not_to_contain_text("private plan")
                await expect(page.locator(".user-message-group")).to_have_count(1)
                await expect(page.locator(".bot-message-group")).to_have_count(1)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_host_snapshot_publishes_title_without_replacing_session_item(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => !_sessionLoading")
                await _select_default_agent(page)
                session = await _create_session(page, base_url, title="Before")
                sid = str(session["id"])
                item = page.locator(f'.session-item[data-session-id="{sid}"]')
                await expect(item.locator(".session-title")).to_have_text("Before")
                await page.evaluate(
                    "sid => { window.__hostSessionItem = document.querySelector("
                    "`.session-item[data-session-id=\"${sid}\"]`); }",
                    sid,
                )

                response = await page.request.patch(
                    f"{base_url}/api/v1/sessions/{sid}", data={"title": "After"}
                )
                assert response.ok
                await expect(item.locator(".session-title")).to_have_text("After")
                assert await page.evaluate(
                    "sid => window.__hostSessionItem === document.querySelector("
                    "`.session-item[data-session-id=\"${sid}\"]`)",
                    sid,
                ) is True
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_host_snapshot_does_not_rebuild_untouched_session_rows(
    tmp_path: Path,
) -> None:
    """主机快照只更新真的变了的内容，别的会话行不重插。

    每次推送都把整个列表重新 append 一遍，会摘掉悬停行的 `:hover`，行底色与
    重命名/删除按钮再淡入一次；鼠标停在上面时看起来就是一直闪。
    """

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => !_sessionLoading")
                await _select_default_agent(page)
                hovered = await _create_session(
                    page, base_url, title="Hover target"
                )
                other = await _create_session(page, base_url, title="Other session")
                await page.evaluate("() => openSessionPanel()")
                row = page.locator(
                    f'.session-item[data-session-id="{hovered["id"]}"]'
                )
                await expect(row).to_be_visible()
                await row.hover()
                await expect(row.locator(".session-rename")).to_have_css(
                    "opacity", "1"
                )

                probe = await page.evaluate(
                    """async ([hoveredId, otherId]) => {
                        const container = document.getElementById('session-list');
                        const row = document.querySelector(
                            `.session-item[data-session-id="${hoveredId}"]`);
                        const rename = row.querySelector('.session-rename');
                        const reinserted = [];
                        const observer = new MutationObserver(records => {
                            records.forEach(record => {
                                if (record.target === container) reinserted.push(record.type);
                            });
                        });
                        observer.observe(container, {childList: true, subtree: true});
                        const payload = Array.from(sessionsById.values())
                            .map(record => ({...record}));
                        const target = payload.find(record => record.id === otherId);
                        target.persistence = {
                            state: 'pending', durable_events: 1,
                            pending_events: 2, error: null,
                        };
                        target.running = true;
                        sessionConnection._message(
                            'host',
                            JSON.stringify({type: 'host/sessions', sessions: payload}),
                        );
                        await new Promise(resolve => setTimeout(resolve, 0));
                        observer.disconnect();
                        return {
                            reinserted,
                            hovered: row.matches(':hover'),
                            opacity: getComputedStyle(rename).opacity,
                            renameNode: row.querySelector('.session-rename') === rename,
                        };
                    }""",
                    [str(hovered["id"]), str(other["id"])],
                )
                assert probe == {
                    "reinserted": [],
                    "hovered": True,
                    "opacity": "1",
                    "renameNode": True,
                }
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_session_projection_preserves_assistant_event_order(
    tmp_path: Path,
) -> None:
    """Changing the reducer back to type buckets must break this contract."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                message = await page.evaluate(
                    """() => {
                        const model = new BianbuSessionRuntime.SessionViewModel('ordered');
                        model.installHistory([
                            {seq: 0, type: 'user/message', run_id: 'run', time: 1,
                             data: {content: 'question'}},
                            {seq: 1, type: 'assistant/reasoning.delta', run_id: 'run', time: 2,
                             data: {text: 'plan one'}},
                            {seq: 2, type: 'assistant/text.delta', run_id: 'run', time: 3,
                             data: {text: 'answer one'}},
                            {seq: 3, type: 'tool/call.started', run_id: 'run', time: 4,
                             data: {call_id: 'tool-1', name: 'lookup', arguments: {q: 'x'}}},
                            {seq: 4, type: 'tool/call.completed', run_id: 'run', time: 5,
                             data: {call_id: 'tool-1', result: 'found', status: 'success'}},
                            {seq: 5, type: 'assistant/reasoning.delta', run_id: 'run', time: 6,
                             data: {text: 'plan two'}},
                            {seq: 6, type: 'assistant/text.delta', run_id: 'run', time: 7,
                             data: {text: 'answer two'}},
                            {seq: 7, type: 'run/completed', run_id: 'run', time: 8,
                             data: {content: 'answer oneanswer two'}},
                        ]);
                        return model.snapshot().messages[1];
                    }"""
                )
                assert message["steps"] == [
                    {"type": "thinking", "content": "plan one"},
                    {"type": "content", "content": "answer one"},
                    {
                        "type": "tool",
                        "tool_call_id": "tool-1",
                        "name": "lookup",
                        "arguments": {"q": "x"},
                        "result": "found",
                        "status": "success",
                    },
                    {"type": "thinking", "content": "plan two"},
                    {"type": "content", "content": "answer two"},
                ]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_pending_user_message_stays_before_its_live_assistant_reply(
    tmp_path: Path,
) -> None:
    """A reply received before claim history must not jump above the input."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                roles = await page.evaluate(
                    """() => {
                        const model = new BianbuSessionRuntime.SessionViewModel('pending');
                        model.installHistory([
                            {seq: 10, type: 'agent/inbox/spliced', run_id: null,
                             time: 10, data: {message: {message_id: 'message-1',
                                 content: 'hi', attachments: [], created_at: 10}}},
                            {seq: 11, type: 'assistant/text.delta', run_id: 'run-1',
                             time: 11, data: {text: 'hello'}},
                        ]);
                        return model.snapshot().messages.map(message => ({
                            role: message.role, content: message.content,
                        }));
                    }"""
                )
                assert roles == [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_live_renderer_repairs_assistant_before_pending_user_dom_order(
    tmp_path: Path,
) -> None:
    """A WebSocket reply racing the prompt response must be reordered."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                roles = await page.evaluate(
                    """() => {
                        const sid = sessionId;
                        const model = sessionRuntime.get(sid);
                        model.installHistory([
                            {seq: 10, type: 'agent/inbox/spliced', run_id: null,
                             time: 10, data: {message: {message_id: 'message-race',
                                 content: 'hi', attachments: [], created_at: 10}}},
                            {seq: 11, type: 'assistant/text.delta', run_id: 'run-race',
                             time: 11, data: {text: 'hello'}},
                        ]);
                        messagesDiv.replaceChildren();
                        const assistant = model.snapshot().messages[1];
                        const bot = createBotMessageEl(
                            assistant.content, new Date(11000), 'run-race', assistant,
                        );
                        bot.dataset.requestId = 'run-race';
                        bot.dataset.sessionSegmentId = assistant.segment_id;
                        bot.dataset.sessionMessageKey = sessionMessageKey(assistant);
                        messagesDiv.appendChild(bot);
                        const user = addUserMessage('hi', new Date(10000), []);
                        user.dataset.messageId = 'message-race';

                        renderLiveSessionModelSnapshot(sid);
                        return Array.from(messagesDiv.children)
                            .filter(element => element.matches(
                                '.user-message-group, .bot-message-group'
                            ))
                            .map(element => element.classList.contains(
                                'user-message-group'
                            ) ? 'user' : 'assistant');
                    }"""
                )
                assert roles == ["user", "assistant"]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_trajectory_projection_groups_records_and_calculates_metrics(
    tmp_path: Path,
) -> None:
    """Trajectory is an independent deterministic fold of Session events."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                trajectory = await page.evaluate(
                    """() => BianbuTrajectory.projectTrajectory([
                        {seq: 0, type: 'user/message', run_id: 'run-a', time: 10,
                         data: {content: 'Find <unsafe> weather'}},
                        {seq: 1, type: 'assistant/reasoning.delta', run_id: 'run-a',
                         time: 11, data: {text: 'check '}},
                        {seq: 2, type: 'assistant/reasoning.delta', run_id: 'run-a',
                         time: 12, data: {text: 'forecast'}},
                        {seq: 3, type: 'tool/call.started', run_id: 'run-a', time: 13,
                         data: {call_id: 'call-1', name: 'weather',
                                arguments: {city: 'Nanjing'}}},
                        {seq: 4, type: 'tool/call.progress', run_id: 'run-a', time: 14,
                         data: {call_id: 'call-1', content: 'fetching'}},
                        {seq: 5, type: 'tool/call.completed', run_id: 'run-a', time: 15,
                         data: {call_id: 'call-1', result: 'sunny', status: 'success'}},
                        {seq: 6, type: 'assistant/text.delta', run_id: 'run-a', time: 16,
                         data: {text: 'It is '}},
                        {seq: 7, type: 'assistant/text.delta', run_id: 'run-a', time: 17,
                         data: {text: 'sunny.'}},
                        {seq: 8, type: 'run/completed', run_id: 'run-a', time: 18,
                         data: {usage: {input_tokens: 12, output_tokens: 5,
                                       total_tokens: 17}}},
                        {seq: 9, type: 'custom/diagnostic', run_id: 'run-b', time: 20,
                         data: {message: 'kept'}},
                        {seq: 10, type: 'run/failed', run_id: 'run-b', time: 21,
                         data: {error: {message: 'boom'}}},
                    ])"""
                )
                assert "summary" not in trajectory
                first = trajectory["turns"][0]
                assert first["run_id"] == "run-a"
                assert first["status"] == "completed"
                assert first["duration"] == 8
                # 轨迹要能对账：耗时 = 覆盖 + 等待，等待行自己也在列表里。
                assert [record["kind"] for record in first["records"]] == [
                    "user", "wait", "reasoning", "tool", "wait", "assistant", "run",
                ]
                assert first["covered"] + first["waiting"] == pytest.approx(
                    first["duration"]
                )
                assert [
                    record["wait"]["reason"]
                    for record in first["records"] if record["kind"] == "wait"
                ] == ["startup", "model"]
                assert first["records"][1]["preview"] == "No events for 1.00s"
                assert first["records"][2]["output"] == "check forecast"
                assert first["records"][2]["duration"] == 2
                assert first["records"][3] == {
                    "key": "tool:call-1:3",
                    "seq": 3,
                    "end_seq": 5,
                    "run_id": "run-a",
                    "turn": 1,
                    "kind": "tool",
                    "title": "weather",
                    "preview": "sunny",
                    "input": '{\n  "city": "Nanjing"\n}',
                    "output": "sunny",
                    "status": "success",
                    "started_at": 13,
                    "ended_at": 15,
                    "duration": 2,
                    "data": {
                        "call_id": "call-1",
                        "progress": "fetching",
                    },
                }
                assert trajectory["turns"][1]["records"][0]["kind"] == "event"
                assert trajectory["turns"][1]["records"][0]["title"] == (
                    "custom/diagnostic"
                )
                assert trajectory["turns"][1]["status"] == "failed"
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_trajectory_rows_reconcile_with_the_turn_wall_clock(
    tmp_path: Path,
) -> None:
    """耗时 = 记录覆盖 + 等待；嵌套与并行行只解释重叠，不重复计入。"""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                trajectory = await page.evaluate(
                    """() => BianbuTrajectory.projectTrajectory([
                        {seq: 0, type: 'user/message', run_id: 'run-a', time: 100,
                         data: {content: 'hello'}},
                        {seq: 1, type: 'hook/invoked', run_id: 'run-a', time: 105,
                         data: {invocation_id: 'h1', point: 'before_run',
                                hook_id: 'bianbu.memory/memory.before_run'}},
                        {seq: 2, type: 'hook/result', run_id: 'run-a', time: 106,
                         data: {invocation_id: 'h1', point: 'before_run',
                                hook_id: 'bianbu.memory/memory.before_run',
                                status: 'completed', duration_ms: 700,
                                context_sections: ['## Memory policy']}},
                        {seq: 3, type: 'assistant/reasoning.delta', run_id: 'run-a',
                         time: 108, data: {text: 'thinking'}},
                        {seq: 4, type: 'tool/call.started', run_id: 'run-a', time: 110,
                         data: {call_id: 'call-1', name: 'skills_list', arguments: {}}},
                        {seq: 5, type: 'hook/invoked', run_id: 'run-a', time: 110.5,
                         data: {invocation_id: 'h2', point: 'before_tool',
                                hook_id: 'bianbu.security/security.before_tool'}},
                        {seq: 6, type: 'hook/result', run_id: 'run-a', time: 111.5,
                         data: {invocation_id: 'h2', point: 'before_tool',
                                hook_id: 'bianbu.security/security.before_tool',
                                status: 'completed', duration_ms: 1000,
                                metadata: {securityd: {decision: 'ALLOW'}}}},
                        {seq: 7, type: 'tool/call.started', run_id: 'run-a', time: 110.6,
                         data: {call_id: 'call-2', name: 'search_files', arguments: {}}},
                        {seq: 8, type: 'tool/call.completed', run_id: 'run-a', time: 114,
                         data: {call_id: 'call-1', result: 'ok', status: 'success'}},
                        {seq: 9, type: 'tool/call.completed', run_id: 'run-a', time: 115,
                         data: {call_id: 'call-2', result: 'ok', status: 'success'}},
                        {seq: 10, type: 'assistant/text.delta', run_id: 'run-a',
                         time: 116, data: {text: 'hi'}},
                        {seq: 11, type: 'run/completed', run_id: 'run-a', time: 117,
                         data: {}},
                    ])"""
                )

                turn = trajectory["turns"][0]
                assert turn["duration"] == pytest.approx(17)
                # 100→105 启动、106→108 等模型、115→116 等模型。
                assert turn["waiting"] == pytest.approx(8)
                assert turn["covered"] + turn["waiting"] == pytest.approx(
                    turn["duration"]
                )
                assert [
                    record["wait"]["reason"]
                    for record in turn["records"] if record["kind"] == "wait"
                ] == ["startup", "model", "model"]
                assert [record["kind"] for record in turn["records"]] == [
                    "user", "wait", "hook", "wait", "reasoning", "tool", "hook",
                    "tool", "wait", "assistant", "run",
                ]

                # 每行显示的耗时（含等待行）加起来会大于"耗时"，多出来的正好是
                # 重叠部分：等待行不重叠，所以它们已经被算进了这一份合计里。
                durations = sum(record["duration"] or 0 for record in turn["records"])
                assert durations == pytest.approx(
                    turn["duration"] + turn["overlap"]
                )

                # 工具调用内部的 before_tool Hook：包含关系，时间算在工具里。
                nested_hook = next(record for record in turn["records"]
                                   if record["key"] == "hook:h2")
                assert nested_hook["nested"] is True
                assert nested_hook["overlap_seconds"] == pytest.approx(1)
                assert nested_hook["owner_key"] == "tool:call-1:4"

                # 同一轮里并发跑的第二个工具：与第一个工具重叠但确实多占时间。
                parallel_tool = next(record for record in turn["records"]
                                     if record["key"].startswith("tool:call-2"))
                assert parallel_tool["parallel"] is True
                assert parallel_tool["overlap_seconds"] == pytest.approx(3.4)
                assert turn["overlap"] == pytest.approx(4.4)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_trajectory_projection_excludes_internal_console_runs(
    tmp_path: Path,
) -> None:
    """A hidden title Run must not leak any of its events into Trajectory."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                trajectory = await page.evaluate(
                    """() => BianbuTrajectory.projectTrajectory([
                        {seq: 0, type: 'user/message', run_id: 'title-run', time: 1,
                         data: {content: 'Generate title',
                                console_operation: 'generate_title'}},
                        {seq: 1, type: 'assistant/text.delta', run_id: 'title-run',
                         time: 2, data: {text: 'Hidden title'}},
                        {seq: 2, type: 'run/completed', run_id: 'title-run', time: 3,
                         data: {content: 'Hidden title'}},
                        {seq: 3, type: 'user/message', run_id: 'visible-run', time: 4,
                         data: {content: 'Visible question'}},
                    ])"""
                )
                assert "summary" not in trajectory
                assert trajectory["records"][0]["run_id"] == "visible-run"

                paged = await page.evaluate(
                    """() => {
                        const runtime = new BianbuSessionRuntime.SessionRuntime();
                        const model = runtime.get('paged');
                        model.installHistory([
                            {seq: 10, type: 'user/message', run_id: 'visible-run',
                             time: 10, data: {content: 'Visible question'}},
                            {seq: 11, type: 'assistant/text.delta', run_id: 'title-run',
                             time: 11, data: {text: 'Leaked title'}},
                            {seq: 12, type: 'run/completed', run_id: 'title-run',
                             time: 12, data: {content: 'Leaked title'}},
                        ], {hasMore: true, hiddenRunIds: ['title-run']});
                        let callbacks = 0;
                        const connection = new BianbuSessionRuntime.SessionConnection(
                            runtime, {onEvent: () => { callbacks += 1; }},
                        );
                        connection._message('mux', JSON.stringify({
                            type: 'session/event', session_id: 'paged',
                            event: {seq: 13, type: 'assistant/text.delta',
                                run_id: 'title-run', time: 13,
                                data: {text: 'Still hidden'}},
                        }));
                        return {
                            projection: BianbuTrajectory.projectTrajectory(
                                model.snapshot().events
                            ),
                            callbacks,
                        };
                    }"""
                )
                assert "summary" not in paged["projection"]
                assert paged["projection"]["records"][0]["run_id"] == (
                    "visible-run"
                )
                assert paged["callbacks"] == 0
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_trajectory_projects_context_and_pairs_hooks_without_noise(
    tmp_path: Path,
) -> None:
    """Observability records are semantic, paired, and omit no-op Hooks."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                trajectory = await page.evaluate(
                    """() => BianbuTrajectory.projectTrajectory([
                        {seq: 0, type: 'user/message', run_id: 'run-a', time: 10,
                         data: {content: 'hello'}},
                        {seq: 1, type: 'request/context', run_id: 'run-a', time: 11,
                         data: {reason: 'initial', agent_id: 'com.bianbu.123456789abc',
                                provider: 'bibit', model: 'local',
                                system_prompt: 'You are the selected agent.'}},
                        {seq: 2, type: 'hook/invoked', run_id: 'run-a', time: 12,
                         data: {invocation_id: 'h1', hook_id: 'bianbu.memory/context',
                                plugin_id: 'bianbu.memory', point: 'after_context'}},
                        {seq: 3, type: 'hook/result', run_id: 'run-a', time: 13,
                         data: {invocation_id: 'h1', hook_id: 'bianbu.memory/context',
                                plugin_id: 'bianbu.memory', point: 'after_context',
                                status: 'completed', duration_ms: 4,
                                context_sections: ['Remember dark mode.']}},
                        {seq: 4, type: 'hook/invoked', run_id: 'run-a', time: 14,
                         data: {invocation_id: 'h2', hook_id: 'vendor.noop',
                                plugin_id: 'vendor', point: 'after_run'}},
                        {seq: 5, type: 'hook/result', run_id: 'run-a', time: 15,
                         data: {invocation_id: 'h2', hook_id: 'vendor.noop',
                                plugin_id: 'vendor', point: 'after_run',
                                status: 'completed', duration_ms: 1}},
                        {seq: 6, type: 'run/completed', run_id: 'run-a', time: 16,
                         data: {}},
                    ])"""
                )

                assert [record["kind"] for record in trajectory["records"]] == [
                    "user", "context", "wait", "hook", "wait", "run",
                ]
                context = next(record for record in trajectory["records"]
                               if record["kind"] == "context")
                assert context["title"] == "System prompt"
                assert context["output"] == "You are the selected agent."
                hook = next(record for record in trajectory["records"]
                            if record["key"] == "hook:h1")
                assert hook["title"] == "bianbu.memory/context"
                assert hook["status"] == "completed"
                assert hook["output"] == "Remember dark mode."
                # 行在时间轴上占的是墙钟区间；Hook 自报的耗时留在 data 里对照。
                assert hook["duration"] == pytest.approx(1.0)
                assert hook["data"]["hook_elapsed_ms"] == 4
                assert "vendor.noop" not in str(trajectory)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_trajectory_folds_session_events_and_summarizes_hooks(
    tmp_path: Path,
) -> None:
    """One question is one Turn, and every Hook row shares one readable shape."""

    events = [
        {"seq": 0, "type": "agent/inbox/spliced", "run_id": None, "time": 5,
         "data": {"message": {"message_id": "m1", "content": "hello",
                              "attachments": [], "created_at": 5}}},
        {"seq": 1, "type": "agent/inbox/claimed", "run_id": "run-a", "time": 5,
         "data": {"message_ids": ["m1"]}},
        {"seq": 2, "type": "user/message", "run_id": "run-a", "time": 5,
         "data": {"content": "hello"}},
        {"seq": 3, "type": "hook/invoked", "run_id": "run-a", "time": 6,
         "data": {"invocation_id": "h1",
                  "hook_id": "bianbu.security/security.before_run",
                  "point": "before_run"}},
        {"seq": 4, "type": "hook/result", "run_id": "run-a", "time": 7,
         "data": {"invocation_id": "h1",
                  "hook_id": "bianbu.security/security.before_run",
                  "point": "before_run", "status": "completed", "duration_ms": 739,
                  "metadata": {"securityd": {"stage": "before_run",
                                             "decision": "ALLOW",
                                             "mode": "yolo"}}}},
        {"seq": 5, "type": "hook/invoked", "run_id": "run-a", "time": 7,
         "data": {"invocation_id": "h2",
                  "hook_id": "bianbu.memory/memory.before_run",
                  "point": "before_run"}},
        {"seq": 6, "type": "hook/result", "run_id": "run-a", "time": 8,
         "data": {"invocation_id": "h2",
                  "hook_id": "bianbu.memory/memory.before_run",
                  "point": "before_run", "status": "completed", "duration_ms": 1100,
                  "context_sections": ["## Memory policy\nBody."]}},
        {"seq": 7, "type": "run/event", "run_id": "run-a", "time": 8,
         "data": {"kind": "usage",
                  "payload": {"prompt_tokens": 12, "completion_tokens": 3}}},
        {"seq": 8, "type": "run/completed", "run_id": "run-a", "time": 9,
         "data": {}},
    ]

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                projection = await page.evaluate(
                    "(events) => BianbuTrajectory.projectTrajectory(events)", events,
                )

                # Run-less inbox events belong to the Run that follows them: one
                # question stays one Turn instead of a Turn plus a stray row.
                assert len(projection["turns"]) == 1
                assert [record["kind"] for record in projection["records"]] == [
                    "event", "event", "user", "wait", "hook", "hook", "event",
                    "wait", "run",
                ]
                assert "[object Object]" not in json.dumps(projection)
                assert projection["records"][0]["preview"] == "Inbox queued · hello"
                # An Event row that has data shows that data instead of
                # repeating its own event name.
                assert projection["records"][6]["preview"] == (
                    "Run event · usage · prompt_tokens=12 · completion_tokens=3"
                )

                security = next(record for record in projection["records"]
                                if record["key"] == "hook:h1")
                assert security["hook"]["name"] == "security.before_run"
                assert security["preview"] == "ALLOW · mode=yolo"
                assert security["output"] == ""
                assert security["data"]["metadata"]["securityd"]["mode"] == "yolo"
                memory = next(record for record in projection["records"]
                              if record["key"] == "hook:h2")
                assert memory["hook"]["name"] == "memory.before_run"
                assert memory["hook"]["sections"] == ["## Memory policy\nBody."]
                assert memory["preview"] == "context +1 · ## Memory policy Body."
                assert memory["output"] == "## Memory policy\nBody."
                turn = projection["turns"][0]
                assert turn["covered"] + turn["waiting"] == pytest.approx(
                    turn["duration"]
                )

                await page.evaluate(
                    """(events) => {
                        sessionRuntime.get(sessionId).installHistory(
                            events, {hasMore: false});
                        renderSessionModelSnapshot(sessionId);
                        setConversationView('trajectory');
                    }""",
                    events,
                )
                await expect(page.locator(".trajectory-turn")).to_have_count(1)
                row = page.locator(
                    '.trajectory-record[data-trajectory-key="hook:h1"]'
                )
                await expect(row.locator(".trajectory-record-kind")).to_have_text(
                    "security.before_run"
                )
                await expect(row.locator(".trajectory-record-preview")).to_have_text(
                    "ALLOW · mode=yolo"
                )
                await expect(
                    page.locator('.trajectory-record[data-trajectory-key="hook:h2"]')
                    .locator(".trajectory-record-preview")
                ).to_have_text("context +1 · ## Memory policy Body.")
                await page.locator(
                    '.trajectory-record[data-trajectory-key="hook:h1"]'
                ).click()
                await expect(page.locator("#trajectory-inspector")).to_contain_text(
                    "bianbu.security/security.before_run"
                )
                await expect(page.locator("#trajectory-inspector")).to_contain_text(
                    "yolo"
                )
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_trajectory_tab_renders_searchable_safe_record_inspector(
    tmp_path: Path,
) -> None:
    """The trajectory ledger is a separate accessible view over Chat events."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate(
                    """() => {
                        sessionRuntime.get(sessionId).installHistory([
                            {seq: 0, type: 'user/message', run_id: 'run-a', time: 10,
                             data: {content: 'Find <unsafe> weather'}},
                            {seq: 1, type: 'assistant/reasoning.delta', run_id: 'run-a',
                             time: 11, data: {text: 'check forecast'}},
                            {seq: 2, type: 'tool/call.started', run_id: 'run-a', time: 13,
                             data: {call_id: 'call-1', name: 'weather',
                                    arguments: {city: 'Nanjing'}}},
                            {seq: 3, type: 'tool/call.completed', run_id: 'run-a', time: 15,
                             data: {call_id: 'call-1', result: 'sunny', status: 'success'}},
                            {seq: 4, type: 'assistant/text.delta', run_id: 'run-a', time: 16,
                             data: {text: 'It is sunny.'}},
                            {seq: 5, type: 'run/completed', run_id: 'run-a', time: 18,
                             data: {usage: {input_tokens: 12, output_tokens: 5,
                                           total_tokens: 17}}},
                        ], {hasMore: true});
                        renderSessionModelSnapshot(sessionId);
                    }"""
                )

                tabs = page.locator("#conversation-view-tabs")
                await expect(tabs).to_have_attribute("role", "tablist")
                await page.locator("#conversation-tab-trajectory").click()
                await expect(page.locator("#trajectory-view")).to_be_visible()
                await expect(page.locator("#chat-messages")).to_be_hidden()
                await expect(page.locator("#chat-input")).to_be_visible()
                await expect(page.locator(".trajectory-turn")).to_have_count(1)
                # 5 条事件记录 + 2 条等待行（10s→11s 启动、15s→16s 等模型）。
                await expect(page.locator(".trajectory-record")).to_have_count(7)
                await expect(page.locator("#trajectory-summary")).to_have_count(0)
                await expect(page.locator(".trajectory-turn > header")).to_contain_text(
                    "耗时 8.0s · 等待 2.0s"
                )
                await expect(
                    page.locator('.trajectory-record[data-trajectory-key^="tool:"]')
                    .locator(".trajectory-record-metric")
                ).to_have_text(re.compile(r"success.*2\.0s"))

                # 等待行要能自己解释"这段时间没有记录"，并带上阶段标签与破折号序号。
                wait_rows = page.locator(".trajectory-record.kind-wait")
                await expect(wait_rows).to_have_count(2)
                await expect(wait_rows.first).to_contain_text("启动")
                await expect(wait_rows.first).to_contain_text(
                    "No events for 1.00s"
                )
                await expect(
                    wait_rows.first.locator(".trajectory-record-index")
                ).to_have_text("—")
                await expect(wait_rows.nth(1)).to_contain_text("等待模型")
                # 一轮的账在界面上当场可验算：记录覆盖 + 等待 = 耗时。
                await expect(page.locator(".trajectory-turn-total")).to_have_text(
                    "记录覆盖 6.0s + 等待 2.0s = 8.0s"
                )

                search = page.locator("#trajectory-search")
                await expect(search).to_have_attribute("role", "searchbox")
                await search.fill("Nanjing")
                await expect(page.locator(".trajectory-record")).to_have_count(1)
                await expect(page.locator(".trajectory-record")).to_contain_text(
                    "weather"
                )
                await page.locator(".trajectory-record").click()
                inspector = page.locator("#trajectory-inspector")
                await expect(inspector).to_be_visible()
                await expect(inspector).to_contain_text("Nanjing")
                await expect(inspector).to_contain_text("sunny")

                await search.fill("unsafe")
                await page.locator(".trajectory-record").click()
                await expect(inspector).to_contain_text("<unsafe>")
                await expect(inspector.locator("unsafe")).to_have_count(0)

                await page.locator("#conversation-tab-chat").click()
                await expect(page.locator("#chat-messages")).to_be_visible()
                await expect(page.locator("#trajectory-view")).to_be_hidden()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_trajectory_session_switch_clears_local_filter_and_selection(
    tmp_path: Path,
) -> None:
    """Trajectory-local state must never leak between Session projections."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate(
                    """() => {
                        sessionRuntime.get(sessionId).installHistory([
                            {seq: 0, type: 'user/message', run_id: 'run-a', time: 1,
                             data: {content: 'alpha question'}},
                        ], {hasMore: false});
                        sessionRuntime.get('other-session').installHistory([
                            {seq: 0, type: 'user/message', run_id: 'run-b', time: 2,
                             data: {content: 'beta question'}},
                        ], {hasMore: false});
                        renderSessionModelSnapshot(sessionId);
                        setConversationView('trajectory');
                    }"""
                )
                await page.locator("#trajectory-search").fill("alpha")
                await page.locator(".trajectory-record").click()
                await expect(page.locator("#trajectory-inspector")).to_be_visible()

                await page.evaluate("() => switchSession('other-session')")

                await expect(page.locator("#trajectory-search")).to_have_value("")
                await expect(page.locator(".trajectory-record")).to_have_count(1)
                await expect(page.locator(".trajectory-record")).to_contain_text(
                    "beta question"
                )
                await expect(page.locator("#trajectory-inspector")).to_be_hidden()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_trajectory_gap_repair_renders_authoritative_history(
    tmp_path: Path,
) -> None:
    """An active Trajectory must repaint when seq-gap repair installs a tail."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                await page.route(
                    "**/api/v1/sessions/*/history*",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "data": [
                                    {
                                        "seq": 0,
                                        "type": "user/message",
                                        "run_id": "run-a",
                                        "time": 1,
                                        "data": {"content": "question"},
                                    },
                                    {
                                        "seq": 1,
                                        "type": "run/completed",
                                        "run_id": "run-a",
                                        "time": 2,
                                        "data": {},
                                    },
                                ],
                                "base_seq": 0,
                                "last_seq": 1,
                                "has_more": False,
                            }
                        ),
                    ),
                )
                await page.evaluate(
                    """() => {
                        sessionRuntime.get(sessionId).installHistory([
                            {seq: 0, type: 'user/message', run_id: 'run-a', time: 1,
                             data: {content: 'question'}},
                        ], {hasMore: false});
                        renderSessionModelSnapshot(sessionId);
                        setConversationView('trajectory');
                    }"""
                )
                await expect(page.locator(".trajectory-record")).to_have_count(1)

                await page.evaluate("() => { void openSessionHistory(sessionId, true); }")

                # 用户事件 + 收尾等待行 + Run 行：这段时间没有别的记录。
                await expect(page.locator(".trajectory-record")).to_have_count(3)
                await expect(page.locator(".trajectory-turn > header")).to_contain_text(
                    "completed"
                )
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_trajectory_live_update_and_history_prepend_preserve_selection(
    tmp_path: Path,
) -> None:
    """Live tail and older pages share one projection without losing inspection."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                await page.route(
                    "**/api/v1/sessions/*/history*",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "data": [
                                    {
                                        "seq": 0,
                                        "type": "user/message",
                                        "run_id": "older-run",
                                        "time": 1,
                                        "data": {"content": "older question"},
                                    },
                                    {
                                        "seq": 1,
                                        "type": "run/completed",
                                        "run_id": "older-run",
                                        "time": 2,
                                        "data": {},
                                    },
                                ],
                                "base_seq": 0,
                                "last_seq": 14,
                                "has_more": False,
                            }
                        ),
                    ),
                )
                await page.evaluate(
                    """() => {
                        sessionRuntime.get(sessionId).installHistory([
                            {seq: 10, type: 'user/message', run_id: 'run-a', time: 10,
                             data: {content: 'current question'}},
                            {seq: 11, type: 'tool/call.started', run_id: 'run-a', time: 11,
                             data: {call_id: 'call-1', name: 'lookup', arguments: {q: 'x'}}},
                            {seq: 12, type: 'tool/call.completed', run_id: 'run-a', time: 12,
                             data: {call_id: 'call-1', result: 'found', status: 'success'}},
                        ], {hasMore: true});
                        renderSessionModelSnapshot(sessionId);
                        setConversationView('trajectory');
                    }"""
                )
                tool = page.locator('.trajectory-record[data-trajectory-key^="tool:"]')
                await tool.click()
                await expect(tool).to_have_attribute("aria-pressed", "true")
                await expect(page.locator("#trajectory-inspector")).to_contain_text(
                    "found"
                )

                await page.evaluate(
                    """() => {
                        const model = sessionRuntime.get(sessionId);
                        model.acceptLive({seq: 13, type: 'assistant/text.delta',
                            run_id: 'run-a', time: 13, data: {text: 'live answer'}});
                        model.acceptLive({seq: 14, type: 'run/completed',
                            run_id: 'run-a', time: 14, data: {}});
                        scheduleSessionModelSnapshot(sessionId, true);
                    }"""
                )
                # 用户、等待启动、工具、等待模型、助手、Run：6 条。
                await expect(page.locator(".trajectory-record")).to_have_count(6)
                tool = page.locator('.trajectory-record[data-trajectory-key^="tool:"]')
                await expect(tool).to_have_attribute("aria-pressed", "true")
                await expect(page.locator("#trajectory-inspector")).to_contain_text(
                    "found"
                )

                await page.locator("#trajectory-load-older").click()
                await expect(page.locator(".trajectory-turn")).to_have_count(2)
                await expect(page.locator("#trajectory-load-older")).to_be_hidden()
                tool = page.locator('.trajectory-record[data-trajectory-key^="tool:"]')
                await expect(tool).to_have_attribute("aria-pressed", "true")
                await expect(page.locator("#trajectory-inspector")).to_contain_text(
                    "found"
                )
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_live_assistant_stream_refreshes_one_bubble_in_event_order(
    tmp_path: Path,
) -> None:
    """A stream must paint intermediate blocks without duplicate bot bubbles."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                await page.locator("#chat-input").fill("ordered live assistant stream")
                await page.locator("#send-btn").click()

                thinking = page.locator(".agent-thinking-step")
                await expect(thinking).to_have_count(1)
                await expect(thinking.first).to_contain_text("plan one")
                await expect(thinking.first).to_have_class(
                    re.compile(r"\bexpanded\b")
                )
                # The placeholder is useful only before the first visible AI
                # event. Once reasoning exists it would be a redundant second
                # bubble below the real reply.
                await expect(page.locator(".chat-loading-indicator")).to_have_count(
                    0, timeout=500
                )
                await expect(page.locator(".bot-message-group")).to_have_count(1)
                running = page.locator(
                    ".bot-message-group .run-progress-indicator"
                )
                await expect(running).to_have_count(1)
                await expect(running).to_have_attribute("role", "timer")
                await expect(running).to_have_attribute("aria-live", "off")
                await expect(running).to_have_text(re.compile(r"^running\.\.\.\. \d+s$"))
                initial_elapsed = int(
                    re.search(r"(\d+)s$", await running.text_content()).group(1)
                )
                await page.wait_for_function(
                    r"""initial => {
                        const text = document.querySelector(
                            '.run-progress-indicator'
                        )?.textContent || '';
                        const match = text.match(/(\d+)s$/);
                        return match && Number(match[1]) > initial;
                    }""",
                    arg=initial_elapsed,
                    timeout=2500,
                )
                await page.evaluate(
                    """() => {
                        window.__stableRunningIndicator =
                            document.querySelector('.run-progress-indicator');
                        _markRunTerminal(sessionId, 'unrelated-run');
                    }"""
                )
                assert await page.evaluate(
                    """() => window.__stableRunningIndicator ===
                        document.querySelector('.run-progress-indicator')"""
                ) is True
                await page.evaluate(
                    """() => {
                        window.__stableStreamingNodes = {
                            user: document.querySelector('.user-message-group'),
                            bot: document.querySelector('.bot-message-group'),
                            thinking: document.querySelector('.agent-thinking-step'),
                        };
                    }"""
                )

                await expect(page.locator(".answer-content")).to_have_text("answer one")
                # The first reasoning phase ended as soon as answer content
                # arrived, even though the overall Run is still active.
                await expect(thinking.first).not_to_have_class(
                    re.compile(r"\bexpanded\b"), timeout=500
                )
                await expect(page.locator(".agent-tool-step")).to_have_count(1)
                await page.locator(".agent-tool-step .tool-header").click()
                await expect(page.locator(".agent-tool-step")).to_have_class(
                    re.compile(r"\bexpanded\b")
                )
                await page.evaluate(
                    """() => {
                        window.__stableStreamingNodes.tool =
                            document.querySelector('.agent-tool-step');
                    }"""
                )
                await expect(page.locator(".agent-thinking-step")).to_have_count(2)
                const_thinking = page.locator(".agent-thinking-step")
                await expect(const_thinking.nth(0)).not_to_have_class(
                    re.compile(r"\bexpanded\b"), timeout=500
                )
                await expect(const_thinking.nth(1)).to_have_class(
                    re.compile(r"\bexpanded\b"), timeout=500
                )
                # Reopening a completed phase is a user decision. Later
                # streaming updates may collapse the active phase, but must
                # not close this previously completed one again.
                await const_thinking.nth(0).locator(".thinking-header").click()
                await expect(page.locator(".answer-content")).to_have_text("answer two")
                await expect(const_thinking.nth(0)).to_have_class(
                    re.compile(r"\bexpanded\b")
                )
                await expect(const_thinking.nth(1)).not_to_have_class(
                    re.compile(r"\bexpanded\b")
                )
                await const_thinking.nth(0).locator(".thinking-header").click()
                await expect(page.locator(".bot-message-group")).to_have_count(1)
                await page.wait_for_function("() => !activeRequestId")
                await expect(page.locator(".chat-loading-indicator")).to_have_count(0)
                await expect(page.locator(".run-progress-indicator")).to_have_count(0)
                assert await page.evaluate(
                    """() => {
                        const saved = window.__stableStreamingNodes;
                        return saved.user === document.querySelector('.user-message-group')
                            && saved.bot === document.querySelector('.bot-message-group')
                            && saved.thinking === document.querySelector('.agent-thinking-step')
                            && saved.tool === document.querySelector('.agent-tool-step')
                            && saved.tool.classList.contains('expanded');
                    }"""
                ) is True

                order = await page.locator(
                    ".bot-message-group .agent-steps > .agent-step"
                ).evaluate_all(
                    """nodes => nodes.map(node =>
                        node.classList.contains('agent-thinking-step') ? 'thinking' :
                        node.classList.contains('agent-content-step') ? 'content' :
                        node.classList.contains('agent-tool-step') ? 'tool' : 'unknown')"""
                )
                assert order == ["thinking", "content", "tool", "thinking"]
                await expect(page.locator(".agent-thinking-step.expanded")).to_have_count(0)

                other_session = str((await _create_session(
                    page, base_url, title="Other"
                ))["id"])
                active_session = await page.evaluate("() => sessionId")
                await page.evaluate("sid => switchSession(sid)", other_session)
                await expect(page.locator(".bot-message-group")).to_have_count(0)
                await page.evaluate("sid => switchSession(sid)", active_session)
                await expect(page.locator(".bot-message-group")).to_have_count(1)
                await expect(page.locator(".answer-content")).to_have_text("answer two")
                assert await page.locator(
                    ".bot-message-group .agent-steps > .agent-step"
                ).evaluate_all(
                    """nodes => nodes.map(node =>
                        node.classList.contains('agent-thinking-step') ? 'thinking' :
                        node.classList.contains('agent-content-step') ? 'content' :
                        node.classList.contains('agent-tool-step') ? 'tool' : 'unknown')"""
                ) == ["thinking", "content", "tool", "thinking"]

                await page.reload(wait_until="domcontentloaded")
                await expect(page.locator(".bot-message-group")).to_have_count(1)
                await expect(page.locator(".answer-content")).to_have_text("answer two")
                assert await page.locator(
                    ".bot-message-group .agent-steps > .agent-step"
                ).evaluate_all(
                    """nodes => nodes.map(node =>
                        node.classList.contains('agent-thinking-step') ? 'thinking' :
                        node.classList.contains('agent-content-step') ? 'content' :
                        node.classList.contains('agent-tool-step') ? 'tool' : 'unknown')"""
                ) == ["thinking", "content", "tool", "thinking"]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_running_timer_survives_page_refresh_until_run_finishes(
    tmp_path: Path,
) -> None:
    """Refreshing an active reply must restore its elapsed-time indicator."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                await page.locator("#chat-input").fill("running timer refresh gate")
                await page.locator("#send-btn").click()
                await expect(page.locator(".answer-content")).to_have_text(
                    "RUNNING_TIMER_READY"
                )
                await expect(page.locator(".run-progress-indicator")).to_have_count(1)
                await page.wait_for_function(
                    r"""() => Number(
                        document.querySelector('.run-progress-indicator')
                            ?.textContent.match(/(\d+)s$/)?.[1] || 0
                    ) >= 1"""
                )
                before_reload = int(
                    re.search(
                        r"(\d+)s$",
                        await page.locator(".run-progress-indicator").text_content(),
                    ).group(1)
                )

                await page.reload(wait_until="domcontentloaded")

                restored = page.locator(".run-progress-indicator")
                await expect(restored).to_have_count(1, timeout=800)
                await expect(restored).to_have_text(
                    re.compile(r"^running\.\.\.\. \d+s$")
                )
                after_reload = int(
                    re.search(r"(\d+)s$", await restored.text_content()).group(1)
                )
                assert after_reload >= before_reload
                await expect(page.locator(".answer-content")).to_contain_text(
                    "RUNNING_TIMER_DONE", timeout=5000
                )
                await expect(restored).to_have_count(0)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_restored_approval_does_not_show_running_timer(tmp_path: Path) -> None:
    """A run waiting for approval is not actively running model work."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                await page.locator("#chat-input").fill("followup approval gate")
                await page.locator("#send-btn").click()
                await expect(page.locator(".approval-card")).to_have_count(1)
                await expect(page.locator(".run-progress-indicator")).to_have_count(0)
                run_id = await page.evaluate(
                    "() => sessionActiveRequest[sessionId]"
                )

                async def delay_run_snapshot(route) -> None:
                    await asyncio.sleep(1.0)
                    await route.continue_()

                await page.route(
                    f"**/api/v1/runs/{run_id}", delay_run_snapshot
                )

                await page.reload(wait_until="domcontentloaded")

                await expect(page.locator(".approval-card")).to_have_count(1)
                await expect(page.locator(".run-progress-indicator")).to_have_count(
                    0, timeout=100
                )
                await page.wait_for_timeout(1100)
                await expect(page.locator(".run-progress-indicator")).to_have_count(0)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_approval_card_uses_assistant_message_alignment(tmp_path: Path) -> None:
    """Approval is an Agent message, not a full-width chat-panel card."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 390, "height": 844})
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                await page.locator("#chat-input").fill("followup approval gate")
                await page.locator("#send-btn").click()

                approval = page.locator(
                    "#chat-messages > .approval-message-group"
                )
                await expect(approval).to_have_count(1)
                await expect(approval.locator(":scope > img")).to_have_count(1)
                await expect(
                    approval.locator(":scope > .approval-message-content > .approval-card")
                ).to_have_count(1)

                alignment = await page.evaluate(
                    """() => {
                        const bot = document.querySelector(
                            '#chat-messages > .bot-message-group:not(.approval-message-group)'
                        );
                        const approval = document.querySelector(
                            '#chat-messages > .approval-message-group'
                        );
                        const botAvatar = bot?.querySelector(':scope > img');
                        const approvalAvatar = approval?.querySelector(':scope > img');
                        const botContent = bot?.querySelector(':scope > div');
                        const approvalContent = approval?.querySelector(
                            ':scope > .approval-message-content'
                        );
                        return {
                            groupLeft: [
                                bot?.getBoundingClientRect().left,
                                approval?.getBoundingClientRect().left,
                            ],
                            avatarLeft: [
                                botAvatar?.getBoundingClientRect().left,
                                approvalAvatar?.getBoundingClientRect().left,
                            ],
                            contentLeft: [
                                botContent?.getBoundingClientRect().left,
                                approvalContent?.getBoundingClientRect().left,
                            ],
                            contentRight: [
                                botContent?.getBoundingClientRect().right,
                                approvalContent?.getBoundingClientRect().right,
                            ],
                        };
                    }"""
                )
                for pair in alignment.values():
                    assert pair[0] == pytest.approx(pair[1], abs=1)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_cancelled_run_removes_running_timer(tmp_path: Path) -> None:
    """Cancelling active model work must remove its elapsed-time indicator."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                await page.locator("#chat-input").fill("followup hold gate")
                await page.locator("#send-btn").click()
                await expect(page.locator(".answer-content")).to_have_text(
                    "FOLLOWUP_HOLD_READY"
                )
                await expect(page.locator(".run-progress-indicator")).to_have_count(1)

                await page.locator("#stop-btn").click()

                await page.wait_for_function("() => !activeRequestId")
                await expect(page.locator(".run-progress-indicator")).to_have_count(0)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_message_end_collapses_active_reasoning_before_the_run_finishes(
    tmp_path: Path,
) -> None:
    """Ignoring message.end leaves a completed reasoning phase marked active."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                result = await page.evaluate(
                    """() => {
                        const sid = 'reasoning-message-end';
                        sessionId = sid;
                        sessionRuntime.select(sid);
                        const model = sessionRuntime.get(sid);
                        model.installHistory([
                            {seq: 0, type: 'user/message', run_id: 'run', time: 1,
                             data: {content: 'question'}},
                            {seq: 1, type: 'assistant/reasoning.delta', run_id: 'run',
                             time: 2, data: {text: 'finished thought'}},
                        ]);
                        renderSessionModelSnapshot(sid);
                        const node = document.querySelector('.agent-thinking-step');
                        const before = node.classList.contains('expanded');
                        model.acceptLive({
                            seq: 2, type: 'assistant/message.end', run_id: 'run',
                            time: 3, data: {},
                        });
                        renderLiveSessionModelSnapshot(sid);
                        return {
                            before,
                            stable: node === document.querySelector('.agent-thinking-step'),
                            after: node.classList.contains('expanded'),
                            summary: node.querySelector('.thinking-summary').textContent,
                        };
                    }"""
                )
                assert result == {
                    "before": True,
                    "stable": True,
                    "after": False,
                    "summary": "已深度思考",
                }
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_assistant_content_before_trailing_tool_keeps_dom_order(
    tmp_path: Path,
) -> None:
    """Hoisting content below a later tool must break this ordering contract."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                order = await page.evaluate(
                    """() => {
                        const root = createBotMessageEl('', new Date(), 'ordered-tool', {
                            streaming: false,
                            steps: [
                                {type: 'content', content: 'before tool'},
                                {type: 'tool', tool_call_id: 'tool-1', name: 'lookup',
                                 arguments: {}, result: 'done', status: 'success'},
                            ],
                        });
                        document.querySelector('#chat-messages').appendChild(root);
                        return [...root.querySelector('.msg-content').children]
                            .flatMap(container => container.classList.contains('agent-steps')
                                ? [...container.children] : [container])
                            .filter(node => node.classList.contains('agent-step')
                                || node.classList.contains('answer-content'))
                            .map(node => node.classList.contains('agent-content-step')
                                ? 'content' : node.classList.contains('agent-tool-step')
                                ? 'tool' : node.classList.contains('answer-content')
                                && node.textContent.trim() ? 'content' : 'empty');
                    }"""
                )
                assert order == ["content", "tool", "empty"]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_incremental_bot_patch_adds_live_media_artifacts_and_audio(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                result = await page.evaluate(
                    """() => {
                        const root = createBotMessageEl('answer', new Date(), 'media-run', {
                            content: 'answer', segment_id: 'media-run:1', streaming: true,
                        });
                        document.querySelector('#chat-messages').appendChild(root);
                        const patched = patchBotMessageElement(root, {
                            content: 'answer', segment_id: 'media-run:1', streaming: false,
                            steps: [{type: 'tool', tool_call_id: 'tool-1', name: 'image',
                                arguments: {}, status: 'success', result: JSON.stringify({
                                    type: 'file_to_send', file_type: 'image',
                                    path: '/api/v1/files/live.png',
                                })}],
                            artifacts: [{name: 'report.txt', path: '/api/v1/files/report.txt',
                                media_type: 'text/plain'}],
                            extras: {audio: {url: '/api/v1/files/live.mp3'}},
                        }, new Date());
                        return {
                            stable: patched === root,
                            toolMedia: root.querySelectorAll('.stored-tool-media img').length,
                            artifacts: root.querySelectorAll('.stored-artifacts .agent-file-artifact')
                                .length,
                            audio: new URL(root.querySelector('.bot-audio-slot audio').src)
                                .pathname,
                        };
                    }"""
                )
                assert result == {
                    "stable": True,
                    "toolMedia": 1,
                    "artifacts": 1,
                    "audio": "/api/v1/files/live.mp3",
                }
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_live_stream_does_not_reclaim_scroll_after_user_moves_up(
    tmp_path: Path,
) -> None:
    """Forcing the transcript to the bottom on each delta must break this test."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 900, "height": 600})
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                await page.locator("#chat-input").fill("ordered live assistant stream")
                await page.locator("#send-btn").click()
                await expect(page.locator(".agent-thinking-step")).to_contain_text("plan one")
                await page.locator("#chat-messages").evaluate(
                    """host => {
                        for (let index = 0; index < 30; index += 1) {
                            const row = document.createElement('div');
                            row.style.height = '80px';
                            row.textContent = `older reasoning ${index}`;
                            host.prepend(row);
                        }
                        host.scrollTop = 0;
                        host.dispatchEvent(new Event('scroll'));
                    }"""
                )
                await expect(page.locator(".answer-content")).to_have_text("answer one")
                upward_position = await page.locator("#chat-messages").evaluate(
                    """host => ({
                        top: host.scrollTop,
                        distance: host.scrollHeight - host.scrollTop - host.clientHeight,
                    })"""
                )
                assert upward_position["top"] <= 1
                assert upward_position["distance"] > 80

                dragged_position = await page.locator("#chat-messages").evaluate(
                    """host => {
                        host.scrollTop = Math.min(480, host.scrollHeight
                            - host.clientHeight - 160);
                        host.dispatchEvent(new Event('scroll'));
                        return host.scrollTop;
                    }"""
                )
                assert dragged_position > 1
                await expect(page.locator(".answer-content")).to_have_text("answer two")
                final_position = await page.locator("#chat-messages").evaluate(
                    "host => host.scrollTop"
                )
                assert abs(final_position - dragged_position) <= 1
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_live_renderer_keeps_steered_assistant_segments_distinct(
    tmp_path: Path,
) -> None:
    """A bare run-id key must not patch the first bubble after steering."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _prepare_default_draft(page)
                result = await page.evaluate(
                    """() => {
                        const sid = sessionId;
                        const model = sessionRuntime.get(sid);
                        model.installHistory([
                            {seq: 0, type: 'user/message', run_id: 'run', time: 1,
                             data: {content: 'question'}},
                            {seq: 1, type: 'assistant/text.delta', run_id: 'run', time: 2,
                             data: {text: 'first answer'}},
                            {seq: 2, type: 'agent/inbox/spliced', run_id: null, time: 3,
                             data: {message: {message_id: 'message-1',
                                 content: 'continue', attachments: [], created_at: 3}}},
                            {seq: 3, type: 'user/message', run_id: 'run', time: 4,
                             data: {message_id: 'message-1', content: 'continue'}},
                            {seq: 4, type: 'assistant/reasoning.delta', run_id: 'run', time: 5,
                             data: {text: 'second thought'}},
                        ]);
                        renderSessionModelSnapshot(sid);
                        const before = [...document.querySelectorAll('.bot-message-group')];
                        model.acceptLive({
                            seq: 5, type: 'assistant/text.delta', run_id: 'run', time: 6,
                            data: {text: 'second answer'},
                        });
                        renderLiveSessionModelSnapshot(sid);
                        const after = [...document.querySelectorAll('.bot-message-group')];
                        return {
                            count: after.length,
                            stable: before[0] === after[0] && before[1] === after[1],
                            first: after[0]?.querySelector('.answer-content')?.textContent.trim(),
                            secondThinking: after[1]?.querySelector('.agent-thinking-step')
                                ?.textContent.includes('second thought'),
                            second: after[1]?.querySelector('.answer-content')?.textContent.trim(),
                        };
                    }"""
                )
                assert result == {
                    "count": 2,
                    "stable": True,
                    "first": "first answer",
                    "secondThinking": True,
                    "second": "second answer",
                }
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_steered_terminal_only_content_creates_final_assistant_segment(
    tmp_path: Path,
) -> None:
    """Terminal content after steering belongs to the new assistant segment."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                messages = await page.evaluate(
                    """() => {
                        const model = sessionRuntime.get(sessionId);
                        model.installHistory([
                            {seq: 0, type: 'user/message', run_id: 'run', time: 1,
                             data: {content: 'question'}},
                            {seq: 1, type: 'assistant/text.delta', run_id: 'run', time: 2,
                             data: {text: 'first answer'}},
                            {seq: 2, type: 'agent/inbox/spliced', run_id: null, time: 3,
                             data: {message: {message_id: 'message-1',
                                 content: 'continue', attachments: [], created_at: 3}}},
                            {seq: 3, type: 'user/message', run_id: 'run', time: 4,
                             data: {message_id: 'message-1', content: 'continue'}},
                            {seq: 4, type: 'run/completed', run_id: 'run', time: 5,
                             data: {content: 'terminal second answer'}},
                        ]);
                        return model.snapshot().messages.map(message => ({
                            role: message.role,
                            content: message.content,
                            streaming: message.streaming,
                        }));
                    }"""
                )
                assert messages == [
                    {"role": "user", "content": "question", "streaming": None},
                    {"role": "assistant", "content": "first answer", "streaming": False},
                    {"role": "user", "content": "continue", "streaming": None},
                    {
                        "role": "assistant",
                        "content": "terminal second answer",
                        "streaming": False,
                    },
                ]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_gap_history_repair_preserves_nodes_and_scroll_position(
    tmp_path: Path,
) -> None:
    """Repairing a mux sequence gap must not rebuild or force-scroll the chat."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 900, "height": 600})
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                repaired_events: list[dict[str, object]] = []
                for index in range(24):
                    run_id = f"run-{index}"
                    repaired_events.extend([
                        {
                            "seq": len(repaired_events), "type": "user/message",
                            "run_id": run_id, "time": len(repaired_events) + 1,
                            "data": {"content": f"question {index}"},
                        },
                        {
                            "seq": len(repaired_events) + 1,
                            "type": "assistant/text.delta", "run_id": run_id,
                            "time": len(repaired_events) + 2,
                            "data": {"text": f"answer {index}"},
                        },
                        {
                            "seq": len(repaired_events) + 2,
                            "type": "run/completed", "run_id": run_id,
                            "time": len(repaired_events) + 3,
                            "data": {"content": f"answer {index}"},
                        },
                    ])
                repaired_events.extend([
                    {
                        "seq": len(repaired_events), "type": "user/message",
                        "run_id": "repair-run", "time": len(repaired_events) + 1,
                        "data": {"content": "repair question"},
                    },
                    {
                        "seq": len(repaired_events) + 1,
                        "type": "assistant/text.delta", "run_id": "repair-run",
                        "time": len(repaired_events) + 2,
                        "data": {"text": "repair answer"},
                    },
                ])

                async def repair_route(route) -> None:
                    await route.fulfill(json={
                        "data": repaired_events,
                        "has_more": False,
                    })

                await page.route(
                    "**/api/v1/sessions/gap-repair-session/history*",
                    repair_route,
                )
                result = await page.evaluate(
                    """async () => {
                        const sid = 'gap-repair-session';
                        sessionId = sid;
                        sessionRuntime.select(sid);
                        const events = [];
                        for (let index = 0; index < 24; index += 1) {
                            const runId = `run-${index}`;
                            events.push({seq: events.length, type: 'user/message',
                                run_id: runId, time: events.length + 1,
                                data: {content: `question ${index}`}});
                            events.push({seq: events.length, type: 'assistant/text.delta',
                                run_id: runId, time: events.length + 1,
                                data: {text: `answer ${index}`}});
                            events.push({seq: events.length, type: 'run/completed',
                                run_id: runId, time: events.length + 1,
                                data: {content: `answer ${index}`}});
                        }
                        const model = sessionRuntime.get(sid);
                        model.installHistory(events);
                        renderSessionModelSnapshot(sid);
                        const firstBot = document.querySelector('.bot-message-group');
                        messagesDiv.scrollTop = 0;
                        messagesDiv.dispatchEvent(new Event('scroll'));
                        const repaired = [...events,
                            {seq: events.length, type: 'user/message',
                             run_id: 'repair-run', time: events.length + 1,
                             data: {content: 'repair question'}},
                            {seq: events.length + 1, type: 'assistant/text.delta',
                             run_id: 'repair-run', time: events.length + 2,
                             data: {text: 'repair answer'}},
                        ];
                        await openSessionHistory(sid, true);
                        return {
                            stable: firstBot === document.querySelector('.bot-message-group'),
                            top: messagesDiv.scrollTop,
                            distance: messagesDiv.scrollHeight - messagesDiv.scrollTop
                                - messagesDiv.clientHeight,
                            repaired: messagesDiv.textContent.includes('repair answer'),
                        };
                    }"""
                )
                assert result["stable"] is True
                assert result["top"] <= 1
                assert result["distance"] > 80
                assert result["repaired"] is True
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_folded_history_window_advances_the_live_cursor(tmp_path: Path) -> None:
    """A folded window must not read its own covered span as a mux gap."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 900, "height": 600})
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                result = await page.evaluate(
                    """() => {
                        const model = sessionRuntime.get('folded-session');
                        model.installHistory([
                            {seq: 0, type: 'user/message', run_id: 'run-a', time: 1,
                             data: {content: 'question'}},
                            {seq: 1, type: 'assistant/reasoning.delta', run_id: 'run-a',
                             time: 2, data: {text: 'plan', seq_range: [1, 40]}},
                        ], {hasMore: false});
                        const inside = model.acceptLive({
                            seq: 30, type: 'assistant/text.delta', run_id: 'run-a',
                            time: 3, data: {text: 'stale'}});
                        const next = model.acceptLive({
                            seq: 41, type: 'assistant/text.delta', run_id: 'run-a',
                            time: 4, data: {text: 'live'}});
                        const gap = model.acceptLive({
                            seq: 60, type: 'assistant/text.delta', run_id: 'run-a',
                            time: 5, data: {text: 'gap'}});
                        return {
                            base_seq: model.baseSeq,
                            last_seq: model.lastSeq,
                            inside, next, gap,
                            text: model.state.messages.at(-1).reasoning,
                        };
                    }"""
                )

                # 折叠组的 seq 只是组内第一条，实时游标必须落在 seq_range 末尾。
                assert result["base_seq"] == 0
                assert result["last_seq"] == 41
                assert result["inside"] == "duplicate"
                assert result["next"] == "applied"
                assert result["gap"] == "gap"
                assert result["text"] == "plan"
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_reasoning_and_answer_survive_a_page_refresh(tmp_path: Path) -> None:
    """History hydration must restore both persisted reasoning and the answer."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                session_id = str((await _create_session(page, base_url))["id"])
                await page.add_init_script(
                    "localStorage.setItem('bianbu_session_id', "
                    f"{json.dumps(session_id)})"
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator("#chat-input").fill("distinct minimax thinking")
                await page.locator("#send-btn").click()
                await expect(page.locator(".answer-content").last).to_have_text(
                    "public answer"
                )
                await page.wait_for_function(
                    "owner => !sessionActiveRequest[owner]",
                    arg=session_id,
                )
                await expect(page.locator(".run-progress-indicator")).to_have_count(0)

                await page.reload(wait_until="domcontentloaded")

                thinking = page.locator(".agent-thinking-step").last
                await expect(thinking).to_contain_text("private plan")
                await expect(thinking.locator(".thinking-summary")).to_have_text(
                    "已深度思考"
                )
                await expect(page.locator(".answer-content").last).to_have_text(
                    "public answer"
                )
            finally:
                await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    ["terminal reasoning done", "terminal reasoning failure"],
)
async def test_terminal_event_finalizes_reasoning_without_answer(
    tmp_path: Path,
    prompt: str,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                session_id = str((await _create_session(page, base_url))["id"])
                await page.add_init_script(
                    "localStorage.setItem('bianbu_session_id', "
                    f"{json.dumps(session_id)})"
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator("#chat-input").fill(prompt)
                await page.locator("#send-btn").click()

                thinking = page.locator(".agent-thinking-step").last
                await expect(thinking).to_contain_text(
                    "done-only plan" if prompt.endswith("done") else "failed plan"
                )
                await expect(thinking.locator(".thinking-summary")).to_have_text(
                    "已深度思考"
                )
                await expect(thinking.locator(".thinking-full")).to_be_hidden()
                await page.wait_for_function(
                    "owner => !sessionActiveRequest[owner]",
                    arg=session_id,
                )
                await expect(page.locator(".run-progress-indicator")).to_have_count(0)

                await page.reload(wait_until="domcontentloaded")

                await expect(thinking).to_contain_text(
                    "done-only plan" if prompt.endswith("done") else "failed plan"
                )
                await expect(thinking.locator(".thinking-summary")).to_have_text(
                    "已深度思考"
                )
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_refused_provider_delete_reports_reason_in_the_modal(tmp_path: Path) -> None:
    """A provider still bound to an expert explains the refusal inside the modal."""

    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="models"]').click()
                provider = page.locator(
                    "#models-providers-list button", has_text="Browser"
                )
                await expect(provider).to_have_count(1)
                await provider.click()
                await expect(
                    page.locator("#custom-provider-modal-overlay")
                ).to_be_visible()
                await page.locator("#custom-provider-modal-delete").click()
                status = page.locator("#custom-provider-modal-status")
                await expect(status).to_contain_text("正在被专家使用")
                await expect(status).to_have_css("opacity", "1")
                await expect(
                    page.locator("#custom-provider-modal-overlay")
                ).to_be_visible()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_refused_agent_delete_shows_the_runtime_reason(tmp_path: Path) -> None:
    """Expert deletion failures must relay the Runtime reason, not a generic guess."""

    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()

                async def refuse(route):
                    if route.request.method != "DELETE":
                        await route.continue_()
                        return
                    await route.fulfill(
                        status=404,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "error": {
                                    "code": "not_found",
                                    "message": "找不到专家 default，它可能已被删除。",
                                    "details": {"kind": "agent", "id": "default"},
                                }
                            }
                        ),
                    )

                await page.route("**/api/v1/agents/**", refuse)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="agents"]').click()
                card = page.locator(".agent-card", has_text="Default")
                await expect(card).to_be_visible()
                await card.locator(".agent-card-delete").click()
                await expect(
                    page.locator("#confirm-dialog-overlay")
                ).to_be_visible()
                await page.locator("#confirm-dialog-ok").click()
                message = page.locator("#confirm-dialog-message")
                await expect(message).to_contain_text("找不到专家")
                await expect(message).not_to_contain_text("securityd")
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_refused_mcp_uninstall_reports_the_runtime_reason(tmp_path: Path) -> None:
    """An MCP Server owned by a Plugin explains the refusal instead of doing nothing."""

    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))

                async def serve(route):
                    if route.request.method == "DELETE":
                        await route.fulfill(
                            status=409,
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "error": {
                                        "code": "mcp_owned_by_plugin",
                                        "message": (
                                            "MCP server mcp.demo 由插件 runtime 管理，"
                                            "请改为删除所属插件。"
                                        ),
                                        "details": {
                                            "mcp_id": "mcp.demo",
                                            "owner_plugin_id": "runtime",
                                        },
                                    }
                                }
                            ),
                        )
                        return
                    if route.request.url.endswith("/mcp-servers"):
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "status": "success",
                                    "data": [
                                        {
                                            "id": "mcp.demo",
                                            "display_name": "Demo MCP",
                                            "status": "running",
                                            "transport": "stdio",
                                            "removable": True,
                                            "tools": [],
                                        }
                                    ],
                                }
                            ),
                        )
                        return
                    await route.continue_()

                await page.route("**/api/v1/mcp-servers**", serve)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="mcp"]').click()
                card = page.locator('#mcp-list article[data-mcp-id="mcp.demo"]')
                await expect(card).to_be_visible()
                await card.locator('[data-action="uninstall"]').click()
                await expect(page.locator("#confirm-dialog-overlay")).to_be_visible()
                await expect(page.locator("#confirm-dialog-message")).to_contain_text(
                    "由插件 runtime 管理"
                )
                await expect(card).to_be_visible()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_refused_plugin_delete_reports_the_runtime_reason(tmp_path: Path) -> None:
    """A Plugin an Expert still uses explains the refusal instead of doing nothing."""

    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()

                async def serve(route):
                    if route.request.method == "DELETE":
                        await route.fulfill(
                            status=409,
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "error": {
                                        "code": "capability_in_use",
                                        "message": (
                                            "插件 demo.plugin 正在被专家使用：nws-agent。"
                                            "请先在这些专家里移除该插件，再删除该插件。"
                                        ),
                                        "details": {"kind": "plugin", "id": "demo.plugin"},
                                    }
                                }
                            ),
                        )
                        return
                    if route.request.url.endswith("/plugins"):
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "status": "success",
                                    "data": [{"id": "demo.plugin", "members": []}],
                                }
                            ),
                        )
                        return
                    await route.continue_()

                await page.route("**/api/v1/plugins**", serve)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="plugins"]').click()
                card = page.locator("#plugin-admin-list .plugin-admin-card").first
                await expect(card).to_contain_text("demo.plugin")
                await card.locator(".plugin-admin-delete").click()
                await expect(page.locator("#confirm-dialog-overlay")).to_be_visible()
                await expect(page.locator("#confirm-dialog-message")).to_contain_text(
                    "正在被专家使用"
                )
                await expect(card).to_be_visible()
            finally:
                await browser.close()


def test_browser_static_source_has_only_versioned_resource_paths() -> None:
    web_root = Path(__file__).parents[3] / "bianbu_agent" / "console" / "web"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (web_root / "chat.html", web_root / "static/js/console.js")
    )
    old_paths = (
        "/api/config",
        "/api/message",
        "/api/stream",
        "/api/poll",
        "/api/cancel",
        "/api/steer",
        "/api/approval",
        "/api/clarification",
        "/api/pending-approval",
        "/api/pending-clarification",
        "/api/sessions",
        "/api/history",
        "/api/messages/delete",
        "/api/agents",
        "/api/models",
        "/api/tools",
        "/api/skills",
        "/api/hooks",
        "/api/plugins",
        "/api/upload",
        "/api/file",
        "/api/agent-avatars",
        "/api/logs",
        "/api/version",
        "/api/voice",
        "/api/weixin",
        "/api/feishu",
        "/api/memory",
        "/api/knowledge",
        "/api/scheduler",
        "/api/channels",
    )

    assert "/api/v1" in source
    assert "/api/v1" in source
    assert [path for path in old_paths if path in source] == []


@pytest.mark.asyncio
async def test_session_runtime_isolated_views_replay_and_sidebar_state(
    tmp_path: Path,
) -> None:
    """History and live events share one per-Session reducer and list model."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                result = await page.evaluate(
                    """() => {
                        const runtime = new window.BianbuSessionRuntime.SessionRuntime();
                        runtime.select('alpha');
                        const alpha = runtime.get('alpha');
                        alpha.installHistory([
                            {seq: 0, type: 'user/message', run_id: 'r1', time: 1,
                             data: {content: 'hello'}},
                            {seq: 1, type: 'assistant/text.delta', run_id: 'r1', time: 2,
                             data: {text: 'hel'}},
                        ]);
                        const duplicate = alpha.acceptLive(
                            {seq: 1, type: 'assistant/text.delta', run_id: 'r1', time: 2,
                             data: {text: 'hel'}}
                        );
                        const appended = alpha.acceptLive(
                            {seq: 2, type: 'assistant/text.delta', run_id: 'r1', time: 3,
                             data: {text: 'lo'}}
                        );
                        const gap = alpha.acceptLive(
                            {seq: 4, type: 'run/completed', run_id: 'r1', time: 4,
                             data: {content: 'hello'}}
                        );
                        runtime.updateSummaries([
                            {id: 'alpha', running: true, waiting: false, failed: false,
                             active_run_id: 'r1', pending_interaction: null},
                            {id: 'beta', running: true, waiting: false, failed: false,
                             active_run_id: 'r2', pending_interaction: null},
                        ]);
                        runtime.updateSummaries([
                            {id: 'alpha', running: true, waiting: false, failed: false,
                             active_run_id: 'r1', pending_interaction: null},
                            {id: 'beta', running: false, waiting: false, failed: false,
                             active_run_id: null, pending_interaction: null},
                        ]);
                        return {
                            duplicate, appended, gap,
                            alpha: alpha.snapshot(),
                            beta: runtime.summary('beta'),
                        };
                    }"""
                )
                assert result["duplicate"] == "duplicate"
                assert result["appended"] == "applied"
                assert result["gap"] == "gap"
                assert result["alpha"]["messages"] == [
                    {"role": "user", "content": "hello", "run_id": "r1", "created_at": 1},
                    {
                        "role": "assistant", "content": "hello", "run_id": "r1",
                        "created_at": 2,
                        "segment_id": "r1:1",
                        "steps": [{"type": "content", "content": "hello"}],
                        "streaming": True,
                    },
                ]
                assert result["beta"]["completed_unread"] is True
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_session_runtime_projects_live_pending_input_immediately(
    tmp_path: Path,
) -> None:
    """A live inbox event must become a user message without a later event."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                result = await page.evaluate(
                    """() => {
                        const model = new window.BianbuSessionRuntime.SessionViewModel('s1');
                        const outcome = model.acceptLive({
                            seq: 0, type: 'agent/inbox/spliced', run_id: null,
                            time: 1, data: {message: {
                                message_id: 'message-1', content: 'first question',
                                created_at: 1, attachments: [],
                            }},
                        });
                        return {outcome, messages: model.snapshot().messages};
                    }"""
                )
                assert result == {
                    "outcome": "applied",
                    "messages": [{
                        "role": "user",
                        "content": "first question",
                        "run_id": None,
                        "message_id": "message-1",
                        "pending": True,
                        "created_at": 1,
                    }],
                }
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_staged_first_input_survives_initial_history_install(
    tmp_path: Path,
) -> None:
    """Initial history must not erase the first locally submitted message."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                result = await page.evaluate(
                    """() => {
                        const model = new window.BianbuSessionRuntime.SessionViewModel('s1');
                        const supported = typeof model.stagePendingInput === 'function';
                        if (!supported) return {supported};
                        model.stagePendingInput({
                            messageId: 'message-first', content: 'hi',
                            createdAt: 1, attachments: [],
                        });
                        const generation = model.beginHistory();
                        model.installTail([{
                            seq: 1, type: 'assistant/text.delta', run_id: 'run-first',
                            time: 2, data: {text: 'hello'},
                        }], {generation});
                        return {supported, messages: model.snapshot().messages};
                    }"""
                )
                assert result["supported"] is True
                assert [message["role"] for message in result["messages"]] == [
                    "user", "assistant",
                ]
                assert result["messages"][0]["message_id"] == "message-first"
                assert result["messages"][0]["content"] == "hi"
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_buffered_authoritative_input_takes_over_staged_input(
    tmp_path: Path,
) -> None:
    """A mux input buffered behind history is already authoritative."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                result = await page.evaluate(
                    """() => {
                        const model = new window.BianbuSessionRuntime.SessionViewModel('s1');
                        model.stagePendingInput({
                            messageId: 'message-buffered', content: 'hi',
                            createdAt: 1, attachments: [],
                        });
                        const generation = model.beginHistory();
                        const outcome = model.acceptLive({
                            seq: 0, type: 'agent/inbox/spliced', run_id: null,
                            time: 1, data: {message: {
                                message_id: 'message-buffered', content: 'hi',
                                created_at: 1, attachments: [],
                            }},
                        });
                        const discarded = model.discardPendingInput('message-buffered');
                        const beforeInstall = model.snapshot().messages;
                        model.installTail([], {generation});
                        return {
                            outcome,
                            discarded,
                            beforeInstall,
                            afterInstall: model.snapshot().messages,
                        };
                    }"""
                )
                assert result["outcome"] == "buffered"
                assert result["discarded"] is False
                assert [item["role"] for item in result["beforeInstall"]] == ["user"]
                assert [item["role"] for item in result["afterInstall"]] == ["user"]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_session_runtime_buffers_live_during_history_and_keeps_one_assistant(
    tmp_path: Path,
) -> None:
    """History overlap and live reasoning converge into one Run projection."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                result = await page.evaluate(
                    """() => {
                        const model = new window.BianbuSessionRuntime.SessionViewModel('s1');
                        const generation = model.beginHistory();
                        const buffered = model.acceptLive({
                            seq: 2, type: 'assistant/reasoning.delta', run_id: 'r1',
                            time: 3, data: {text: 'plan two'},
                        });
                        model.installHistory([
                            {seq: 0, type: 'user/message', run_id: 'r1', time: 1,
                             data: {content: 'question'}},
                            {seq: 1, type: 'assistant/reasoning.delta', run_id: 'r1',
                             time: 2, data: {text: 'plan one '}},
                            {seq: 2, type: 'assistant/reasoning.delta', run_id: 'r1',
                             time: 3, data: {text: 'plan two'}},
                        ], {generation});
                        model.acceptLive({
                            seq: 3, type: 'assistant/text.delta', run_id: 'r1',
                            time: 4, data: {text: 'answer'},
                        });
                        model.acceptLive({
                            seq: 4, type: 'run/completed', run_id: 'r1', time: 5,
                            data: {content: 'answer'},
                        });
                        return {buffered, snapshot: model.snapshot()};
                    }"""
                )

                assert result["buffered"] == "buffered"
                assert result["snapshot"]["messages"] == [
                    {"role": "user", "content": "question", "run_id": "r1", "created_at": 1},
                    {
                        "role": "assistant",
                        "content": "answer",
                        "reasoning": "plan one plan two",
                            "run_id": "r1",
                            "created_at": 2,
                            "segment_id": "r1:1",
                        "steps": [
                            {"type": "thinking", "content": "plan one plan two"},
                            {"type": "content", "content": "answer"},
                        ],
                        "streaming": False,
                    },
                ]
            finally:
                await browser.close()


def test_console_uses_only_shared_harness_downlinks() -> None:
    web_root = Path(__file__).parents[3] / "bianbu_agent" / "console" / "web"
    source = (web_root / "static/js/console.js").read_text(encoding="utf-8")
    runtime = (web_root / "static/js/session-runtime.js").read_text(encoding="utf-8")

    assert "new Socket" in runtime
    assert "this._open('mux')" in runtime
    assert "this._open('host')" in runtime
    assert "${this.basePath}/events/${channel}" in runtime
    for obsolete in (
        "createRunPollingStream",
        "openConsoleRunStream",
        "createSessionEventFeed",
        "_reattachStream",
        "streamBuffers",
        "runConnections",
        "activeStreams",
        "/messages?",
        "/events?cursor=",
        "once=true",
    ):
        assert obsolete not in source


def test_local_model_page_static_contract() -> None:
    """The read-only local model page remains a first-class console destination."""

    web_root = Path(__file__).parents[3] / "bianbu_agent" / "console" / "web"
    markup = (web_root / "chat.html").read_text(encoding="utf-8")
    source = (web_root / "static/js/console.js").read_text(encoding="utf-8")

    for contract in (
        'data-view="bibit-models"',
        'id="view-bibit-models"',
        'id="bibit-model-llm-list"',
        'id="bibit-model-embed-list"',
    ):
        assert contract in markup
    assert 'id="bibit-model-origin"' not in markup
    assert "/local-models" in source
    assert "plugin-consoles" not in source
    assert "loadLocalModelsView" in source
    assert "127.0.0.1:8050" not in source
    assert "BIBIT_MODEL_MANAGER_PORT" not in source


@pytest.mark.asyncio
@pytest.mark.parametrize("viewport_width", [1280, 390])
async def test_local_model_page_renders_read_only_gateway_status(
    tmp_path: Path, viewport_width: int,
) -> None:
    """The page shows the Gateway state and offers no lifecycle control."""

    mutations: list[tuple[str, str]] = []

    def payload() -> dict[str, object]:
        return {
            "source": "bibit-intent",
            "read_only": True,
            "models": [
                {
                    "model_type": "llm", "name": "bibit-llm", "backend": "ai_gateway",
                    "artifact": "glm-edge-1.5b-chat-q4_0.gguf",
                    "port": None, "path": "/home/bianbu/.cache/models/llm/glm-edge-1.5b-chat-q4_0.gguf",
                    "download": {"status": "idle", "progress": 0},
                    "server": {"status": "stopped", "managed": True},
                },
                {
                    "model_type": "embed", "name": "bibit-embed", "backend": "ai_gateway",
                    "artifact": "Qwen3-Embedding-0.6B-Q4_0.gguf",
                    "port": None, "path": "/home/bianbu/.cache/models/embed/Qwen3-Embedding-0.6B-Q4_0.gguf",
                    "download": {"status": "downloading", "progress": 64},
                    "server": {"status": "running", "managed": True},
                },
            ],
        }

    async def local_models(route) -> None:
        mutations.append((route.request.method, route.request.url))
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": payload()}),
        )

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(
                    viewport={"width": viewport_width, "height": 844}
                )
                await page.route("**/api/v1/local-models**", local_models)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                if viewport_width < 1024:
                    await page.wait_for_function("() => agentsLoaded")
                    await page.evaluate("() => closeSessionPanel()")
                    await page.locator("#menu-toggle").click()
                await page.locator('[data-view="bibit-models"]').click()

                llm = page.locator('#bibit-model-llm-list [data-model-id="llm"]')
                embed = page.locator('#bibit-model-embed-list [data-model-id="embed"]')
                # The Gateway ids stay out of the page; the artifact names the model.
                await expect(llm).to_contain_text("glm-edge-1.5b-chat-q4_0.gguf")
                await expect(llm).not_to_contain_text("bibit-llm")
                await expect(llm).to_contain_text("未加载")
                await expect(embed).to_contain_text("Qwen3-Embedding-0.6B-Q4_0.gguf")
                await expect(embed).not_to_contain_text("bibit-embed")
                await expect(embed).to_contain_text("运行中")
                await expect(page.locator("#view-bibit-models")).to_contain_text("大语言模型")
                await expect(page.locator("#view-bibit-models")).not_to_contain_text("意图识别模型")
                await expect(
                    embed.locator(".bibit-model-progress-label")
                ).to_have_text("64%")
                # Read-only: no download/start/stop button anywhere on the page.
                await expect(page.locator('#view-bibit-models [data-action]')).to_have_count(0)
                await expect(page.get_by_text("Failed to fetch")).to_have_count(0)
                for text in ("下载", "启动服务", "停止服务", "取消下载"):
                    await expect(page.get_by_role("button", name=text)).to_have_count(0)

                assert [method for method, _ in mutations] == ["GET"]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_local_model_page_reports_unavailable_gateway(tmp_path: Path) -> None:
    """A dead Gateway is explained instead of silently retried."""

    async def local_models(route) -> None:
        await route.fulfill(
            status=502,
            content_type="application/json",
            body=json.dumps({
                "error": {
                    "code": "bad_gateway",
                    "message": "Bibit Intent request failed (http://127.0.0.1:8765/v1/models)",
                }
            }),
        )

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/api/v1/local-models**", local_models)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="bibit-models"]').click()

                alert = page.locator("#bibit-models-alert")
                await expect(alert).to_contain_text("本地模型状态不可用")
                await expect(alert).to_contain_text("Bibit Intent request failed")
                await expect(page.get_by_text("HTTP 502")).to_have_count(0)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_local_model_page_recovers_after_invalid_payload(tmp_path: Path) -> None:
    """A bad payload is explained and the next poll recovers without re-entry."""

    polls = 0

    async def local_models(route) -> None:
        nonlocal polls
        polls += 1
        payload = {"models": [None]} if polls == 1 else {"models": [
            {"model_type": "llm", "name": "bibit-llm", "backend": "ai_gateway",
             "port": None, "path": None,
             "download": {"status": "completed", "progress": 100},
             "server": {"status": "stopped"}},
            {"model_type": "embed", "name": "bibit-embed", "backend": "ai_gateway",
             "port": None, "path": None,
             "download": {"status": "completed", "progress": 100},
             "server": {"status": "stopped"}},
        ]}
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": payload}),
        )

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/api/v1/local-models**", local_models)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="bibit-models"]').click()
                await expect(
                    page.locator('#bibit-model-llm-list [data-model-id="llm"]')
                ).to_contain_text("bibit-llm", timeout=6_000)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_local_model_page_lists_unregistered_models_without_inventing_state(
    tmp_path: Path,
) -> None:
    """An empty Gateway report is shown as not-registered, not as a failure."""

    async def local_models(route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": {"source": "ai-gateway", "read_only": True, "models": []}}),
        )

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/api/v1/local-models**", local_models)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="bibit-models"]').click()

                await expect(
                    page.locator('#bibit-model-llm-list')
                ).to_contain_text("未在 AI Gateway 注册")
                await expect(page.locator("#bibit-models-alert")).to_have_count(1)
                await expect(page.locator("#bibit-models-alert")).to_be_hidden()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_hostile_console_values_cannot_execute_or_request_admin_mutations(
    tmp_path: Path,
) -> None:
    """IDs, titles, choices, and Run media stay data at every browser boundary."""

    hostile_id = "x');window.__consoleXss.push('id');fetch('/api/v1/settings',{method:'PATCH'});//"
    hostile_title = 'hover me" onpointerenter="window.__consoleXss.push(\'title\');fetch(\'/api/v1/settings\',{method:\'PATCH\'})" x="'
    hostile_video = 'broken.mp4" onerror="window.__consoleXss.push(\'media\');fetch(\'/api/v1/settings\',{method:\'PATCH\'})" x="'

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                admin_mutations: list[str] = []
                page.on(
                    "request",
                    lambda request: admin_mutations.append(request.url)
                    if urlparse(request.url).path.startswith("/api/v1/")
                    and request.method != "GET"
                    else None,
                )
                await page.route(
                    "**/api/v1/sessions?*",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({
                            "data": [{
                                "id": hostile_id,
                                "title": hostile_title,
                                "agent_id": "default",
                                "metadata": {},
                                "created_at": 1,
                                "updated_at": 1,
                            }],
                            "page": {"number": 1, "size": 50, "has_more": False},
                        }),
                    ),
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate("window.__consoleXss = []")
                await page.wait_for_function("() => agentsLoaded")
                await page.evaluate("() => openSessionPanel()")
                await page.evaluate("() => closeChatAgentMenu()")
                row = page.locator(".session-item", has_text="hover me")
                await expect(row).to_be_visible()
                await expect(row.locator(".session-title")).not_to_have_attribute(
                    "onpointerenter", re.compile(".+")
                )
                await row.locator(".session-title").hover()
                await row.locator(".session-rename").click()

                await page.evaluate(
                    """agentId => {
                        agentsState = [{
                            id: agentId, name: 'Hostile Agent', avatar: '🤖',
                            tools: [], skills: [],
                        }];
                        agentsLoaded = true;
                        renderAgentsView();
                    }""",
                    hostile_id,
                )
                await page.locator(".agent-card-use").evaluate("element => element.click()")
                await page.evaluate(
                    """video => {
                        const host = document.getElementById('chat-messages');
                        const image = createImagePreview(
                            'javascript:window.__consoleXss.push("image")'
                        );
                        const movie = createVideoPreview(video);
                        if (image) host.appendChild(image);
                        if (movie) host.appendChild(movie);
                    }""",
                    hostile_video,
                )
                await page.wait_for_timeout(100)

                assert await page.evaluate("window.__consoleXss") == []
                assert admin_mutations == []
                assert await page.locator(".media-content [onerror]").count() == 0
                assert await page.locator(".media-content img").count() == 0
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_agent_top_monitor_renders_operational_snapshot(tmp_path: Path) -> None:
    """The Monitor group exposes a native Agent and Run dashboard."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                calls = 0

                async def snapshot(route) -> None:
                    nonlocal calls
                    calls += 1
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"data": {
                            "summary": {"agents": 3, "available_agents": 2, "sessions": 7, "runs": 12, "active_runs": 1, "waiting_runs": 1, "failed_runs": 2},
                            "agents": [{"id": "researcher", "name": "Researcher", "avatar": "🔎", "available": True, "status": "waiting", "session_count": 4, "run_count": 9, "active_run_count": 1, "failed_run_count": 1, "last_active_at": 1786400000.0}],
                            "recent_runs": [{"id": "run-1", "session_id": "session-1", "session_title": "Analyze release", "agent_id": "researcher", "status": "waiting", "created_at": 1786399990.0, "updated_at": 1786400000.0}],
                            "server_time": 1786400001.0,
                        }}),
                    )

                await page.route("**/api/v1/agent-top", snapshot)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                monitor_group = page.locator('[data-group="monitor"]')
                await expect(
                    monitor_group.locator('[data-view="agent-top"]')
                ).to_have_count(1)
                await monitor_group.locator('[data-view="agent-top"]').click()

                await expect(page.locator(".agent-top-dashboard")).to_be_visible()
                await expect(page.locator("#agent-top-summary")).to_be_visible()
                await expect(page.locator("#view-agent-top .agent-top-panel")).to_have_count(2)
                await expect(page.locator(".agent-top-terminal")).to_have_count(0)
                await expect(page.locator("#agent-top-summary")).to_contain_text("12")
                await expect(page.locator("#agent-top-agents")).to_contain_text("Researcher")
                await expect(page.locator("#agent-top-runs")).to_contain_text("Analyze release")
                assert calls >= 1
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_provider_capture_page_manages_target_records_and_safe_detail(
    tmp_path: Path,
) -> None:
    """The dedicated Monitor page owns capture without changing ordinary Logs."""

    async with _running_console(tmp_path) as environment:
        base_url, home = environment
        store = ProviderCaptureStore(home.logs_dir / "provider-capture")
        store.select("browser")
        hostile = '<img src=x onerror="window.__captureXss = true">'
        record_id = store.write(
            {
                "provider_id": "browser",
                "model": "browser-contract",
                "status": "success",
                "started_at": 1786400000.0,
                "ended_at": 1786400001.25,
                "duration_ms": 1250,
                "input": {
                    "messages": [
                        f"HEAD-MARKER {hostile}" + ("x" * 9000) + " TAIL-MARKER"
                    ]
                },
                "output": {"text": f"SAFE-ANSWER {hostile}"},
                "http_request": {
                    "method": "POST",
                    "url": "https://browser.example.test/v1/chat/completions",
                    "headers": {"content-type": "application/json"},
                    "body": {
                        "model": "browser-contract",
                        "messages": [
                            {
                                "role": "user",
                                "content": "RAW-REQUEST-MARKER "
                                + ("x" * 9000)
                                + " RAW-REQUEST-TAIL",
                            },
                            {
                                "role": "assistant",
                                "content": "Checking the weather.",
                                "tool_calls": [
                                    {
                                        "id": "call-weather",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup_weather",
                                            "arguments": '{"city":"Shanghai"}',
                                        },
                                    }
                                ],
                            },
                            {
                                "role": "tool",
                                "tool_call_id": "call-weather",
                                "content": '{"temperature_c":31}',
                            },
                        ],
                        "tools": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "lookup_weather",
                                    "description": "Look up current weather",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {
                                            "city": {"type": "string"}
                                        },
                                        "required": ["city"],
                                    },
                                },
                            }
                        ],
                        "stream": True,
                    },
                },
                "http_response": {
                    "status_code": 200,
                    "headers": {"content-type": "application/json"},
                    "body": {
                        "stream": [
                            {
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "role": "assistant",
                                            "content": "RAW-RESPONSE-",
                                        },
                                    }
                                ]
                            },
                            {
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "content": "MARKER",
                                            "tool_calls": [
                                                {
                                                    "index": 0,
                                                    "id": "call-streamed",
                                                    "type": "function",
                                                    "function": {
                                                        "name": "lookup_",
                                                        "arguments": '{"city":"Shang',
                                                    },
                                                }
                                            ],
                                        },
                                    }
                                ]
                            },
                            {
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "tool_calls": [
                                                {
                                                    "index": 0,
                                                    "function": {
                                                        "name": "weather",
                                                        "arguments": 'hai"}',
                                                    },
                                                }
                                            ]
                                        },
                                    }
                                ]
                            },
                        ]
                    },
                },
                "usage": {"input_tokens": 12, "output_tokens": 5},
                "metadata": {
                    "agent_id": "agent-browser",
                    "session_id": "session-browser",
                    "run_id": "run-browser",
                },
            }
        )
        nonstream_record_id = store.write(
            {
                "provider_id": "browser",
                "model": "browser-contract",
                "status": "success",
                "http_request": {
                    "body": {
                        "messages": [
                            {"role": "user", "content": "NONSTREAM-REQUEST"}
                        ]
                    }
                },
                "http_response": {
                    "body": {
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "NONSTREAM-RESPONSE",
                                    "tool_calls": [
                                        {
                                            "id": "call-nonstream",
                                            "type": "function",
                                            "function": {
                                                "name": "lookup_time",
                                                "arguments": '{"zone":"Asia/Shanghai"}',
                                            },
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                },
            }
        )
        reconstructable_previews = {
                store.write({
                    "provider_id": "browser",
                    "model": "ascii-4097",
                    "status": "success",
                    "http_request": {"body": "a" * 4097},
                    "http_response": {"body": "ok"},
                }): "a" * 4097,
                store.write({
                    "provider_id": "browser",
                    "model": "multibyte-8192",
                    "status": "success",
                    "http_request": {"body": "🙂" * 2048},
                    "http_response": {"body": "ok"},
                }): "🙂" * 2048,
        }
        omitted_bytes = next(
            row["input_preview"]["omitted_bytes"]
            for row in store.list()
            if row["id"] == record_id
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                created = await page.request.post(
                    f"{base_url}/api/v1/providers",
                    data={
                        "id": "custom:switch",
                        "label": "Switch Target",
                        "base_url": "https://switch.example.test/v1",
                        "api_key": "switch-secret",
                    },
                )
                assert created.status == 201
                await page.request.post(
                    f"{base_url}/api/v1/providers",
                    data={
                        "id": "custom:incomplete",
                        "label": "Incomplete Target",
                        "base_url": "https://incomplete.example.test/v1",
                    },
                )
                detail_requests: list[str] = []
                capture_mutations: list[str] = []

                def observe_request(request) -> None:
                    path = urlparse(request.url).path
                    if path == f"/api/v1/provider-capture/{record_id}":
                        detail_requests.append(path)
                    if path.startswith("/api/v1/provider-capture") and request.method != "GET":
                        capture_mutations.append(request.method)

                page.on("request", observe_request)
                await page.add_init_script(
                    "localStorage.setItem('bianbu_lang', 'en'); window.__captureXss = false"
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")

                monitor = page.locator('[data-group="monitor"]')
                await expect(monitor.locator('[data-view="api-capture"]')).to_have_count(1)
                await expect(monitor.locator('[data-view="logs"]')).to_have_count(1)
                await monitor.locator('[data-view="api-capture"]').click()
                await expect(page.locator("#view-api-capture")).to_be_visible()

                selector = page.locator("#provider-capture-provider")
                await expect(selector.locator('option[value="browser"]')).to_have_text("Browser")
                await expect(selector.locator('option[value="custom:switch"]')).to_have_text("Switch Target")
                await expect(selector.locator('option[value="custom:incomplete"]')).to_have_count(0)
                await expect(selector).to_have_value("browser")
                await expect(page.locator("#provider-capture-active-target")).to_contain_text("Browser")
                await expect(page.locator("#provider-capture-retention")).to_contain_text("7 days")
                await expect(page.locator("#provider-capture-retention")).to_contain_text("100 MiB")
                await expect(page.locator("#provider-capture-retention")).to_contain_text("total storage")
                await expect(page.locator("#provider-capture-retention")).to_contain_text("1,000 records")
                await expect(page.locator("#provider-capture-retention")).to_contain_text("8 MiB")
                await expect(page.locator("#provider-capture-retention")).to_contain_text("4 KiB")
                await expect(page.locator("#provider-capture-retention")).to_contain_text("64 KiB sidecars")
                await expect(page.locator("#provider-capture-warning")).to_be_visible()

                await page.reload(wait_until="domcontentloaded")
                await page.locator('[data-view="api-capture"]').click()
                await expect(selector).to_have_value("browser")
                await expect(page.locator("#provider-capture-active-target")).to_contain_text("Browser")

                row = page.locator(f'[data-capture-record-id="{record_id}"]')
                await expect(row).to_contain_text("RAW-REQUEST-MARKER")
                await expect(row).to_contain_text("truncated")
                await expect(row).to_contain_text(f"{omitted_bytes} bytes omitted")
                await expect(row).to_contain_text("agent-browser")
                await expect(row).to_contain_text("Success")
                await expect(row).to_contain_text("Tokens 17")
                await expect(row).not_to_contain_text("Tokens 0")
                assert detail_requests == []
                assert capture_mutations == []
                assert await page.evaluate("window.__captureXss") is False
                assert await page.locator("#provider-capture-records img").count() == 0
                for preview_id, expected_text in reconstructable_previews.items():
                    preview_row = page.locator(
                        f'[data-capture-record-id="{preview_id}"]'
                    )
                    assert await preview_row.locator("pre").first.text_content() == expected_text
                    await expect(preview_row).not_to_contain_text("truncated")

                await row.locator(".provider-capture-open").click()
                await expect(page.locator("#provider-capture-modal")).to_be_visible()
                await expect(page.locator("#provider-capture-detail-input")).to_contain_text("RAW-REQUEST-MARKER")
                await expect(page.locator("#provider-capture-detail-output")).to_contain_text("RAW-RESPONSE-")
                await expect(page.locator("#provider-capture-detail-output")).to_contain_text('"content": "MARKER"')
                request_friendly = page.locator(
                    "#provider-capture-detail-request-friendly"
                )
                await expect(request_friendly).to_contain_text("USER")
                await expect(request_friendly).to_contain_text(
                    "RAW-REQUEST-TAIL"
                )
                await expect(request_friendly).to_contain_text("ASSISTANT")
                await expect(request_friendly).to_contain_text(
                    "Checking the weather."
                )
                await expect(request_friendly).to_contain_text("TOOL")
                await expect(request_friendly).to_contain_text("call-weather")
                await expect(request_friendly).to_contain_text(
                    '{"temperature_c":31}'
                )
                await expect(request_friendly).to_contain_text(
                    "Look up current weather"
                )
                await expect(request_friendly).to_contain_text(
                    '"required": [\n  "city"\n]'
                )
                response_friendly = page.locator(
                    "#provider-capture-detail-response-friendly"
                )
                await expect(response_friendly).to_contain_text("ASSISTANT")
                await expect(response_friendly).to_contain_text(
                    "RAW-RESPONSE-MARKER"
                )
                await expect(response_friendly).to_contain_text(
                    "lookup_weather"
                )
                await expect(response_friendly).to_contain_text(
                    "call-streamed"
                )
                await expect(response_friendly).to_contain_text(
                    '{"city":"Shanghai"}'
                )
                await expect(page.locator("#provider-capture-detail-truncation")).to_be_visible()
                assert detail_requests == [f"/api/v1/provider-capture/{record_id}"]
                assert await page.evaluate("window.__captureXss") is False
                assert await page.locator("#provider-capture-modal img").count() == 0
                await page.locator("#provider-capture-modal-close").click()

                nonstream_row = page.locator(
                    f'[data-capture-record-id="{nonstream_record_id}"]'
                )
                await nonstream_row.locator(".provider-capture-open").click()
                nonstream_friendly = page.locator(
                    "#provider-capture-detail-response-friendly"
                )
                await expect(nonstream_friendly).to_contain_text(
                    "NONSTREAM-RESPONSE"
                )
                await expect(nonstream_friendly).to_contain_text("lookup_time")
                await expect(nonstream_friendly).to_contain_text(
                    '{"zone":"Asia/Shanghai"}'
                )
                await page.locator("#provider-capture-modal-close").click()

                await selector.select_option("custom:switch")
                await page.wait_for_timeout(1200)
                await expect(selector).to_have_value("custom:switch")
                await page.locator("#provider-capture-start").click()
                await expect(page.locator("#provider-capture-active-target")).to_contain_text("Switch Target")
                assert store.target() == "custom:switch"

                await page.locator("#provider-capture-stop").click()
                await expect(page.locator("#provider-capture-active-target")).to_contain_text("Not capturing")
                await expect(row).to_have_count(1)
                assert store.target() is None

                await selector.select_option("browser")
                await page.locator("#provider-capture-start").click()
                await page.locator("#provider-capture-clear").click()
                await expect(page.locator("#provider-capture-records")).to_contain_text("No captured calls")
                assert store.target() == "browser"
                assert store.list() == []
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_provider_capture_polling_is_single_flight_and_stops_on_navigation(
    tmp_path: Path,
) -> None:
    """Preview polling has one request in flight and aborts when its page hides."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                started = asyncio.Event()
                release = asyncio.Event()
                calls = 0
                in_flight = 0
                maximum_in_flight = 0

                async def held_collection(route) -> None:
                    nonlocal calls, in_flight, maximum_in_flight
                    if route.request.method != "GET":
                        await route.continue_()
                        return
                    calls += 1
                    in_flight += 1
                    maximum_in_flight = max(maximum_in_flight, in_flight)
                    started.set()
                    await release.wait()
                    in_flight -= 1
                    try:
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({"data": {"provider_id": None, "limits": {}, "records": []}}),
                        )
                    except Exception:
                        pass

                await page.route("**/api/v1/provider-capture", held_collection)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                mutations: list[str] = []
                page.on(
                    "request",
                    lambda request: mutations.append(request.method)
                    if urlparse(request.url).path.startswith("/api/v1/provider-capture")
                    and request.method != "GET"
                    else None,
                )
                await page.locator('[data-view="api-capture"]').click()
                await asyncio.wait_for(started.wait(), timeout=3)
                await page.wait_for_timeout(1200)
                assert calls == 1
                assert maximum_in_flight == 1
                assert mutations == []

                await page.locator('[data-view="logs"]').click()
                release.set()
                await page.wait_for_timeout(1200)
                assert calls == 1
                assert await page.evaluate("currentView") == "logs"
            finally:
                release.set()
                await browser.close()


@pytest.mark.asyncio
async def test_provider_capture_catalog_failure_does_not_block_records_and_retries(
    tmp_path: Path,
) -> None:
    """Provider discovery retries separately while persisted capture data renders."""

    async with _running_console(tmp_path) as environment:
        base_url, home = environment
        store = ProviderCaptureStore(home.logs_dir / "provider-capture")
        store.select("browser")
        record_id = store.write(
            {
                "provider_id": "browser",
                "model": "browser-contract",
                "status": "success",
                    "http_request": {"body": "catalog independent request" * 500},
                    "http_response": {"body": "catalog independent response"},
            }
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            retry_release = asyncio.Event()
            try:
                page = await browser.new_page()
                await page.add_init_script("localStorage.setItem('bianbu_lang', 'en')")
                catalog_calls = 0

                async def catalog(route) -> None:
                    nonlocal catalog_calls
                    catalog_calls += 1
                    if catalog_calls == 1:
                        await route.fulfill(
                            status=503,
                            content_type="application/json",
                            body=json.dumps({
                                "error": {
                                    "code": "catalog_unavailable",
                                    "message": "catalog unavailable",
                                    "details": {},
                                }
                            }),
                        )
                        return
                    await retry_release.wait()
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({
                            "data": [{
                                "id": "browser",
                                "label": "Browser Retried",
                                "configured": True,
                            }]
                        }),
                    )

                await page.route("**/api/v1/providers", catalog)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="api-capture"]').click()

                await expect(
                    page.locator("#provider-capture-active-target")
                ).to_have_text("browser")
                await expect(
                    page.locator(f'[data-capture-record-id="{record_id}"]')
                ).to_be_visible()
                row = page.locator(f'[data-capture-record-id="{record_id}"]')
                await expect(row).to_contain_text("browser · browser-contract")
                provider_status = page.locator("#provider-capture-provider-status")
                await expect(provider_status).to_contain_text("catalog unavailable")
                await expect(provider_status).not_to_have_class(
                    re.compile(r"\bopacity-0\b")
                )

                await asyncio.sleep(1.1)
                assert catalog_calls >= 2
                await expect(provider_status).to_contain_text("Loading Providers")
                await expect(provider_status).not_to_have_class(
                    re.compile(r"\bopacity-0\b")
                )
                retry_release.set()
                await expect(
                    page.locator(
                        '#provider-capture-provider option[value="browser"]'
                    )
                ).to_have_text("Browser Retried")
                await expect(
                    page.locator("#provider-capture-active-target")
                ).to_have_text("Browser Retried")
                await expect(row).to_contain_text(
                    "Browser Retried · browser-contract"
                )
                open_button = row.locator(".provider-capture-open")
                await open_button.focus()
                assert await row.locator("pre").first.evaluate(
                    "element => { element.scrollTop = 40; return element.scrollTop; }"
                ) > 0
                await page.evaluate(
                    """recordId => {
                        window.__renamedCaptureRow = document.querySelector(
                            `[data-capture-record-id="${recordId}"]`
                        );
                        window.__renamedCapturePreview =
                            window.__renamedCaptureRow.querySelector('pre');
                    }""",
                    record_id,
                )
                await page.wait_for_timeout(1200)
                assert await page.evaluate(
                    """recordId => {
                        const row = document.querySelector(
                            `[data-capture-record-id="${recordId}"]`
                        );
                        return row === window.__renamedCaptureRow
                            && row.querySelector('pre')
                                === window.__renamedCapturePreview
                            && row.querySelector('pre').scrollTop === 40
                            && document.activeElement
                                === row.querySelector('.provider-capture-open');
                    }""",
                    record_id,
                )
            finally:
                retry_release.set()
                await browser.close()


@pytest.mark.asyncio
async def test_provider_capture_stale_catalog_cannot_replace_new_generation(
    tmp_path: Path,
) -> None:
    """A late catalog response from a departed page cannot overwrite re-entry."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            first_started = asyncio.Event()
            first_release = asyncio.Event()
            first_completed = asyncio.Event()
            try:
                page = await browser.new_page()
                catalog_calls = 0

                async def catalog(route) -> None:
                    nonlocal catalog_calls
                    catalog_calls += 1
                    if catalog_calls == 1:
                        first_started.set()
                        await first_release.wait()
                        label = "Stale Provider A"
                        provider_id = "stale-a"
                    else:
                        label = "Current Provider B"
                        provider_id = "current-b"
                    try:
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({
                                "data": [{
                                    "id": provider_id,
                                    "label": label,
                                    "configured": True,
                                }]
                            }),
                        )
                    except Exception:
                        pass
                    finally:
                        if catalog_calls >= 2 and provider_id == "stale-a":
                            first_completed.set()

                await page.route("**/api/v1/providers", catalog)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="api-capture"]').click()
                await asyncio.wait_for(first_started.wait(), timeout=3)
                await page.locator('[data-view="logs"]').click()
                await page.locator('[data-view="api-capture"]').click()

                current = page.locator(
                    '#provider-capture-provider option[value="current-b"]'
                )
                await expect(current).to_have_text("Current Provider B")
                first_release.set()
                await asyncio.wait_for(first_completed.wait(), timeout=3)
                await page.wait_for_timeout(300)
                await expect(current).to_have_count(1)
                await expect(
                    page.locator(
                        '#provider-capture-provider option[value="stale-a"]'
                    )
                ).to_have_count(0)
            finally:
                first_release.set()
                await browser.close()


@pytest.mark.asyncio
async def test_provider_capture_reentry_starts_new_poll_before_old_abort_settles(
    tmp_path: Path,
) -> None:
    """A delayed old request cannot retain poll ownership across re-entry."""

    record_id = "b" * 32
    preview_record = {
        "id": record_id,
        "provider_id": "browser",
        "model": "new-generation",
        "status": "success",
        "captured_at": 1786400000.0,
        "usage": {"total_tokens": None},
        "input_characters": 3,
        "output_characters": 3,
        "input_preview": {
            "head": "new",
            "tail": "new",
            "truncated": False,
            "bytes": 3,
            "characters": 3,
        },
        "output_preview": {
            "head": "now",
            "tail": "now",
            "truncated": False,
            "bytes": 3,
            "characters": 3,
        },
    }
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            old_started = asyncio.Event()
            old_release = asyncio.Event()
            try:
                page = await browser.new_page()
                await page.add_init_script(
                    """(() => {
                        localStorage.setItem('bianbu_lang', 'en');
                        const nativeFetch = window.fetch.bind(window);
                        let delayFirstCaptureAbort = true;
                        window.__releaseOldCaptureAbort = null;
                        window.fetch = (...args) => {
                            const url = String(args[0] || '');
                            const result = nativeFetch(...args);
                            if (!delayFirstCaptureAbort
                                    || !url.endsWith('/api/v1/provider-capture')) {
                                return result;
                            }
                            delayFirstCaptureAbort = false;
                            return result.catch(error => new Promise((resolve, reject) => {
                                window.__releaseOldCaptureAbort = () => reject(error);
                            }));
                        };
                    })()"""
                )
                calls = 0

                async def collection(route) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        old_started.set()
                        await old_release.wait()
                        records = []
                    else:
                        records = [preview_record]
                    try:
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({
                                "data": {
                                    "provider_id": None,
                                    "limits": {},
                                    "records": records,
                                }
                            }),
                        )
                    except Exception:
                        pass

                await page.route("**/api/v1/provider-capture", collection)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="api-capture"]').click()
                await asyncio.wait_for(old_started.wait(), timeout=3)
                loading_status = page.locator("#provider-capture-status")
                await expect(loading_status).to_contain_text("Loading capture")
                await expect(loading_status).not_to_have_class(
                    re.compile(r"\bopacity-0\b")
                )
                await page.locator('[data-view="logs"]').click()
                await page.locator('[data-view="api-capture"]').click()

                await expect(
                    page.locator(f'[data-capture-record-id="{record_id}"]')
                ).to_contain_text("new-generation")
                await expect(
                    page.locator(f'[data-capture-record-id="{record_id}"]')
                ).not_to_contain_text("Tokens 0")
                assert calls >= 2
                await page.evaluate("window.__releaseOldCaptureAbort?.()")
                old_release.set()
                await page.wait_for_timeout(300)
                await expect(
                    page.locator(f'[data-capture-record-id="{record_id}"]')
                ).to_have_count(1)
            finally:
                try:
                    await page.evaluate("window.__releaseOldCaptureAbort?.()")
                except Exception:
                    pass
                old_release.set()
                await browser.close()


@pytest.mark.asyncio
async def test_provider_capture_navigation_modal_and_unchanged_poll_dom(
    tmp_path: Path,
) -> None:
    """Navigation and modal are keyboard-safe; unchanged polls preserve the DOM."""

    async with _running_console(tmp_path) as environment:
        base_url, home = environment
        record_id = ProviderCaptureStore(
            home.logs_dir / "provider-capture"
        ).write(
            {
                "provider_id": "browser",
                "model": "focus-model",
                "status": "success",
                    "http_request": {"body": "i" * 9000},
                    "http_response": {"body": "focus output"},
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            }
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.add_init_script("localStorage.setItem('bianbu_lang', 'en')")
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                nav = page.locator('[data-view="api-capture"]')
                await nav.focus()
                await page.keyboard.press("Enter")
                await expect(page.locator("#view-api-capture")).to_be_visible()

                row = page.locator(f'[data-capture-record-id="{record_id}"]')
                await expect(row).to_contain_text("Tokens 7")
                await page.locator("#lang-toggle").click()
                await page.locator('.lang-menu-item[data-lang="zh"]').click()
                await expect(row).to_contain_text("成功")
                await expect(row).to_contain_text("专家")
                await expect(row).to_contain_text("运行")
                await expect(row).to_contain_text("Token 7")
                await expect(
                    row.locator("strong", has_text="原始 HTTP 请求 Body").locator("..").locator("span")
                ).to_have_text("9000 字符")
                await page.locator("#lang-toggle").click()
                await page.locator('.lang-menu-item[data-lang="en"]').click()
                await expect(row).to_contain_text("Success")
                open_button = row.locator(".provider-capture-open")
                await open_button.focus()
                preserved = await page.evaluate(
                    """recordId => {
                        window.__captureStableRow = document.querySelector(
                            `[data-capture-record-id="${recordId}"]`
                        );
                        window.__captureStableSelect = document.getElementById(
                            'provider-capture-provider'
                        );
                        const preview = window.__captureStableRow.querySelector('pre');
                        preview.scrollTop = 40;
                        window.__captureStablePreview = preview;
                        return preview.scrollTop;
                    }""",
                    record_id,
                )
                assert preserved > 0
                await page.wait_for_timeout(1200)
                assert await page.evaluate(
                    """recordId => {
                        const row = document.querySelector(
                            `[data-capture-record-id="${recordId}"]`
                        );
                        return row === window.__captureStableRow
                            && document.getElementById('provider-capture-provider')
                                === window.__captureStableSelect
                            && row.querySelector('pre') === window.__captureStablePreview
                            && row.querySelector('pre').scrollTop === 40
                            && document.activeElement === row.querySelector(
                                '.provider-capture-open'
                            );
                    }""",
                    record_id,
                )

                await open_button.click()
                modal = page.locator("#provider-capture-modal")
                await expect(modal).to_be_visible()
                await expect(page.locator("#provider-capture-modal-close")).to_be_focused()
                assert await page.locator("#app").evaluate("element => element.inert")
                await page.keyboard.press("Shift+Tab")
                await expect(
                    page.locator("#provider-capture-copy-output")
                ).to_be_focused()
                await page.keyboard.press("Tab")
                await expect(page.locator("#provider-capture-modal-close")).to_be_focused()
                await page.keyboard.press("Escape")
                await expect(modal).to_be_hidden()
                await expect(open_button).to_be_focused()
                assert not await page.locator("#app").evaluate("element => element.inert")

                await open_button.click()
                await expect(modal).to_be_visible()
                await page.evaluate("navigateTo('logs')")
                await expect(modal).to_be_hidden()
                assert not await page.locator("#app").evaluate("element => element.inert")
                await expect(page.locator('[data-view="logs"]')).to_be_focused()

                await nav.click()
                await open_button.click()
                await expect(modal).to_be_visible()
                await open_button.evaluate(
                    "element => element.closest('[data-capture-record-id]').remove()"
                )
                await page.keyboard.press("Escape")
                await expect(modal).to_be_hidden()
                await expect(nav).to_be_focused()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_console_omits_channels_and_scheduled_tasks(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")

                await expect(page.locator('[data-view="channels"]')).to_have_count(0)
                await expect(page.locator("#view-channels")).to_have_count(0)
                await expect(page.locator('[data-view="tasks"]')).to_have_count(0)
                await expect(page.locator("#view-tasks")).to_have_count(0)
                await expect(page.locator("#task-edit-modal-overlay")).to_have_count(0)
                assert await page.evaluate(
                    """() => [
                        'loadChannelsView',
                        'startFeishuRegister',
                        'startWeixinQrLogin',
                        'startWecomBotScan',
                        'loadTasksView',
                        'openTaskEditModal',
                    ].every(name => typeof window[name] === 'undefined')"""
                )
            finally:
                await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("submit_action", ["enter", "click"])
async def test_active_composer_routes_text_and_attachments_to_steer(
    tmp_path: Path,
    submit_action: str,
) -> None:
    """Every active-Run submission steers the next step, including attachments."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                page.set_default_timeout(4000)
                session_id, run_id = await _open_active_run(
                    page, base_url, prompt="followup hold gate"
                )
                await expect(page.get_by_text("FOLLOWUP_HOLD_READY")).to_be_visible()
                steering_payloads: list[dict[str, object]] = []
                page.on(
                    "request",
                    lambda request: (
                        steering_payloads.append(request.post_data_json)
                        if urlparse(request.url).path
                        == f"/api/v1/sessions/{session_id}/prompt"
                        else None
                    ) if request.method == "POST" else None,
                )
                await page.evaluate(
                    """() => {
                        pendingAttachments = [{
                            file_path: '/tmp/report.txt',
                            file_name: 'report.txt',
                            file_type: 'file',
                            preview_url: '',
                        }];
                        renderAttachmentPreview();
                    }"""
                )
                await page.locator("#chat-input").fill("first steer")
                if submit_action == "enter":
                    await page.locator("#chat-input").press("Enter")
                else:
                    await page.locator("#send-btn").click()
                await expect(page.locator("#chat-input")).to_have_value("")
                await page.locator("#chat-input").fill("second steer")
                await page.locator("#chat-input").press("Enter")

                await expect(
                    page.locator(".user-message-group", has_text="first steer")
                ).to_have_count(1)
                await expect(
                    page.locator(".user-message-group", has_text="second steer")
                ).to_have_count(1)
                await page.wait_for_timeout(100)
                assert len(steering_payloads) == 2
                assert [payload["content"] for payload in steering_payloads] == [
                    "first steer",
                    "second steer",
                ]
                assert steering_payloads[0]["attachments"] == [
                    {
                        "file_path": "/tmp/report.txt",
                        "file_name": "report.txt",
                        "file_type": "file",
                    }
                ]
                assert steering_payloads[1]["attachments"] == []
                assert all("mode" not in payload for payload in steering_payloads)
                assert all("message_id" in payload for payload in steering_payloads)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_plain_active_run_does_not_repeatedly_poll_empty_followup_queue(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                followup_gets: list[str] = []
                page.on(
                    "request",
                    lambda request: followup_gets.append(request.url)
                    if request.method == "GET"
                    and urlparse(request.url).path
                    and "/followups" in urlparse(request.url).path
                    else None,
                )
                await _open_active_run(
                    page, base_url, prompt="followup approval gate"
                )
                await page.wait_for_timeout(700)

                assert followup_gets == []
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_idle_enter_posts_a_steer_only_session_prompt(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                prompt_posts: list[dict[str, object]] = []
                followup_posts: list[str] = []
                page.on(
                    "request",
                    lambda request: prompt_posts.append(request.post_data_json)
                    if urlparse(request.url).path.endswith("/prompt")
                    and "/api/v1/sessions/" in urlparse(request.url).path
                    and request.method == "POST"
                    else followup_posts.append(request.url)
                    if "/followups" in urlparse(request.url).path
                    and request.method == "POST"
                    else None,
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _select_default_agent(page)
                await page.locator("#chat-input").fill("ordinary idle turn")
                await page.locator("#chat-input").press("Enter")

                await expect(
                    page.locator("#chat-messages").get_by_text(
                        "ordinary idle turn", exact=True
                    )
                ).to_have_count(1)
                assert len(prompt_posts) == 1
                assert prompt_posts[0]["content"] == "ordinary idle turn"
                assert "mode" not in prompt_posts[0]
                assert prompt_posts[0]["message_id"]
                assert followup_posts == []
            finally:
                await browser.close()



@pytest.mark.asyncio
@pytest.mark.parametrize("_iteration", range(1))
async def test_three_sessions_run_in_parallel_and_survive_rapid_switching(
    tmp_path: Path,
    _iteration: int,
) -> None:
    """Concurrent sessions must keep isolated, ordered, duplicate-free views."""

    parallel_started: list[str] = []
    parallel_release = asyncio.Event()
    async with _running_console(
        tmp_path,
        parallel_started=parallel_started,
        parallel_release=parallel_release,
    ) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                websocket_urls: list[str] = []
                page.on("websocket", lambda websocket: websocket_urls.append(websocket.url))
                session_ids = {
                    label: str((await _create_session(
                        page,
                        base_url,
                        title=f"Parallel {label}",
                    ))["id"])
                    for label in ("A", "B", "C")
                }
                await page.add_init_script(
                    "localStorage.setItem('bianbu_session_id', "
                    f"{json.dumps(session_ids['A'])})"
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function(
                    "() => sessionConnection.sockets.size === 2"
                )
                await page.evaluate(
                    """sessions => {
                        for (const [label, id] of Object.entries(sessions)) {
                            sessionsById.set(id, {
                                id, title: `Parallel ${label}`, agent_id: 'default'
                            });
                        }
                    }""",
                    session_ids,
                )

                run_ids: dict[str, str] = {}
                for label, owner in session_ids.items():
                    if label != "A":
                        await page.evaluate("owner => switchSession(owner)", owner)
                    await page.wait_for_function(
                            "owner => sessionId === owner && sessionRuntime.get(owner).opened && !sessionRuntime.get(owner).historyPending",
                        arg=owner,
                    )
                    await page.evaluate(
                        "document.getElementById('welcome-screen')?.remove()"
                    )
                    await page.locator("#chat-input").fill(f"parallel session {label}")
                    await page.locator("#send-btn").click()
                    active_run = await page.wait_for_function(
                        "owner => sessionActiveRequest[owner] || false",
                        arg=owner,
                    )
                    run_ids[label] = str(await active_run.json_value())

                # Exercise cache restore, stale history responses, and stream
                # re-attachment while all three Runs are still active.
                for label in ("A", "C", "B", "A", "C"):
                    await page.evaluate(
                        "owner => switchSession(owner)",
                        session_ids[label],
                    )
                    await page.wait_for_timeout(30)

                snapshots = {
                    label: await _wait_for_run(
                        page,
                        base_url,
                        run_id,
                        session_id=session_ids[label],
                    )
                    for label, run_id in run_ids.items()
                }
                assert all(item["status"] == "completed" for item in snapshots.values())
                assert set(parallel_started) == {"A", "B", "C"}

                for label, owner in session_ids.items():
                    await page.evaluate("owner => switchSession(owner)", owner)
                    await page.wait_for_function(
                        "owner => sessionId === owner && sessionRuntime.get(owner).opened && !sessionRuntime.get(owner).historyPending",
                        arg=owner,
                    )
                    expected = [
                        f"parallel session {label}",
                        f"START-{label} DONE-{label}",
                    ]
                    groups = page.locator(
                        "#chat-messages > .user-message-group, "
                        "#chat-messages > .bot-message-group"
                    )
                    await expect(groups).to_have_count(2)
                    assert await groups.locator(".msg-content").all_inner_texts() == expected
                    await expect(page.locator(
                        f".bot-message-group[data-request-id='{run_ids[label]}']"
                    )).to_have_count(1)
                    visible_text = await page.locator("#chat-messages").inner_text()
                    for other in ({"A", "B", "C"} - {label}):
                        assert f"START-{other}" not in visible_text
                        assert f"DONE-{other}" not in visible_text
                assert sorted(urlparse(url).path for url in websocket_urls) == [
                    "/api/v1/events/host",
                    "/api/v1/events/mux",
                ]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_approval_locks_composer_until_resolution(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                owner = str((await _create_session(page, base_url))["id"])
                followup_requests: list[str] = []
                await page.add_init_script(f"""(() => {{
                    localStorage.setItem('bianbu_session_id', {json.dumps(owner)});
                    localStorage.setItem('bianbu_active_runs', JSON.stringify({{{json.dumps(owner)}: 'approval-run'}}));
                }})();""")
                await page.route(
                    "**/api/v1/runs/approval-run",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"data": {
                            "id": "approval-run", "session_id": owner,
                            "agent_id": "default", "status": "waiting",
                            "content": "", "usage": None, "error": None,
                            "pending_actions": [], "created_at": 1, "updated_at": 2,
                        }}),
                    ),
                )
                page.on(
                    "request",
                    lambda request: followup_requests.append(request.url)
                    if f"/sessions/{owner}/prompt" in request.url
                    and request.method == "POST"
                    else None,
                )

                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_timeout(300)
                await page.evaluate(
                    """owner => trackApprovalEvent(owner, 'approval-run', {
                        type: 'approval_required', approval_id: 'approval-1',
                    })""",
                    owner,
                )
                approval_state = await page.evaluate(
                    """owner => ({pending: Array.from(
                        sessionPendingApprovals.get(owner)?.ids || []
                    )})""",
                    owner,
                )
                assert approval_state["pending"] == ["approval-1"], approval_state

                await expect(page.locator("#chat-input")).to_be_disabled()
                await expect(page.locator("#chat-input")).to_have_attribute(
                    "placeholder", "请先批准或拒绝待处理操作"
                )
                await expect(page.locator("#send-btn")).to_be_disabled()
                await page.evaluate("dispatchComposerSubmission()")
                await page.wait_for_timeout(50)
                assert followup_requests == []

                await page.evaluate(
                    """owner => trackApprovalEvent(owner, 'approval-run', {
                        type: 'approval_resolved', approval_id: 'approval-1',
                    })""",
                    owner,
                )
                await expect(page.locator("#chat-input")).to_be_enabled()
                await expect(page.locator("#chat-input")).to_have_attribute(
                    "placeholder", "输入消息，@ 切换专家，/ 临时使用技能"
                )

                followup_attempts = {"value": 0}

                async def fail_first_followup(route) -> None:
                    followup_attempts["value"] += 1
                    if followup_attempts["value"] == 1:
                        await page.evaluate(
                            """owner => trackApprovalEvent(owner, 'approval-run', {
                                type: 'approval_required',
                                approval_id: 'approval-during-retry',
                            })""",
                            owner,
                        )
                        await route.abort("failed")
                        return
                    await route.fulfill(
                        status=202,
                        content_type="application/json",
                        body='{"data":{"accepted":true}}',
                    )

                await page.route(
                    f"**/api/v1/sessions/{owner}/prompt",
                    fail_first_followup,
                )
                await page.locator("#chat-input").fill("do not retry while waiting")
                await page.evaluate("sendMessage()")
                assert followup_attempts["value"] == 1
                await expect(page.locator("#chat-input")).to_be_disabled()
                await expect(page.locator("#chat-input")).to_have_value(
                    "do not retry while waiting"
                )
                await page.evaluate(
                    """owner => trackApprovalEvent(owner, 'approval-run', {
                        type: 'approval_resolved',
                        approval_id: 'approval-during-retry',
                    })""",
                    owner,
                )
                await expect(page.locator("#chat-input")).to_be_enabled()

                stale_snapshot_applied = await page.evaluate(
                    """owner => {
                        const revision = approvalRevision(owner);
                        trackApprovalEvent(owner, 'approval-run', {
                            type: 'approval_required', approval_id: 'approval-2',
                        });
                        const pendingRevision = approvalRevision(owner);
                        trackApprovalEvent(owner, 'approval-run', {
                            type: 'approval_resolved', approval_id: 'approval-2',
                        });
                        const stalePendingApplied = setSessionPendingApprovals(
                            owner,
                            'approval-run',
                            [{type: 'approval_required', approval_id: 'approval-2'}],
                            pendingRevision,
                        );
                        return {revision, stalePendingApplied};
                    }""",
                    owner,
                )
                assert stale_snapshot_applied["stalePendingApplied"] is False
                await expect(page.locator("#chat-input")).to_be_enabled()

                await page.evaluate(
                    """owner => {
                        trackApprovalEvent(owner, 'new-run', {
                            type: 'approval_required', approval_id: 'new-approval',
                        });
                        clearSessionPendingApprovals(owner, 'old-run');
                    }""",
                    owner,
                )
                await expect(page.locator("#chat-input")).to_be_disabled()
                await page.evaluate(
                    """owner => trackApprovalEvent(owner, 'new-run', {
                        type: 'approval_resolved',
                        approval_id: 'new-approval',
                    })""",
                    owner,
                )
                await expect(page.locator("#chat-input")).to_be_enabled()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_approval_locks_composer_after_restore_only_for_owner_session(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                owner = str((await _create_session(page, base_url))["id"])
                other = str((await _create_session(page, base_url))["id"])
                await page.add_init_script(f"""(() => {{
                    localStorage.setItem('bianbu_session_id', {json.dumps(owner)});
                    localStorage.setItem('bianbu_active_runs', JSON.stringify({{{json.dumps(owner)}: 'restored-approval-run'}}));
                    window.EventSource = class {{ constructor(url) {{ this.url = url; }} close() {{}} }};
                }})();""")
                await page.route(
                    f"**/api/v1/sessions/{owner}",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"data": {
                            "id": owner,
                            "agent_id": "default",
                            "running": False,
                            "waiting": True,
                            "active_run_id": "restored-approval-run",
                            "pending_interaction": "approval",
                        }}),
                    ),
                )

                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate(
                    "([owner, requestId]) => restorePendingRun(owner, requestId)",
                    [owner, "restored-approval-run"],
                )
                await expect(page.locator("#chat-input")).to_be_disabled()
                await expect(page.locator("#chat-input")).to_have_attribute(
                    "placeholder", "请先批准或拒绝待处理操作"
                )

                await page.evaluate("sid => switchSession(sid)", other)
                await expect(page.locator("#chat-input")).to_be_enabled()

                blocked_prompt_posts: list[dict[str, object]] = []
                page.on(
                    "request",
                    lambda request: blocked_prompt_posts.append(
                        request.post_data_json or {}
                    )
                    if (
                        request.method == "POST"
                        and urlparse(request.url).path.endswith("/prompt")
                        and (request.post_data_json or {}).get("content")
                        == "must wait for approval"
                    )
                    else None,
                )
                await page.locator("#chat-input").fill("must wait for approval")
                await page.evaluate(
                    """owner => {
                        trackApprovalEvent(owner, 'race-run', {
                            type: 'approval_required', approval_id: 'race-approval',
                        });
                    }""",
                    other,
                )
                await page.evaluate("sendNewRun()")
                assert blocked_prompt_posts == []
                await expect(page.locator("#chat-input")).to_have_value(
                    "must wait for approval"
                )
                await page.evaluate(
                    """owner => trackApprovalEvent(owner, 'race-run', {
                        type: 'approval_resolved', approval_id: 'race-approval',
                    })""",
                    other,
                )

                await page.evaluate("sid => switchSession(sid)", owner)
                await expect(page.locator("#chat-input")).to_be_disabled()
                await expect(page.locator("#chat-input")).to_have_attribute(
                    "placeholder", "请先批准或拒绝待处理操作"
                )
            finally:
                await browser.close()



@pytest.mark.asyncio
async def test_skill_catalog_is_read_only_and_hostile_ids_are_inert(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                mutations: list[str] = []
                page.on("request", lambda request: mutations.append(request.url)
                        if urlparse(request.url).path.startswith("/api/v1/") and request.method != "GET" else None)
                hostile = "x');fetch('/api/v1/plugins/pwn',{method:'DELETE'});//"
                await page.route(
                    "**/api/v1/capabilities/skill",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "data": [
                                    {
                                        "id": hostile,
                                        "name": hostile,
                                        "display_name": "Review Skill",
                                        "description": "Review safely",
                                        "enabled": True,
                                    }
                                ]
                            }
                        ),
                    ),
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate("window.__consoleXss = 0")
                await page.locator('[data-view="skills"]').click()
                await expect(
                    page.locator("#skills-list", has_text="Review Skill")
                ).to_be_visible()
                await expect(page.locator("#skills-list button")).to_have_count(0)
                await expect(
                    page.locator('#skills-list [role="switch"]')
                ).to_have_count(0)
                assert not any("/plugins/pwn" in url for url in mutations)
                assert await page.locator("[onclick]").evaluate_all(
                    "els => els.filter(el => el.closest('#skills-list')).length"
                ) == 0
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_frontend_uses_expert_terminology_and_has_no_skill_toggle_commands(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")

                labels = {
                    "zh": ("专家", "创建专家"),
                    "en": ("Experts", "Create Expert"),
                }
                for language, (menu_label, create_label) in labels.items():
                    await page.evaluate("lang => setLanguage(lang)", language)
                    await expect(
                        page.locator('[data-view="agents"] [data-i18n="menu_agents"]')
                    ).to_have_text(menu_label)
                    await page.locator('[data-view="agents"]').click()
                    await expect(page.locator('[data-i18n="agents_create"]').first).to_have_text(
                        create_label
                    )

                await page.locator("#lang-toggle").click()
                await expect(page.locator("#lang-menu .lang-menu-item")).to_have_count(2)
                assert await page.locator(
                    "#lang-menu .lang-menu-item"
                ).evaluate_all("items => items.map(item => item.dataset.lang)") == ["zh", "en"]

                await page.evaluate("localStorage.setItem('bianbu_lang', 'fr')")
                await page.reload(wait_until="domcontentloaded")
                assert await page.evaluate("currentLang") == "zh"
                await expect(
                    page.locator('[data-view="agents"] [data-i18n="menu_agents"]')
                ).to_have_text("专家")

                await page.evaluate("setLanguage('zh')")
                await page.locator('[data-view="chat"]').click()
                # 「/」只列这个专家还没有的技能：专家自己的、所有专家固定启用的
                # 预设技能都不出现，命令列表整组下线。
                slash_state = await page.evaluate(
                    """() => {
                        availableSkills = [
                            {id: 'alpha-skill', name: 'alpha-skill', enabled: true, assignable: true},
                            {id: 'expert-skill', name: 'expert-skill', enabled: true, assignable: true},
                            {id: 'preset-skill', name: 'preset-skill', enabled: true, assignable: true, preset: true},
                            {id: 'disabled-skill', name: 'disabled-skill', enabled: false, assignable: true},
                        ];
                        activeAgent = {skills: ['expert-skill']};
                        return {
                            items: slashItems('/').map(item => item.cmd),
                            hasCommandTable: typeof SLASH_COMMANDS,
                        };
                    }"""
                )
                assert slash_state["items"] == ["alpha-skill"]
                assert slash_state["hasCommandTable"] == "undefined"
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_skills_command_selects_global_skills_and_sends_them_once(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                prompt_payloads: list[dict[str, object]] = []
                hostile = '<img src=x onerror="window.__skillXss=1">'

                async def skill_catalog(route) -> None:
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "data": [
                                    {
                                        "id": "global.skills/review",
                                        "name": "review",
                                        "display_name": "Global Review",
                                        "description": "Review the next request",
                                        "enabled": True,
                                        "assignable": True,
                                    },
                                    {
                                        "id": "global.skills/hostile",
                                        "name": hostile,
                                        "display_name": hostile,
                                        "description": hostile,
                                        "enabled": True,
                                        "assignable": True,
                                    },
                                    {
                                        "id": "global.skills/disabled",
                                        "name": "disabled",
                                        "display_name": "Disabled Skill",
                                        "enabled": False,
                                        "assignable": True,
                                    },
                                ]
                            }
                        ),
                    )

                async def prompt(route) -> None:
                    prompt_payloads.append(route.request.post_data_json or {})
                    await route.fulfill(
                        status=202,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "data": {
                                    "accepted": True,
                                    "message_id": "selected-skill-message",
                                    "status": "staged",
                                    "run_id": None,
                                }
                            }
                        ),
                    )

                await page.route("**/api/v1/capabilities/skill", skill_catalog)
                await page.route("**/api/v1/sessions/*/prompt", prompt)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate("window.__skillXss = 0")
                await _select_default_agent(page)

                await page.locator("#chat-input").fill("/skills")
                await page.locator("#chat-input").press("Enter")
                await expect(page.locator("#skill-picker")).to_be_visible()
                await expect(page.locator("#skill-picker .skill-picker-item")).to_have_count(2)
                active_option = await page.locator("#skill-picker-search").get_attribute(
                    "aria-activedescendant"
                )
                assert active_option
                assert await page.locator(f"#{active_option}").count() == 1
                assert await page.locator("#selected-skills").get_attribute("aria-label")
                await expect(page.locator("#skill-picker", has_text="Disabled Skill")).to_have_count(0)
                assert await page.locator("#skill-picker img").count() == 0
                assert await page.evaluate("window.__skillXss") == 0

                await page.mouse.click(5, 5)
                await expect(page.locator("#skill-picker")).to_be_hidden()
                # 失焦收起菜单有个 150ms 定时器：先让它跑完，否则它会在 fill 之后再收起。
                await page.wait_for_timeout(250)
                # `global.skills/review` 名字里带 "skills"，所以建议列表会命中它：别名
                # 必须赢过这一行，否则这个面板（输入区没有别的入口）只能靠 Escape 打开。
                await page.wait_for_function("() => availableSkills.length > 0")
                await page.locator("#chat-input").fill("/skills")
                await expect(page.locator("#slash-menu")).to_be_visible()
                await page.locator("#chat-input").press("Enter")
                await expect(page.locator("#skill-picker")).to_be_visible()

                await page.locator("#skill-picker-search").fill("Global Review")
                await page.locator("#skill-picker .skill-picker-item").click()
                await expect(page.locator("#selected-skills .selected-skill-chip")).to_have_count(1)
                await expect(page.locator("#chat-input")).to_have_value("")
                await page.locator("#skill-picker-search").press("Escape")

                await page.locator("#chat-input").fill("review this")
                await page.locator("#send-btn").click()
                for _ in range(200):
                    if prompt_payloads:
                        break
                    await asyncio.sleep(0.01)
                assert len(prompt_payloads) == 1
                assert prompt_payloads[0]["skill_ids"] == ["global.skills/review"]
                await expect(page.locator("#selected-skills .selected-skill-chip")).to_have_count(0)
                await page.locator("#chat-input").fill("ordinary follow-up")
                await page.locator("#send-btn").click()
                for _ in range(200):
                    if len(prompt_payloads) == 2:
                        break
                    await asyncio.sleep(0.01)
                assert len(prompt_payloads) == 2
                assert prompt_payloads[1]["skill_ids"] == []
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_rejected_skills_prompt_preserves_the_temporary_selection(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                prompt_payloads: list[dict[str, object]] = []

                await page.route(
                    "**/api/v1/capabilities/skill",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "data": [
                                    {
                                        "id": "global.skills/review",
                                        "name": "review",
                                        "display_name": "Global Review",
                                        "description": "Review the next request",
                                        "enabled": True,
                                        "assignable": True,
                                    }
                                ]
                            }
                        ),
                    ),
                )

                async def reject_prompt(route) -> None:
                    prompt_payloads.append(route.request.post_data_json or {})
                    await route.fulfill(
                        status=409,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "error": {
                                    "code": "prompt_rejected",
                                    "message": "try again",
                                }
                            }
                        ),
                    )

                await page.route("**/api/v1/sessions/*/prompt", reject_prompt)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _select_default_agent(page)
                await page.locator("#chat-input").fill("/skills")
                await page.locator("#chat-input").press("Enter")
                await page.locator("#skill-picker .skill-picker-item").click()
                await page.locator("#skill-picker-search").press("Escape")
                await page.locator("#chat-input").fill("keep my selection")
                await page.locator("#send-btn").click()

                for _ in range(200):
                    if prompt_payloads:
                        break
                    await asyncio.sleep(0.01)
                assert len(prompt_payloads) == 1
                assert prompt_payloads[0]["skill_ids"] == ["global.skills/review"]
                await expect(page.locator("#chat-input")).to_have_value("keep my selection")
                await expect(page.locator("#selected-skills .selected-skill-chip")).to_have_count(1)
            finally:
                await browser.close()



@pytest.mark.asyncio
async def test_sidebar_follows_light_and_dark_theme(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate("localStorage.setItem('bianbu_theme', 'dark')")
                await page.reload(wait_until="domcontentloaded")

                dark_background = await page.locator("#sidebar").evaluate(
                    "element => getComputedStyle(element).backgroundColor"
                )
                await page.locator("#theme-toggle").click()
                light_background = await page.locator("#sidebar").evaluate(
                    "element => getComputedStyle(element).backgroundColor"
                )

                assert dark_background == "rgb(10, 10, 10)"
                assert light_background == "rgb(255, 255, 255)"
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_tools_and_skills_are_separate_navigation_pages(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                requested_paths: list[str] = []
                page.on(
                    "request",
                    lambda request: requested_paths.append(
                        urlparse(request.url).path
                    ),
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")

                await page.locator('[data-view="tools"]').click()
                await expect(page.locator("#view-tools")).to_have_class(
                    re.compile(r"\bactive\b")
                )
                await expect(page.locator("#view-skills")).not_to_have_class(
                    re.compile(r"\bactive\b")
                )
                await expect(page.locator("#view-tools #tools-list")).to_have_count(1)
                await expect(page.locator("#view-tools #skills-list")).to_have_count(0)
                await page.wait_for_function(
                    "() => performance.getEntriesByType('resource').some(entry => new URL(entry.name).pathname === '/api/v1/capabilities/tool')"
                )

                await page.locator('[data-view="skills"]').click()
                await expect(page.locator("#view-skills")).to_have_class(
                    re.compile(r"\bactive\b")
                )
                await expect(page.locator("#view-tools")).not_to_have_class(
                    re.compile(r"\bactive\b")
                )
                await expect(page.locator("#view-skills #skills-list")).to_have_count(1)
                await expect(page.locator("#view-skills #tools-list")).to_have_count(0)
                await page.wait_for_function(
                    "() => performance.getEntriesByType('resource').some(entry => new URL(entry.name).pathname === '/api/v1/capabilities/skill')"
                )

                assert "/api/v1/capabilities/tool" in requested_paths
                assert "/api/v1/capabilities/skill" in requested_paths
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_tool_intros_follow_the_console_language_and_keep_hover_detail(
    tmp_path: Path,
) -> None:
    """Tool intros switch with the UI language; hover still shows the full text."""

    async def capabilities(route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "status": "success",
                "data": [
                    {
                        "id": "web_search", "name": "web_search",
                        "display_name": "web_search",
                        "description": "Search the web; use category=news for current events.",
                        "summaries": {
                            "zh": "搜索网页；查时事用 category=news。",
                            "en": "Search the web; use category=news for current events.",
                        },
                        "version": "0.1.111", "origin": "standalone", "plugin_id": None,
                        "enabled": True, "assignable": True, "preset": False,
                        "unavailable_reason": "",
                    },
                    {
                        "id": "filesystem.read", "name": "filesystem.read",
                        "display_name": "Read File",
                        "description": "Read a file from the MCP server, with a long description the card clamps.",
                        "version": "1.0.0", "origin": "mcp", "plugin_id": "mcp.filesystem",
                        "enabled": True, "assignable": True, "preset": False,
                        "unavailable_reason": "",
                    },
                ],
            }),
        )

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/api/v1/capabilities/tool**", capabilities)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="tools"]').click()

                intro = page.locator('[data-tool-id="web_search"] p')
                await expect(intro).to_have_text("搜索网页；查时事用 category=news。")
                await expect(intro).to_have_attribute(
                    "title", "搜索网页；查时事用 category=news。"
                )

                await page.evaluate("setLanguage('en')")
                await expect(intro).to_have_text(
                    "Search the web; use category=news for current events."
                )

                # A tool Runtime does not own keeps its own description; the
                # hover title is the unclamped text.
                await expect(
                    page.locator('[data-tool-id="filesystem.read"] p')
                ).to_have_attribute("title", re.compile("Read a file from the MCP server"))
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_plugin_lifecycle_admin_view_invalidates_console_descriptors(
    tmp_path: Path,
) -> None:
    """The admin view lists, installs, inspects, and deletes Plugin packages."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                calls: list[tuple[str, str]] = []
                plugins = [{
                    "id": "vendor.old",
                    "members": [
                        {"id": "vendor.old/check", "kind": "hook", "name": "check"},
                        {"id": "vendor.old/review", "kind": "skill", "name": "review"},
                        {"id": "vendor.old/weather", "kind": "mcp", "name": "weather"},
                    ],
                    "source": "/plugins/old",
                }]

                async def route_plugins(route, request) -> None:
                    path = urlparse(request.url).path
                    calls.append((request.method, path))
                    if request.method == "POST":
                        installed = {
                            "id": "vendor.new",
                            "members": ["hook"],
                            "source": "/plugins/new",
                        }
                        plugins.append(installed)
                        await route.fulfill(
                            status=201,
                            content_type="application/json",
                            body=json.dumps({"data": installed}),
                        )
                    elif request.method == "DELETE":
                        plugins[:] = [item for item in plugins if item["id"] != "vendor.old"]
                        await route.fulfill(status=204, body="")
                    elif path.endswith("/vendor.old"):
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({"data": plugins[0]}),
                        )
                    else:
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({
                                "data": plugins,
                                "page": {"number": 1, "size": 100, "has_more": False},
                            }),
                        )

                await page.route("**/api/v1/plugins**", route_plugins)
                # 插件自带的 MCP 状态来自这一份快照，插件页要用它打状态标签。
                await page.route(
                    "**/api/v1/mcp-servers",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"status": "success", "data": [{
                            "id": "vendor.old/weather", "display_name": "Plugin weather",
                            "owner_plugin_id": "vendor.old", "status": "running",
                            "transport": "stdio", "removable": False,
                            "tools": [{"id": "w1", "name": "w1"}],
                        }]}),
                    ),
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="plugins"]').click()
                await expect(page.locator("#view-plugins")).to_have_class(
                    re.compile(r"\bactive\b")
                )
                await expect(page.locator(".plugin-admin-card", has_text="vendor.old")).to_be_visible()
                old_members = page.locator(".plugin-admin-card", has_text="vendor.old")
                await expect(old_members).to_contain_text("Hook · check")
                await expect(old_members).to_contain_text("Skill · review")
                # MCP 成员不再混在成员清单里，而是带实时状态单独一段，
                # 状态用生命周期词而不是 Runtime 的原始 status 原文。
                await expect(old_members).to_contain_text("vendor.old/weather")
                await expect(old_members).to_contain_text("就绪")
                await page.locator(".plugin-admin-inspect").click()
                await expect(page.locator("#plugin-admin-details")).to_contain_text("review")

                await page.locator("#plugin-install-source").fill("/plugins/new")
                await page.locator("#plugin-install-btn").click()
                await expect(page.locator(".plugin-admin-card", has_text="vendor.new")).to_be_visible()
                old_card = page.locator(".plugin-admin-card", has_text="vendor.old")
                await old_card.locator(".plugin-admin-delete").click()
                await expect(old_card).to_have_count(0)
                assert ("GET", "/api/v1/plugins/vendor.old") in calls
                assert ("POST", "/api/v1/plugins") in calls
                assert ("DELETE", "/api/v1/plugins/vendor.old") in calls
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_standalone_skill_and_mcp_views_hide_plugin_members(tmp_path: Path) -> None:
    """插件成员只在插件页出现：技能页过滤，MCP 页过滤并转述去向。"""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(
                    "**/api/v1/capabilities/skill",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"status": "success", "data": [{
                            "id": "office.core/review", "name": "review",
                            "display_name": "Plugin review", "plugin_id": "office.core",
                        }]}),
                    ),
                )
                await page.route(
                    "**/api/v1/mcp-servers",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"status": "success", "data": [{
                            "id": "office.core/weather", "display_name": "Plugin weather",
                            "owner_plugin_id": "office.core", "tools": [],
                            "status": "running", "transport": "stdio", "removable": False,
                        }]}),
                    ),
                )
                await page.route(
                    "**/api/v1/plugins",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"status": "success", "data": [{
                            "id": "office.core",
                            "members": [{"id": "office.core/weather", "kind": "mcp", "name": "weather"}],
                        }]}),
                    ),
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="skills"]').click()
                await expect(page.locator("#skills-list")).not_to_contain_text("Plugin review")
                await page.locator('[data-view="mcp"]').click()
                await expect(page.locator("#mcp-list")).not_to_contain_text("Plugin weather")
                await expect(page.locator("#mcp-plugin-owned-note")).to_contain_text(
                    "这里只管理独立 MCP"
                )
                await expect(page.locator("#mcp-plugin-owned-note")).to_contain_text("（1）")
                # 同一台 MCP 的状态在插件页随插件显示，用的是同一套生命周期词，
                # 而不是 Runtime 的原始 status 原文。
                await page.locator('[data-view="plugins"]').click()
                plugin_card = page.locator(".plugin-admin-card", has_text="office.core")
                await expect(plugin_card).to_contain_text("office.core/weather")
                await expect(plugin_card).to_contain_text("就绪")
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_mcp_has_dedicated_command_installation_page(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")

                await page.locator('[data-view="mcp"]').click()

                await expect(page.locator("#view-mcp")).to_have_class(
                    re.compile(r"\bactive\b")
                )
                await expect(page.locator("#mcp-install-name")).to_be_visible()
                await expect(page.locator("#mcp-install-command")).to_be_visible()
                await expect(page.locator("#mcp-install-button")).to_be_visible()
                await expect(page.locator("#mcp-refresh-button")).to_be_visible()
                await expect(page.locator("#mcp-list")).to_have_count(1)

                await page.locator('[data-view="agents"]').click()
                await page.locator('.agent-card[data-agent-id="default"]').click()
                await expect(page.locator("#agent-form-mcps")).to_be_visible()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_mcp_page_explains_the_lifecycle_instead_of_one_status_word(
    tmp_path: Path,
) -> None:
    """MCP 页要说清「现在到哪一步」：只显示 preparing/not_loaded，用户盯着一动
    不动的徽章也不知道是在装 116MB 的 npm 包，还是根本没有 Worker 加载它。"""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(
                    "**/api/v1/mcp-servers",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"status": "success", "data": [
                            {
                                "id": "mcp.bilibili-search",
                                "display_name": "bilibili-search",
                                "command": "npx bilibili-mcp-js",
                                "transport": "stdio", "tools": [],
                                "status": "preparing", "state": "preparing",
                                "phase": "installing", "ready": False,
                                "tools_count": 0, "detail": "",
                                "source_kind": "standalone", "owner_plugin_id": None,
                                "removable": True, "health_checked_at": None,
                            },
                            {
                                "id": "mcp.notes",
                                "display_name": "notes",
                                "command": "notes-mcp",
                                "transport": "stdio", "tools": [],
                                "status": "not_loaded", "state": "not_loaded",
                                "phase": "", "ready": False, "tools_count": 0,
                                "detail": "",
                                "source_kind": "standalone", "owner_plugin_id": None,
                                "removable": True, "health_checked_at": None,
                            },
                        ]}),
                    ),
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="mcp"]').click()

                preparing = page.locator('[data-mcp-id="mcp.bilibili-search"]')
                await expect(preparing).to_contain_text("准备中")
                # phase 是唯一能解释「还在等什么」的东西。
                await expect(preparing).to_contain_text("正在安装依赖")

                # not_loaded 不是一个死状态：Runtime 会在 Worker 就绪时自动
                # 准备它，用户也可以点刷新立即重试。
                not_loaded = page.locator('[data-mcp-id="mcp.notes"]')
                await expect(not_loaded).to_contain_text("未加载")
                await expect(not_loaded).to_contain_text("Runtime 会在 Worker 就绪时自动准备")
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_mcp_page_keeps_looking_after_an_install(tmp_path: Path) -> None:
    """装完那一下看到的是控制面目录，Worker 还没把新 Server 发布出来。

    ``mcp add`` 的重载有 5 秒上界，响应可以比 Worker 先到，于是刚装完刷新只能
    看到 not_loaded。页面必须自己接着看，不许把这个状态留给用户去猜。
    """

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                state = {"installed": False, "reads": 0}
                installed = {
                    "id": "mcp.notes", "display_name": "notes",
                    "command": "notes-mcp", "transport": "stdio", "tools": [],
                    "status": "not_loaded", "state": "not_loaded", "phase": "",
                    "ready": False, "tools_count": 0, "detail": "",
                    "source_kind": "standalone", "owner_plugin_id": None,
                    "removable": True, "health_checked_at": None,
                }

                def fulfill(route, status, payload):
                    return route.fulfill(
                        status=status,
                        content_type="application/json",
                        body=json.dumps(payload),
                    )

                def handler(route):
                    if route.request.method == "POST":
                        state["installed"] = True
                        return fulfill(route, 201, {"status": "success", "data": installed})
                    if not state["installed"]:
                        return fulfill(route, 200, {"status": "success", "data": []})
                    state["reads"] += 1
                    row = dict(installed)
                    if state["reads"] >= 2:
                        row.update({"status": "ready", "state": "ready", "ready": True})
                    return fulfill(route, 200, {"status": "success", "data": [row]})

                await page.route("**/api/v1/mcp-servers", handler)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="mcp"]').click()
                await page.locator("#mcp-install-name").fill("notes")
                await page.locator("#mcp-install-command").fill("notes-mcp")
                await page.locator("#mcp-install-button").click()

                # 谁也没点刷新：页面自己等到 Worker 发布完。
                # 徽章要单独看：not_loaded 的提示里也有「就绪」两个字。
                badge = page.locator('[data-mcp-id="mcp.notes"] span').first
                await expect(badge).to_have_text("就绪", timeout=20000)
                assert state["reads"] >= 2
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_agent_editor_round_trips_mcp_wildcard(tmp_path: Path) -> None:
    """Select-all creates an MCP wildcard that survives health refreshes."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                agent = {
                    "id": "default",
                    "name": "Default",
                    "description": "",
                    "soul": "",
                    "model": {
                        "provider": "browser",
                        "name": "browser-contract",
                    },
                    "tools": [],
                    "skills": [],
                    "hooks": [],
                    "mcps": [],
                    "metadata": {},
                    "permissions": {},
                    "avatar": "🤖",
                    "available": True,
                    "unavailable_reason": "",
                }
                update_payloads: list[dict[str, object]] = []

                async def agents_collection(route) -> None:
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"data": [agent]}),
                    )

                async def update_agent(route) -> None:
                    payload = route.request.post_data_json
                    update_payloads.append(payload)
                    agent.update(payload)
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"data": agent}),
                    )

                mcp_entries = [
                    {
                        "id": "mcp.filesystem",
                        "name": "mcp.filesystem",
                        "display_name": "Filesystem",
                        "enabled": True,
                    },
                    {
                        "id": "mcp.weather",
                        "name": "mcp.weather",
                        "display_name": "Weather",
                        "enabled": True,
                    },
                ]

                async def mcp_catalog(route) -> None:
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"data": mcp_entries}),
                    )

                await page.route("**/api/v1/agents", agents_collection)
                await page.route("**/api/v1/agents/default", update_agent)
                await page.route("**/api/v1/mcp-servers", mcp_catalog)
                await page.route(
                    "**/api/v1/mcp-health-checks",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"data": mcp_entries}),
                    ),
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="agents"]').click()
                await page.locator('.agent-card[data-agent-id="default"]').click()

                mcp_options = page.locator(
                    '#agent-form-mcps input[type="checkbox"]'
                )
                await expect(mcp_options).to_have_count(2)
                await expect(mcp_options.nth(0)).not_to_be_checked()
                await expect(mcp_options.nth(1)).not_to_be_checked()
                await page.locator("#agent-form-mcps").locator("..").locator(
                    "button"
                ).click()
                await expect(mcp_options.nth(0)).to_be_checked()
                await expect(mcp_options.nth(1)).to_be_checked()

                await page.locator("#agent-editor-save").click()
                await expect(page.locator("#agent-editor-overlay")).to_be_hidden()
                assert update_payloads[0]["mcps"] == ["*"]
                assert update_payloads[0]["plugins"] == []

                await page.locator('.agent-card[data-agent-id="default"]').click()
                await expect(mcp_options.nth(0)).to_be_checked()
                await expect(mcp_options.nth(1)).to_be_checked()
                # The MCP view owns the health refresh; it must not disturb the
                # Expert editor that is open on top of it.
                await page.evaluate("refreshMcp()")
                await expect(mcp_options.nth(0)).to_be_checked()
                await expect(mcp_options.nth(1)).to_be_checked()
                await page.locator("#agent-editor-save").click()
                await expect(page.locator("#agent-editor-overlay")).to_be_hidden()
                # A health refresh does not change the stored wildcard, so a
                # no-op save must not issue another PATCH.
                assert update_payloads == [{"plugins": [], "mcps": ["*"]}]
                assert agent["mcps"] == ["*"]

                await page.locator('.agent-card[data-agent-id="default"]').click()
                await expect(mcp_options.nth(0)).to_be_checked()
                await expect(mcp_options.nth(1)).to_be_checked()
                await mcp_options.nth(1).uncheck()
                await page.locator("#agent-editor-save").click()
                await expect(page.locator("#agent-editor-overlay")).to_be_hidden()
                assert update_payloads[1]["mcps"] == ["mcp.filesystem"]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_agent_role_cards_render_before_slow_mcp_health_is_checked_non_blocking(
    tmp_path: Path,
) -> None:
    """Awaiting MCP health from the role loader must not hide usable role cards."""

    health_started = asyncio.Event()
    health_gate = asyncio.Event()
    health_started_at: list[float] = []
    async with _running_console(
        tmp_path,
        mcp_health_started=health_started,
        mcp_health_gate=health_gate,
        mcp_health_started_at=health_started_at,
    ) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                mcp_requests: list[float] = []
                loop = asyncio.get_running_loop()
                page.on(
                    "request",
                    lambda request: mcp_requests.append(loop.time())
                    if request.method == "GET"
                    and urlparse(request.url).path == "/api/v1/mcp-servers"
                    else None,
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="agents"]').click()

                await expect(page.locator('.agent-card[data-agent-id="default"]')).to_be_visible()
                role_rendered_at = loop.time()
                await expect(page.locator("#agents-loading")).to_be_hidden()
                await asyncio.wait_for(health_started.wait(), timeout=1)

                assert len(mcp_requests) == 1
                assert health_started_at
                assert mcp_requests[0] <= role_rendered_at
                assert not health_gate.is_set()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_mcp_view_refresh_owns_mcp_health_status(tmp_path: Path) -> None:
    """The refresh button in the MCP view is the only owner of MCP health state.

    One click issues exactly one health POST, keeps the button busy while that
    POST is in flight, and reports the moment the check happened.
    """

    health_started = asyncio.Event()
    health_gate = asyncio.Event()
    async with _running_console(
        tmp_path,
        mcp_health_started=health_started,
        mcp_health_gate=health_gate,
    ) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                refresh_requests: list[str] = []
                health_bodies: list[dict] = []

                async def capture_health_body(response) -> None:
                    if (
                        response.request.method == "POST"
                        and urlparse(response.url).path == "/api/v1/mcp-health-checks"
                    ):
                        health_bodies.append(await response.json())

                page.on(
                    "response",
                    lambda response: asyncio.create_task(capture_health_body(response)),
                )
                page.on(
                    "request",
                    lambda request: refresh_requests.append(request.url)
                    if request.method == "POST"
                    and urlparse(request.url).path
                    == "/api/v1/mcp-health-checks"
                    else None,
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="mcp"]').click()
                await asyncio.wait_for(health_started.wait(), timeout=5)
                requests_before = len(refresh_requests)

                refresh = page.locator("#mcp-refresh-button")
                async with page.expect_request(
                    lambda request: request.method == "POST"
                    and urlparse(request.url).path == "/api/v1/mcp-health-checks"
                ):
                    await refresh.click()
                    await expect(refresh).to_be_disabled()
                    await expect(refresh).to_have_attribute("aria-busy", "true")

                health_gate.set()
                assert len(refresh_requests) == requests_before + 1
                await expect(refresh).to_be_enabled()
                await expect(refresh).to_have_attribute("aria-busy", "false")
                for _ in range(50):
                    if health_bodies:
                        break
                    await asyncio.sleep(0.1)
                assert health_bodies
                checked_at_values = {
                    item["health_checked_at"]
                    for body in health_bodies
                    for item in body.get("data", [])
                    if item.get("health_checked_at")
                }
                assert checked_at_values
                status = await page.locator("#mcp-health-status").text_content()
                assert any(value in status for value in checked_at_values)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_mcp_view_refresh_failure_restores_button_and_localized_status(
    tmp_path: Path,
) -> None:
    """A failed health POST clears busy state and prefixes the localized error."""

    async with _running_console(
        tmp_path,
        mcp_health_check_error="health endpoint unavailable",
    ) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate("currentLang = 'zh'")
                await page.locator('[data-view="mcp"]').click()
                await page.locator("#mcp-refresh-button").click()

                refresh = page.locator("#mcp-refresh-button")
                await expect(refresh).to_be_enabled()
                await expect(refresh).to_have_attribute("aria-busy", "false")
                await expect(page.locator("#mcp-health-status")).to_contain_text(
                    "刷新 MCP 状态失败"
                )
                await expect(page.locator("#mcp-health-status")).to_contain_text(
                    "health endpoint unavailable"
                )
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_unfinished_header_icons_are_visible_but_not_links(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")

                for selector in ("#docs-link", "#website-icon", "#github-icon"):
                    icon = page.locator(selector)
                    await expect(icon).to_be_visible()
                    assert await icon.evaluate("element => element.tagName") == "SPAN"
                    assert await icon.get_attribute("href") is None
                    assert await icon.evaluate("element => element.closest('a')") is None
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_stale_persisted_session_starts_default_new_chat_without_404_requests(
    tmp_path: Path,
) -> None:
    stale_session_id = "session_stale_browser_state"
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                stale_requests: list[str] = []
                page.on(
                    "request",
                    lambda request: stale_requests.append(request.url)
                    if f"/sessions/{stale_session_id}" in request.url
                    else None,
                )
                await page.add_init_script(
                    "localStorage.setItem('bianbu_session_id', "
                    f"{json.dumps(stale_session_id)})"
                )

                await page.goto(f"{base_url}/chat", wait_until="networkidle")
                await page.wait_for_function(
                    "() => sessionId === null && isNewChatDraft()"
                )

                assert stale_requests == []
                assert await page.evaluate(
                    "localStorage.getItem('bianbu_session_id')"
                ) is None
                # The missing session must not bounce the user into the picker.
                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                await expect(page.locator("#active-agent-bar")).to_be_visible()
                await expect(page.locator("#chat-input")).to_be_enabled()
                assert await page.evaluate("() => newChatDraft?.agent?.id") == "default"
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_sessions_share_history_and_restore_locked_agent(
    tmp_path: Path,
) -> None:
    writer = AgentSpec(
        id="writer",
        name="Writer",
        metadata={"avatar": "📝"},
    )
    async with _running_console(tmp_path, (writer,)) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await _create_session(
                    page,
                    base_url,
                    title="Default chat",
                )
                writer_session = await _create_session(
                    page,
                    base_url,
                    agent_id="writer",
                    title="Writing plan",
                )
                writer_session_id = str(writer_session["id"])

                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => agentsLoaded")
                await page.evaluate("() => openSessionPanel()")
                await page.evaluate("() => closeChatAgentMenu()")
                await expect(page.locator(".session-agent-group")).to_have_count(0)
                await expect(page.locator("#session-list > .session-item")).to_have_count(2)
                await expect(page.locator(".session-item small")).to_have_count(0)
                session_list_text = await page.locator("#session-list").inner_text()
                assert all(
                    label not in session_list_text
                    for label in ("今天", "昨天", "更早")
                )

                await page.locator(
                    f'.session-item[data-session-id="{writer_session_id}"]'
                ).click()
                await expect(page.locator("#active-agent-bar")).to_be_visible()
                await expect(page.locator("#active-agent-bar")).to_contain_text(
                    "Writer"
                )
                await expect(page.locator("#active-agent-bar")).to_contain_text(
                    "已绑定"
                )
                await expect(page.locator("#chat-agent-toggle")).to_have_count(0)
                assert await page.evaluate(
                    "localStorage.getItem('bianbu_session_agents')"
                ) is None

                for index in range(51):
                    await _create_session(page, base_url)
                await page.evaluate("closeSessionPanel()")
                await page.reload(wait_until="domcontentloaded")
                await expect(page.locator("#active-agent-bar")).to_contain_text(
                    "Writer"
                )

                await _open_agent_chooser(page)
                await expect(page.locator("#chat-agent-menu")).to_be_visible()
                await expect(page.locator("#chat-agent-menu")).to_contain_text(
                    "Writer"
                )

                await page.evaluate("navigateTo('agents')")
                writer_card = page.locator(".agent-card").filter(has_text="Writer")
                previous_session = await page.evaluate("sessionId")
                await writer_card.get_by_role(
                    "button", name="使用并新建会话"
                ).click()
                await page.wait_for_function("() => isNewChatDraft()")
                assert await page.evaluate("sessionId") is None
                await page.wait_for_function("() => activeAgent?.id === 'writer'")
                await page.locator("#chat-input").fill("start writing")
                await page.locator("#send-btn").click()
                await page.wait_for_function(
                    "previous => !isNewChatDraft() && sessionId !== previous",
                    arg=previous_session,
                )
                current_session = await page.evaluate("sessionId")
                assert current_session != previous_session
                records = await _list_sessions(page, base_url)
                assert any(
                    item["id"] == current_session
                    and item["agent_id"] == "writer"
                    for item in records
                )
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_unsent_new_chat_draft_is_discarded_on_reload(
    tmp_path: Path,
) -> None:
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (writer,)) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                created = await _create_session(page, base_url)
                saved_session_id = str(created["id"])
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate(
                    "sid => localStorage.setItem('bianbu_session_id', sid)",
                    saved_session_id,
                )
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_function("() => activeAgent?.id === 'default'")
                await _open_agent_chooser(page)
                await page.locator(
                    ".chat-agent-option", has_text="Writer"
                ).click()
                assert await page.evaluate("isNewChatDraft()") is True

                await page.reload(wait_until="domcontentloaded")

                await page.wait_for_function(
                    "sid => sessionId === sid", arg=saved_session_id
                )
                assert await page.evaluate("isNewChatDraft()") is False
                await page.wait_for_function("() => activeAgent?.id === 'default'")
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_startup_stale_session_cleanup_keeps_newer_stored_pointer(
    tmp_path: Path,
) -> None:
    """Startup bookkeeping must not clear a pointer written after the load.

    The console decides "the stored session is gone" from the pointer it read
    while booting.  That pass is asynchronous, so a slow settings fetch lets a
    newer pointer land first; dropping the key unconditionally would then
    discard a session the user is actually on.
    """
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (writer,)) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                release = asyncio.Event()

                async def _hold_settings(route) -> None:
                    await release.wait()
                    await route.continue_()

                await page.route("**/api/v1/settings", _hold_settings)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                created = await _create_session(page, base_url)
                saved_session_id = str(created["id"])
                await page.evaluate(
                    "sid => localStorage.setItem('bianbu_session_id', sid)",
                    saved_session_id,
                )
                # Let the stalled startup pass finish: it finds the session it
                # read (none) missing and installs a background draft.
                release.set()
                await page.wait_for_function("() => isNewChatDraft()")
                assert await page.evaluate(
                    "localStorage.getItem('bianbu_session_id')"
                ) == saved_session_id

                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_function(
                    "sid => sessionId === sid", arg=saved_session_id
                )
                assert await page.evaluate("isNewChatDraft()") is False
                await page.wait_for_function("() => activeAgent?.id === 'default'")
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_rejected_first_prompt_keeps_draft_with_bound_empty_session(
    tmp_path: Path,
) -> None:
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (writer,)) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                stored = await page.evaluate(
                    "localStorage.getItem('bianbu_session_id')"
                )
                before = await _list_sessions(page, base_url)
                await _open_agent_chooser(page)
                await page.locator(
                    ".chat-agent-option", has_text="Writer"
                ).click()
                await page.route(
                    "**/api/v1/sessions/*/prompt",
                    lambda route: route.fulfill(
                        status=422,
                        content_type="application/json",
                        body=json.dumps({
                            "error": {
                                "code": "validation_error",
                                "message": "synthetic rejection",
                                "details": {},
                            }
                        }),
                    ),
                )
                await page.locator("#chat-input").fill("retry me")
                await page.locator("#send-btn").click()
                await expect(page.locator("#chat-input")).to_have_value(
                    "retry me"
                )

                assert await page.evaluate("isNewChatDraft()") is True
                assert await page.evaluate(
                    "localStorage.getItem('bianbu_session_id')"
                ) == stored
                after = await _list_sessions(page, base_url)
                assert len(after) == len(before) + 1
                assert after[0]["agent_id"] == "writer"
                await page.wait_for_function("() => activeAgent?.id === 'writer'")
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_deleting_last_session_starts_default_new_chat(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                created = await _create_session(page, base_url)
                only_session_id = str(created["id"])
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate(
                    "sid => localStorage.setItem('bianbu_session_id', sid)",
                    only_session_id,
                )
                await page.reload(wait_until="domcontentloaded")
                await page.locator("#session-toggle-btn").click()
                row = page.locator(
                    f'.session-item[data-session-id="{only_session_id}"]'
                )
                await expect(row).to_be_visible()
                await row.locator(".session-delete").click()
                await page.locator(".confirm-btn-ok").click()
                await page.wait_for_function(
                    "() => sessionId === null && isNewChatDraft()"
                )

                assert await page.evaluate("sessionId") is None
                records = await _list_sessions(page, base_url)
                assert records == []
                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                await expect(page.locator("#chat-input")).to_be_enabled()
                assert await page.evaluate("() => newChatDraft?.agent?.id") == "default"
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_composer_warns_but_still_uploads_an_image_no_model_can_read(
    tmp_path: Path,
) -> None:
    """Attaching never blocks: the console only warns when vision is missing.

    The Runtime answers `GET .../vision` for the Expert's model, so the console
    can tell the user the model cannot read images, but the bytes still land in
    the workspace for a Tool or a later model to use.
    """

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                created = await _create_session(page, base_url)
                session_id = str(created["id"])
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate(
                    "sid => localStorage.setItem('bianbu_session_id', sid)",
                    session_id,
                )
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_function(
                    "sid => sessionId === sid", arg=session_id
                )

                uploads: list[str] = []

                async def record_upload(route) -> None:
                    uploads.append(route.request.url)
                    await route.continue_()

                await page.route(
                    "**/api/v1/sessions/*/attachments", record_upload
                )
                await page.locator("#attach-btn").click()
                await expect(page.locator("#attach-menu")).to_be_visible()
                await page.set_input_files(
                    "#composer-file-input",
                    {
                        "name": "a.png",
                        "mimeType": "image/png",
                        "buffer": b"\x89PNG\r\n\x1a\n" + b"0" * 32,
                    },
                )
                await expect(page.locator("#composer-notice")).to_have_text(
                    "当前模型不支持图片,请切换支持图片的模型"
                )
                # The warning never blocked the upload.
                assert uploads != []
                await expect(
                    page.locator("#attachment-preview .att-thumb")
                ).to_have_count(1)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_attach_menu_opens_above_the_button(tmp_path: Path) -> None:
    """The upload menu is anchored to the clip, not floating over the composer.

    It was placed with a hard-coded offset from the composer row, so it opened
    far from the button that owns it.  The menu must sit directly above the
    button and stay inside the viewport.
    """

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 800})
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_selector("#attach-btn")
                await page.locator("#attach-btn").click()
                menu = page.locator("#attach-menu")
                await expect(menu).to_be_visible()

                button_box = await page.locator("#attach-btn").bounding_box()
                menu_box = await menu.bounding_box()
                assert button_box and menu_box
                # Sits above the button...
                assert menu_box["y"] + menu_box["height"] <= button_box["y"] + 1
                # ...and within a couple of pixels of its right edge.
                button_right = button_box["x"] + button_box["width"]
                menu_right = menu_box["x"] + menu_box["width"]
                assert abs(menu_right - button_right) <= 2
                # ...and is not clipped off the left of the viewport.
                assert menu_box["x"] >= 0
            finally:
                await browser.close()


async def test_uploaded_image_survives_a_session_reload(tmp_path: Path) -> None:
    """The image the composer shows must come back when history is replayed.

    Live rendering reads the local pending attachment, which carried the
    preview URL; history is rebuilt from the event log, so the server has to
    derive the same URL or the image disappears on reload.
    """

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                created = await _create_session(page, base_url)
                session_id = str(created["id"])
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate(
                    "sid => localStorage.setItem('bianbu_session_id', sid)",
                    session_id,
                )
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_function(
                    "sid => sessionId === sid", arg=session_id
                )

                await page.locator("#attach-btn").click()
                await page.set_input_files(
                    "#composer-file-input",
                    {
                        "name": "shot.png",
                        "mimeType": "image/png",
                        "buffer": b"\x89PNG\r\n\x1a\n" + b"0" * 32,
                    },
                )
                await expect(
                    page.locator("#attachment-preview .att-thumb")
                ).to_have_count(1)
                await page.locator("#chat-input").fill("what is this")
                await page.locator("#send-btn").click()
                await page.wait_for_function(
                    "() => document.querySelectorAll('.bot-message-group').length > 0"
                )
                await expect(
                    page.locator(".user-msg-attachments .user-msg-image")
                ).to_have_count(1)

                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_function(
                    "sid => sessionId === sid", arg=session_id
                )
                await expect(
                    page.locator(".user-msg-attachments .user-msg-image")
                ).to_have_count(1)
                src = await page.locator(
                    ".user-msg-attachments .user-msg-image"
                ).first.get_attribute("src")
                assert src and "/attachments/" in src
            finally:
                await browser.close()


async def test_composer_uploads_a_plain_file_and_a_folder(tmp_path: Path) -> None:
    """The picker carries any file, and a folder upload reports its contents."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                created = await _create_session(page, base_url)
                session_id = str(created["id"])
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate(
                    "sid => localStorage.setItem('bianbu_session_id', sid)",
                    session_id,
                )
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_function(
                    "sid => sessionId === sid", arg=session_id
                )

                await page.locator("#attach-btn").click()
                await expect(page.locator("#attach-menu")).to_be_visible()
                await page.set_input_files(
                    "#composer-file-input",
                    {
                        "name": "report.txt",
                        "mimeType": "text/plain",
                        "buffer": b"hello attachment",
                    },
                )
                await expect(
                    page.locator("#attachment-preview .att-chip")
                ).to_have_count(1)
                await expect(page.locator("#composer-notice")).to_be_hidden()

                folder = await page.evaluate(
                    """() => {
                        const transfer = new DataTransfer();
                        const names = ['notes/one.txt', 'notes/deep/two.txt'];
                        for (const name of names) {
                            transfer.items.add(new File(['x'], name));
                        }
                        const input = document.getElementById('composer-folder-input');
                        Object.defineProperty(input, 'files', {value: transfer.files});
                        input.dispatchEvent(new Event('change'));
                        return names;
                    }"""
                )
                assert folder == ["notes/one.txt", "notes/deep/two.txt"]
                await expect(
                    page.locator("#attachment-preview .att-chip", has_text="notes")
                ).to_have_count(1)
            finally:
                await browser.close()


async def test_new_chat_controls_belong_to_history_panel(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")

                await expect(page.locator("#new-chat-btn")).to_have_count(0)
                await expect(page.locator("#clear-context-btn")).to_have_count(0)
                # Attaching an image stays in the composer: it acts on the
                # message being written, not on the conversation list.
                await expect(page.locator("#attach-btn")).to_have_count(1)

                await page.wait_for_function("() => agentsLoaded")
                await page.evaluate("() => openSessionPanel()")
                history_new_chat = page.locator("#history-new-chat-btn")
                await expect(history_new_chat).to_be_visible()
                await page.evaluate("() => closeChatAgentMenu()")
                await history_new_chat.click()
                chooser = page.locator("#session-panel #chat-agent-menu")
                # New chat reuses the current Expert silently: it must never
                # bounce the user back into the Expert picker.
                await expect(chooser).to_be_hidden()
                assert await page.evaluate("() => newChatDraft?.agent?.id") == "default"
                await expect(page.locator("#chat-input")).to_be_enabled()
                assert page_errors == []
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_new_chat_never_opens_expert_picker_when_preferred_expert_is_unavailable(
    tmp_path: Path,
) -> None:
    """The intermittent picker came from a transiently unavailable built-in Expert."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()

                async def agents(route) -> None:
                    response = await route.fetch()
                    data = await response.json()
                    original = data["data"][0]
                    data["data"] = [
                        {
                            **original,
                            "id": "com.bianbu.aaaaaaaaaaaa",
                            "name": "BibitEdgeAssistant",
                            "origin": "system",
                            "available": False,
                            "unavailable_reason": "worker_starting",
                        },
                        original,
                    ]
                    await route.fulfill(response=response, json=data)

                await page.route("**/api/v1/agents", agents)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => agentsLoaded")
                await page.evaluate("() => openSessionPanel()")
                await page.locator("#history-new-chat-btn").click()

                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                assert await page.evaluate("() => newChatDraft?.agent?.id") == "default"
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_memory_management_page_is_not_rendered(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")

                await expect(page.locator('[data-view="memory"]')).to_have_count(0)
                await expect(page.locator("#view-memory")).to_have_count(0)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_config_page_removed_but_header_language_still_works(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await expect(page.locator('[data-view="config"]')).to_have_count(0)
                await expect(page.locator('#view-config')).to_have_count(0)
                await page.locator('#lang-toggle').click()
                await page.locator('.lang-menu-item[data-lang="en"]').click()
                await expect(page.locator('#lang-label')).to_have_text('EN')
                await page.reload(wait_until="domcontentloaded")
                await expect(page.locator('#lang-label')).to_have_text('EN')
                assert not errors
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_mobile_history_opens_on_first_tap_after_reload(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(
                    viewport={"width": 375, "height": 760}
                )
                created = await _create_session(page, base_url)
                await page.add_init_script(
                    "localStorage.setItem('bianbu_session_panel_open', '1');"
                    "localStorage.setItem('bianbu_session_id', "
                    f"{json.dumps(str(created['id']))});"
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                panel = page.locator("#session-panel")
                await expect(panel).to_have_class(re.compile(r"\bhidden\b"))

                await page.locator("#session-toggle-btn").click()

                await expect(panel).not_to_have_class(re.compile(r"\bhidden\b"))
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_mobile_expert_chooser_closes_history_panel(
    tmp_path: Path,
) -> None:
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (writer,)) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(
                    viewport={"width": 375, "height": 760}
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _open_agent_chooser(page)
                chooser_bounds = await page.locator(
                    "#chat-agent-menu"
                ).bounding_box()
                assert chooser_bounds is not None
                assert chooser_bounds["x"] >= 0
                assert chooser_bounds["x"] + chooser_bounds["width"] <= 375

                await page.locator(
                    ".chat-agent-option", has_text="Writer"
                ).click()

                await expect(page.locator("#session-panel")).to_have_class(
                    re.compile(r"\bhidden\b")
                )
                await expect(page.locator("#chat-input")).to_be_focused()
                assert await page.evaluate("() => newChatDraft?.agent?.id") == "writer"
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_new_chat_agent_selection_enters_unsaved_draft(
    tmp_path: Path,
) -> None:
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (writer,)) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                seeded = await _create_session(
                    page,
                    base_url,
                    agent_id="writer",
                    title="Writing plan",
                )
                writer_session_id = str(seeded["id"])
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate(
                    "sid => localStorage.setItem('bianbu_session_id', sid)",
                    writer_session_id,
                )
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_function("() => activeAgent?.id === 'writer'")
                await expect(page.locator("#active-agent-name")).to_have_text(
                    "Writer"
                )
                before = await _list_sessions(page, base_url)
                stored = await page.evaluate(
                    "localStorage.getItem('bianbu_session_id')"
                )
                await page.evaluate("openSessionPanel()")
                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                await page.locator(".session-new-chat-control .session-panel-new").click()

                await expect(page.locator("#chat-agent-menu")).to_be_hidden()
                after = await _list_sessions(page, base_url)
                assert after == before
                assert await page.evaluate("sessionId") is None
                assert await page.evaluate("isNewChatDraft()") is True
                assert await page.evaluate(
                    "localStorage.getItem('bianbu_session_id')"
                ) == stored
                await expect(page.locator("#session-panel")).not_to_have_class(
                    re.compile(r"\bhidden\b")
                )
                await expect(page.locator("#welcome-screen")).to_be_visible()
                await expect(page.locator("#chat-input")).to_be_focused()
                await page.wait_for_function("() => activeAgent?.id === 'writer'")
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_first_draft_message_persists_selected_agent_once(
    tmp_path: Path,
) -> None:
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (writer,)) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                prompt_requests: list[dict[str, object]] = []
                title_commands: list[dict[str, object]] = []

                def record_message_request(request) -> None:
                    path = urlparse(request.url).path
                    payload = request.post_data_json or {}
                    if path.endswith("/generate-title") and request.method == "POST":
                        title_commands.append(payload)
                    elif path.endswith("/prompt") and request.method == "POST":
                        prompt_requests.append(payload)

                page.on("request", record_message_request)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                before = await _list_sessions(page, base_url)

                await page.locator('[data-view="agents"]').click()
                await page.locator(".agent-card").filter(has_text="Writer").get_by_role(
                    "button", name="使用并新建会话"
                ).click()
                await page.locator("#chat-input").fill("first lazy message")
                await page.locator("#send-btn").click()
                await page.wait_for_function(
                    "() => !isNewChatDraft() && !!sessionId"
                )

                created_id = await page.evaluate("sessionId")
                records = await _list_sessions(page, base_url)
                created = [
                    item for item in records
                    if item["id"] == created_id
                ]
                assert len(records) == len(before) + 1
                assert len(created) == 1
                assert created[0]["agent_id"] == "writer"
                assert await page.evaluate(
                    "localStorage.getItem('bianbu_session_id')"
                ) == created_id
                assert len(prompt_requests) == 1
                assert "mode" not in prompt_requests[0]
                assert prompt_requests[0]["content"] == "first lazy message"
                assert prompt_requests[0]["message_id"]
                await page.wait_for_function(
                    "() => document.querySelectorAll('.bot-message-group').length > 0"
                )
                roles = await page.locator("#chat-messages").evaluate(
                    """host => Array.from(host.children)
                        .filter(element => element.matches(
                            '.user-message-group, .bot-message-group'
                        ))
                        .map(element => element.classList.contains(
                            'user-message-group'
                        ) ? 'user' : 'assistant')"""
                )
                assert roles[:2] == ["user", "assistant"]
                for _ in range(200):
                    if title_commands:
                        break
                    await asyncio.sleep(0.01)
                assert len(title_commands) == 1
                assert title_commands[0]["content"] == "first lazy message"
                assert title_commands[0]["message_id"]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_console_title_uses_one_server_owned_command(tmp_path: Path) -> None:
    """The browser must not own title Run polling, cancellation, or recovery."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                session = await _create_session(page, base_url)
                owner = str(session["id"])
                commands: list[dict[str, object]] = []

                async def accept_title(route) -> None:
                    commands.append(route.request.post_data_json)
                    await route.fulfill(
                        status=202,
                        content_type="application/json",
                        body=json.dumps({
                            "data": {
                                "title": "hello K3",
                                "run_id": "title-run",
                                "status": "running",
                            }
                        }),
                    )

                await page.route(
                    f"**/api/v1/sessions/{owner}/generate-title",
                    accept_title,
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")

                await page.evaluate(
                    "([sid, content]) => generateSessionTitle(sid, content)",
                    [owner, "hello K3"],
                )

                assert len(commands) == 1
                assert commands[0]["content"] == "hello K3"
                assert commands[0]["message_id"]
                assert await page.evaluate(
                    "Object.keys(localStorage).filter(key => key.startsWith('bianbu_title_run:'))"
                ) == []
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_first_draft_input_survives_history_render_before_prompt_response(
    tmp_path: Path,
) -> None:
    """The new-Session history race must not erase the optimistic input."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            release_prompt = asyncio.Event()
            prompt_seen = asyncio.Event()

            async def hold_prompt(route) -> None:
                prompt_seen.set()
                await release_prompt.wait()
                await route.fulfill(
                    status=202,
                    content_type="application/json",
                    body=json.dumps({
                        "status": "success",
                        "data": {"run_id": "race-run"},
                    }),
                )

            try:
                page = await browser.new_page()
                await page.route("**/api/v1/sessions/*/prompt", hold_prompt)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _select_default_agent(page)
                await page.locator("#chat-input").fill("first race input")
                await page.locator("#send-btn").click()
                await asyncio.wait_for(prompt_seen.wait(), timeout=5)

                result = await page.evaluate(
                    """() => {
                        const model = sessionRuntime.get(sessionId);
                        renderSessionModelSnapshot(sessionId);
                        return {
                            snapshotRoles: model.snapshot().messages.map(
                                message => message.role
                            ),
                            domRoles: Array.from(messagesDiv.children)
                                .filter(element => element.matches(
                                    '.user-message-group, .bot-message-group'
                                ))
                                .map(element => element.classList.contains(
                                    'user-message-group'
                                ) ? 'user' : 'assistant'),
                        };
                    }"""
                )
                assert result == {
                    "snapshotRoles": ["user"],
                    "domRoles": ["user"],
                }
            finally:
                release_prompt.set()
                await browser.close()


@pytest.mark.asyncio
async def test_first_draft_orders_real_mux_events_during_delayed_history(
    tmp_path: Path,
) -> None:
    """Real prompt and mux events stay ordered while the first history GET waits."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            history_started = asyncio.Event()
            release_history = asyncio.Event()

            async def hold_history(route) -> None:
                history_started.set()
                await release_history.wait()
                await route.continue_()

            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _select_default_agent(page)
                await page.route("**/api/v1/sessions/*/history*", hold_history)
                await page.locator("#chat-input").fill("real first race")
                await page.locator("#send-btn").click()
                await asyncio.wait_for(history_started.wait(), timeout=5)
                await page.wait_for_function(
                    """() => sessionRuntime.get(sessionId).liveBuffer.size > 0"""
                )
                release_history.set()
                await page.wait_for_function(
                    "() => document.querySelectorAll('.bot-message-group').length > 0"
                )
                roles = await page.locator("#chat-messages").evaluate(
                    """host => Array.from(host.children)
                        .filter(element => element.matches(
                            '.user-message-group, .bot-message-group'
                        ))
                        .map(element => element.classList.contains(
                            'user-message-group'
                        ) ? 'user' : 'assistant')"""
                )
                assert roles[:2] == ["user", "assistant"]
            finally:
                release_history.set()
                await browser.close()


@pytest.mark.asyncio
async def test_first_draft_keeps_authoritative_input_when_prompt_response_fails(
    tmp_path: Path,
) -> None:
    """A mux input event wins over a later failed HTTP response."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                accept_outcomes: list[dict[str, object]] = []
                input_accepted = asyncio.Event()
                release_history = asyncio.Event()

                async def hold_history(route) -> None:
                    await release_history.wait()
                    await route.continue_()

                async def accept_then_fail(route) -> None:
                    payload = route.request.post_data_json or {}
                    sid = urlparse(route.request.url).path.split("/")[-2]
                    accept_outcomes.append(await page.evaluate(
                        """([owner, message]) => {
                            const model = sessionRuntime.get(owner);
                            model.abortHistory(model.historyGeneration);
                            const outcome = model.acceptLive({
                                seq: 0,
                                type: 'agent/inbox/spliced',
                                run_id: null,
                                time: 1,
                                data: {message: {
                                    message_id: message.message_id,
                                    content: message.content,
                                    created_at: 1,
                                    attachments: message.attachments || [],
                                }},
                            });
                            return {
                                outcome,
                                staged: model.stagedInputs.has(message.message_id),
                                selectedSkills: selectedSkills.size,
                            };
                        }""",
                        [sid, payload],
                    ))
                    input_accepted.set()
                    await route.fulfill(
                        status=500,
                        content_type="application/json",
                        body=json.dumps({"status": "error", "message": "lost response"}),
                    )

                await page.route("**/api/v1/sessions/*/prompt", accept_then_fail)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _select_default_agent(page)
                await page.evaluate(
                    """() => {
                        selectedSkills.set('global.skills/review', {
                            id: 'global.skills/review',
                            display_name: 'Global Review',
                        });
                        renderSelectedSkills();
                    }"""
                )
                await expect(
                    page.locator("#selected-skills .selected-skill-chip")
                ).to_have_count(1)
                await page.route("**/api/v1/sessions/*/history*", hold_history)
                await page.locator("#chat-input").fill("accepted once")
                await page.locator("#send-btn").click()
                await asyncio.wait_for(input_accepted.wait(), timeout=5)
                await page.wait_for_function(
                    "() => !isNewChatDraft() || chatInput.value === 'accepted once'"
                )
                await page.wait_for_function("() => composerSubmissionPromise === null")

                result = await page.evaluate(
                    """() => ({
                        draft: isNewChatDraft(),
                        input: chatInput.value,
                        messages: sessionRuntime.get(sessionId).snapshot().messages,
                    })"""
                )
                assert accept_outcomes == [{
                    "outcome": "applied",
                    "staged": False,
                    "selectedSkills": 1,
                }]
                assert result["draft"] is False
                assert result["input"] == ""
                assert await page.evaluate("selectedSkills.size") == 0
                assert [message["role"] for message in result["messages"]] == ["user"]
                assert result["messages"][0]["content"] == "accepted once"
            finally:
                release_history.set()
                await browser.close()


@pytest.mark.asyncio
async def test_attachment_only_message_uses_a_nonempty_run_prompt(
    tmp_path: Path,
) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                prompt_requests: list[dict[str, object]] = []

                def record_run(request) -> None:
                    if urlparse(request.url).path.endswith("/prompt") and request.method == "POST":
                        payload = request.post_data_json or {}
                        prompt_requests.append(payload)

                page.on("request", record_run)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _select_default_agent(page)
                await page.evaluate(
                    """() => {
                        pendingAttachments = [{
                            file_path: 'artifact.txt',
                            file_name: 'artifact.txt',
                            file_type: 'file',
                        }];
                        renderAttachmentPreview();
                    }"""
                )
                await page.locator("#send-btn").click()
                await page.wait_for_function(
                    "() => document.querySelectorAll('.bot-message-group').length > 0"
                )

                assert len(prompt_requests) == 1
                assert str(prompt_requests[0]["content"]).strip()
                assert "mode" not in prompt_requests[0]
                assert prompt_requests[0]["attachments"] == [
                    {
                        "file_path": "artifact.txt",
                        "file_name": "artifact.txt",
                        "file_type": "file",
                    }
                ]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_session_prompt_cannot_override_authoritative_session_agent(
    tmp_path: Path,
) -> None:
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (writer,)) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                response = await _create_session(page, base_url)
                bound_session_id = str(response["id"])
                await page.add_init_script(
                    "localStorage.setItem('bianbu_session_id', "
                    f"{json.dumps(bound_session_id)})"
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => activeAgent?.id === 'default'")

                prompt_requests: list[dict[str, object]] = []
                page.on(
                    "request",
                    lambda request: prompt_requests.append(request.post_data_json)
                    if urlparse(request.url).path.endswith("/prompt")
                    and request.method == "POST"
                    else None,
                )
                await page.evaluate(
                    "activeAgent = agentForId('writer'); renderActiveAgent()"
                )
                await page.locator("#chat-input").fill("bound agent input")
                await page.locator("#send-btn").click()

                await expect(
                    page.locator(".user-message-group", has_text="bound agent input")
                ).to_have_count(1)
                assert len(prompt_requests) == 1
                assert "agent_id" not in prompt_requests[0]
                authoritative = await page.request.get(
                    f"{base_url}/api/v1/sessions/{bound_session_id}"
                )
                assert (await authoritative.json())["data"]["agent_id"] == "default"
            finally:
                await browser.close()

@pytest.mark.asyncio
async def test_run_rejection_restores_existing_draft_and_attachments(
    tmp_path: Path,
) -> None:
    """An explicit 409 never loses the user's existing Session draft."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                session = await _create_session(page, base_url)
                owner = str(session["id"])
                await page.add_init_script(
                    f"localStorage.setItem('bianbu_session_id', {json.dumps(owner)})"
                )
                await page.route(
                    "**/api/v1/sessions/*/prompt",
                    lambda route: route.fulfill(
                        status=409,
                        content_type="application/json",
                        body='{"error":{"code":"run_conflict","message":"busy","details":{}}}',
                    ),
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate(
                    """() => {
                        pendingAttachments = [{
                            file_path: 'artifact.txt', file_name: 'artifact.txt', file_type: 'file',
                        }];
                        renderAttachmentPreview();
                    }"""
                )
                await page.locator("#chat-input").fill("retry unchanged")
                await page.locator("#send-btn").click()
                await expect(page.locator("#chat-input")).to_have_value("retry unchanged")
                assert await page.evaluate("pendingAttachments.length") == 1
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_lost_run_response_uses_session_event_without_duplicate_history(
    tmp_path: Path,
) -> None:
    """A committed Run remains a single turn when its HTTP response is lost."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                attempts: list[dict[str, object]] = []
                owner = {"value": ""}

                async def lose_first_response(route, request) -> None:
                    payload = request.post_data_json or {}
                    owner["value"] = urlparse(request.url).path.split("/")[-2]
                    attempts.append(payload)
                    if len(attempts) == 1:
                        persisted = await page.request.post(
                            request.url,
                            data=payload,
                        )
                        assert persisted.status == 202
                        await route.abort("failed")
                    else:
                        await route.continue_()

                await page.route("**/api/v1/sessions/*/prompt", lose_first_response)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _select_default_agent(page)
                await page.locator("#chat-input").fill("send exactly once")
                await page.locator("#send-btn").click()
                await page.wait_for_function(
                    "() => document.querySelectorAll('.bot-message-group').length > 0"
                )

                assert 1 <= len(attempts) <= 3
                assert {item["message_id"] for item in attempts} == {
                    attempts[0]["message_id"]
                }
                events = await _session_events(page, base_url, owner["value"])
                users = [
                    item for item in events
                    if item["type"] == "user/message"
                    and not item["data"].get("console_operation")
                ]
                terminals = [
                    item for item in events if item["type"] == "run/completed"
                ]
                assert len(users) == 1
                assert users[0]["data"]["content"] == "send exactly once"
                assert len(terminals) == 1
                assert str(terminals[0]["data"]["content"]).endswith("send exactly once")
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_deleted_bound_agent_keeps_session_locked_and_disables_composer(
    tmp_path: Path,
) -> None:
    writer = AgentSpec(id="writer", name="Writer")
    async with _running_console(tmp_path, (writer,)) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                created = await _create_session(
                    page, base_url, agent_id="writer"
                )
                orphaned_session_id = str(created["id"])
                submitted = await _create_run(
                    page,
                    base_url,
                    session_id=orphaned_session_id,
                    agent_id="writer",
                    prompt="locked history",
                )
                await _wait_for_run(page, base_url, str(submitted["id"]))
                await page.add_init_script(
                    "localStorage.setItem('bianbu_session_id', "
                    f"{json.dumps(orphaned_session_id)})"
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function("() => activeAgent?.id === 'writer'")

                deleted = await page.request.delete(
                    f"{base_url}/api/v1/agents/writer"
                )
                assert deleted.ok
                await page.reload(wait_until="domcontentloaded")

                await page.wait_for_function("() => activeAgent?.available === false")
                await expect(page.locator("#chat-input")).to_be_disabled()
                await expect(page.locator(".edit-msg-btn")).to_have_count(0)
                await expect(page.locator(".regenerate-msg-btn")).to_have_count(0)
                await _open_agent_chooser(page)
                await page.locator(
                    ".chat-agent-option", has_text="Default"
                ).click()
                await page.wait_for_function("() => activeAgent?.id === 'default'")
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_endpoint_unavailable_agent_cannot_be_selected_or_used(
    tmp_path: Path,
) -> None:
    """A present Agent with a missing Provider must fail closed across all controls."""

    unavailable = AgentSpec(
        id="unavailable",
        name="Unavailable",
        model=AgentModel("missing-provider", "missing-model"),
    )
    async with _running_console(tmp_path, (unavailable,)) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                session = await _create_session(
                    page, base_url, agent_id="unavailable"
                )
                await page.add_init_script(
                    "localStorage.setItem('bianbu_session_id', "
                    f"{json.dumps(session['id'])})"
                )
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.wait_for_function(
                    "() => activeAgent?.id === 'unavailable' && activeAgent?.available === false"
                )
                await expect(page.locator("#chat-input")).to_be_disabled()
                await expect(page.locator("#send-btn")).to_be_disabled()

                await page.locator('[data-view="agents"]').click()
                card = page.locator(".agent-card", has_text="Unavailable")
                await expect(card.locator(".agent-card-use")).to_be_disabled()
                await _open_agent_chooser(page)
                option = page.locator(".chat-agent-option", has_text="Unavailable")
                await expect(option).to_be_disabled()
            finally:
                await browser.close()


def _agent_catalog_plugin(root: Path) -> Path:
    source = root / "vendor.bundle"
    source.mkdir(parents=True)
    (source / "plugin.yaml").write_text(
        "schema_version: 1\n"
        "name: vendor.bundle\n"
        "version: 1.0.0\n"
        "description: Agent catalog fixture bundle.\n",
        encoding="utf-8",
    )
    tool = source / "tools" / "plugin_echo"
    tool.mkdir(parents=True)
    (tool / "tool.py").write_text(
        "from bianbu_agent.tools import tool\n"
        "def create_tool(context):\n"
        "    @tool\n"
        "    def plugin_echo() -> str:\n"
        "        '''Plugin Echo'''\n"
        "        return 'ok'\n"
        "    return plugin_echo\n",
        encoding="utf-8",
    )
    skill = source / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndisplay_name: Plugin Review\n"
        "description: Review from plugin\nversion: 1.0.0\n---\n\nReview.\n",
        encoding="utf-8",
    )
    return source


def _pending_catalog_skill(root: Path) -> Path:
    source = root / "pending"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: pending\ndisplay_name: Pending Skill\n"
        "description: Skill installed while the editor is open\nversion: 1.0.0\n"
        "---\n\nPending.\n",
        encoding="utf-8",
    )
    return source


@pytest.mark.asyncio
async def test_agent_editor_round_trips_plugin_and_hot_loaded_capabilities(
    tmp_path: Path,
) -> None:
    """Editing through the effective catalog must never drop selected Plugin IDs."""

    runtime_root = tmp_path / "runtime"
    # The Plugin is loaded with the Runtime so the Agent catalog knows its
    # members; the pending Skill arrives through the package API.
    plugin_source = _agent_catalog_plugin(tmp_path / "plugin-source")
    pending = _pending_catalog_skill(tmp_path / "pending")
    # A Control Runtime validates Agent selections against the installed
    # inventory, which is the surface a Plugin install updates.
    async with _running_console(runtime_root, role="control") as environment:
        base_url, home = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                installed = await page.request.post(
                    f"{base_url}/api/v1/plugins",
                    data={"source": str(plugin_source.resolve())},
                )
                assert installed.status == 201, await installed.text()
                pending_install = await page.request.post(
                    f"{base_url}/api/v1/packages/skill",
                    data={"source": str(pending.resolve())},
                )
                assert pending_install.status == 201, await pending_install.text()
                created = await page.request.post(
                    f"{base_url}/api/v1/agents",
                    data={
                        "name": "Plugin Agent",
                        "model": {"provider": "browser", "name": "browser-contract"},
                        # A Plugin owns its members: selecting the Plugin is
                        # what makes them assignable to the Agent.
                        "plugins": ["vendor.bundle"],
                        "tools": ["vendor.bundle/plugin_echo"],
                        "skills": ["vendor.bundle/review"],
                    },
                )
                assert created.status == 201, await created.text()
                agent_id = (await created.json())["data"]["id"]

                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="agents"]').click()
                card = page.locator(f'.agent-card[data-agent-id="{agent_id}"]')
                await expect(card).to_be_visible(timeout=30_000)
                await card.click()
                await expect(page.locator("#agent-editor-overlay")).to_be_visible()
                plugin_tool = page.locator(
                    '#agent-form-tools input[value="vendor.bundle/plugin_echo"]'
                )
                plugin_skill = page.locator(
                    '#agent-form-skills input[value="vendor.bundle/review"]'
                )
                await expect(plugin_tool).to_be_checked()
                await expect(plugin_skill).to_be_checked()
                # Plugin members are selected through their Plugin, so the
                # pending standalone Skill is what the editor still offers.
                pending_option = page.locator('#agent-form-skills input[value="pending"]')
                await expect(pending_option).to_be_enabled()
                await expect(
                    page.locator("#agent-form-skills", has_text="Restart the session")
                ).to_have_count(0)

                await page.locator("#agent-form-name").fill("Plugin Agent Edited")
                await page.locator("#agent-editor-save").click()
                await expect(page.locator("#agent-editor-overlay")).to_be_hidden()
                stored = AgentManifest.from_path(
                    home.agents_dir / agent_id / "agent.yaml"
                ).spec
                assert stored.tools == ("vendor.bundle/plugin_echo",)
                assert stored.skills == ("vendor.bundle/review",)
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_agent_model_save_ignores_pending_catalogs_and_updates_card(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as (base_url, home):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            release = asyncio.Event()
            async def pending_catalog(route):
                await release.wait()
                await route.abort()
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="agents"]').click()
                await expect(page.locator('.agent-card', has_text='Default')).to_be_visible()
                await page.route('**/api/v1/capabilities/**', pending_catalog)
                await page.route('**/api/v1/mcp-servers', pending_catalog)
                await page.locator('.agent-card', has_text='Default').click()
                await expect(page.locator('#agent-form-model-name')).to_have_value('browser-contract')
                await page.locator('#agent-form-model-name').fill('updated-model')
                await expect(page.locator("button[onclick=\"toggleAllAgentCapabilities('tools')\"]")).to_be_disabled()
                await page.route('**/api/v1/agents', pending_catalog)
                async with page.expect_request(lambda request: request.method == 'PATCH' and '/agents/default' in request.url) as saving:
                    await page.locator('#agent-editor-save').click()
                assert (await saving.value).post_data_json == {'model': {'provider': 'browser', 'name': 'updated-model'}}
                await expect(page.locator('#agent-editor-overlay')).to_be_hidden()
                await expect(page.locator('.agent-card', has_text='Default')).to_contain_text('updated-model')
                saved = AgentManifest.from_path(home.agents_dir / 'default' / 'agent.yaml').spec
                assert saved.permissions == {}
            finally:
                release.set()
                await browser.close()


@pytest.mark.asyncio
async def test_agent_editor_preserves_selection_order_and_guards_slow_operations(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f'{base_url}/chat', wait_until='domcontentloaded')
                await page.locator('[data-view="agents"]').click()
                await expect(page.locator('.agent-card', has_text='Default')).to_be_visible()
                original = await page.evaluate("({...agentsState.find(item => item.id === 'default'), tools:['z', 'a']})")
                patches = asyncio.Queue()
                lists = asyncio.Queue()
                async def patch_agent(route):
                    response = asyncio.get_running_loop().create_future()
                    await patches.put((route.request.post_data_json, response))
                    await route.fulfill(json={'data': await response})
                async def list_agents(route):
                    response = asyncio.get_running_loop().create_future()
                    await lists.put(response)
                    await route.fulfill(json={'data': await response})
                await page.route('**/api/v1/agents/default', patch_agent)
                await page.route('**/api/v1/agents', list_agents)
                await page.evaluate('agent => openAgentEditor(agent)', original)
                await page.wait_for_function("() => document.getElementById('agent-form-tools').dataset.pending === 'false'")
                await page.evaluate("() => renderAgentCapabilityOptions('tools', [{name:'a'}, {name:'z'}], ['z', 'a'])")
                await page.locator('#agent-form-model-name').fill('new-model')
                await page.locator('#agent-editor-save').click()
                payload, finish_patch = await asyncio.wait_for(patches.get(), 5)
                assert payload == {'model': {'provider': 'browser', 'name': 'new-model'}}
                await page.evaluate("() => { closeAgentEditor(); openAgentEditor({id:'other', name:'Other'}); }")
                assert await page.evaluate('editingAgentId') == 'default'
                first = {**original, **payload}
                finish_patch.set_result(first)
                await expect(page.locator('#agent-editor-overlay')).to_be_hidden()
                old_list = await asyncio.wait_for(lists.get(), 5)
                await page.evaluate('agent => openAgentEditor(agent)', first)
                await page.wait_for_function("() => document.getElementById('agent-form-tools').dataset.pending === 'false'")
                await page.locator('#agent-form-model-name').fill('newest-model')
                await page.locator('#agent-editor-save').click()
                payload, finish_patch = await asyncio.wait_for(patches.get(), 5)
                newest = {**first, **payload}
                finish_patch.set_result(newest)
                await expect(page.locator('#agent-editor-overlay')).to_be_hidden()
                latest_list = await asyncio.wait_for(lists.get(), 5)
                async with page.expect_response('**/api/v1/agents'):
                    latest_list.set_result([newest])
                async with page.expect_response('**/api/v1/agents'):
                    old_list.set_result([first])
                await page.wait_for_timeout(50)
                assert await page.evaluate("agentsState.find(item => item.id === 'default').model.name") == 'newest-model'
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_agent_provider_loading_does_not_steal_model_input_focus(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            release = asyncio.Event()
            try:
                page = await browser.new_page()
                await page.goto(f'{base_url}/chat', wait_until='domcontentloaded')
                await page.locator('[data-view="agents"]').click()
                await expect(page.locator('.agent-card', has_text='Default')).to_be_visible()
                async def delayed_providers(route):
                    await release.wait()
                    await route.continue_()
                await page.route('**/api/v1/providers', delayed_providers)
                await page.locator('.agent-card', has_text='Default').click()
                await page.locator('#agent-form-model-name').focus()
                release.set()
                await expect(page.locator('#agent-editor-save')).to_be_enabled()
                await expect(page.locator('#agent-form-model-name')).to_be_focused()
                await page.keyboard.press('ControlOrMeta+A')
                await page.keyboard.type('typed-model')
                await expect(page.locator('#agent-form-model-name')).to_have_value('typed-model')
                await expect(page.locator('#agent-form-name')).to_have_value('Default')
            finally:
                release.set()
                await browser.close()


@pytest.mark.asyncio
async def test_skill_picker_ranks_names_before_description_matches(tmp_path: Path) -> None:
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                skills = [
                    {"id": "a-guide", "description": "Use for reviewing code"},
                    {"id": "receiving-code-review"},
                    {"id": "vibe-coder"},
                    {"id": "plugin/vi", "display_name": "VI"},
                    {"id": "vivid"},
                ]
                await page.evaluate("skills => { availableSkills = skills; }", skills)
                await page.evaluate("() => { skillPickerSearch.value = ' VI '; renderSkillPickerItems(); }")
                assert await page.locator(".skill-picker-item-name").all_text_contents() == [
                    "VI", "vibe-coder", "vivid", "receiving-code-review", "a-guide",
                ]
                await page.evaluate("() => { skillPickerSearch.value = ''; renderSkillPickerItems(); }")
                assert await page.locator(".skill-picker-item-name").all_text_contents() == [
                    "a-guide", "receiving-code-review", "vibe-coder", "VI", "vivid",
                ]
                assert await page.locator(".skill-picker-item-description").count() == 0
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_skill_names_follow_the_console_language(tmp_path: Path) -> None:
    """Power-format v2 Skills show a name per language and fall back to `name`."""

    async def skill_catalog(route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "status": "success",
                "data": [
                    {
                        "id": "using-apm", "name": "using-apm",
                        "display_name": "using-apm",
                        "description": "Manage Powers with APM.",
                        "name_zh": "APM 使用手册", "name_en": "APM Agent Handbook",
                        "description_zh": "使用 APM 管理 Power 资源。",
                        "version": "v1.2.0", "origin": "standalone", "plugin_id": None,
                        "enabled": True, "assignable": True, "preset": False,
                        "unavailable_reason": "",
                    },
                    {
                        "id": "vendor.skills/plain-skill", "name": "plain-skill",
                        "display_name": "plain-skill",
                        "description": "Written before Power format v2.",
                        "version": "1.0.0", "origin": "plugin",
                        "plugin_id": "vendor.skills",
                        "enabled": True, "assignable": True, "preset": False,
                        "unavailable_reason": "",
                    },
                ],
            }),
        )

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/api/v1/capabilities/skill", skill_catalog)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")

                async def picker_names() -> list[str]:
                    await page.evaluate("() => openSkillPicker()")
                    await page.wait_for_function("() => availableSkills.length === 2")
                    await page.evaluate(
                        "() => { skillPickerSearch.value = ''; renderSkillPickerItems(); }"
                    )
                    return await page.locator(".skill-picker-item-name").all_text_contents()

                # 中文界面用 name_zh；没有 v2 名字的技能回退到 name。
                assert await picker_names() == [
                    "APM 使用手册", "vendor.skills:plain-skill",
                ]
                await page.evaluate("setLanguage('en')")
                await page.wait_for_function("() => currentLang === 'en'")
                assert await picker_names() == [
                    "APM Agent Handbook", "vendor.skills:plain-skill",
                ]
                # 技能页同样跟随语言，并且中文描述来自 description_zh。
                await page.evaluate("() => setLanguage('zh')")
                await page.locator('[data-view="skills"]').click()
                await expect(
                    page.locator('[data-skill-name="using-apm"] .skill-card-name')
                ).to_have_text("APM 使用手册")
                await expect(
                    page.locator('[data-skill-name="using-apm"] .skill-card-description')
                ).to_have_text("使用 APM 管理 Power 资源。")
            finally:
                await browser.close()


def _localized_catalog_skill(root: Path) -> Path:
    source = root / "localized"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: localized-skill\ndisplay_name: Localized Skill\n"
        "description: Manage Powers with APM.\n"
        "name_zh: APM 使用手册\nname_en: APM Agent Handbook\n"
        "description_zh: 使用 APM 管理 Power 资源。\nversion: 1.0.0\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    return source


@pytest.mark.asyncio
async def test_first_slash_opens_the_skill_menu_while_the_catalog_loads(
    tmp_path: Path,
) -> None:
    """The first `/` lists Skills even though the catalog is still in flight."""

    async def skill_catalog(route) -> None:
        # Hold the response: the menu is redrawn from the reply, and it used to
        # stay closed because hiding it was mistaken for "the user gave up".
        await asyncio.sleep(0.4)
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "status": "success",
                "data": [{
                    "id": "standalone-slash", "name": "standalone-slash",
                    "display_name": "Standalone Slash",
                    "description": "Only reachable from the slash list.",
                    "name_zh": "独立斜杠技能", "name_en": "Standalone Slash Skill",
                    "version": "1.0.0", "origin": "standalone", "plugin_id": None,
                    "enabled": True, "assignable": True, "preset": False,
                    "unavailable_reason": "",
                }],
            }),
        )

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/api/v1/capabilities/skill", skill_catalog)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _select_default_agent(page)
                assert await page.evaluate("() => availableSkills.length") == 0

                command = page.locator("#slash-menu .slash-menu-item .cmd")
                await page.locator("#chat-input").fill("/")
                await expect(page.locator("#slash-menu")).to_be_visible()
                await expect(command).to_have_text(["独立斜杠技能"])

                # The list follows the console language like the picker does.
                await page.evaluate("setLanguage('en')")
                await page.wait_for_function("() => currentLang === 'en'")
                await expect(command).to_have_text(["Standalone Slash Skill"])
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_agent_editor_capability_labels_follow_the_console_language(
    tmp_path: Path,
) -> None:
    """An open expert editor relabels its Tool and Skill panels on a switch."""

    runtime_root = tmp_path / "runtime"
    localized = _localized_catalog_skill(tmp_path / "localized-source")
    # A Control Runtime validates Agent selections against the installed
    # inventory, so the Skill has to be a real package.
    async with _running_console(runtime_root, role="control") as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                installed = await page.request.post(
                    f"{base_url}/api/v1/packages/skill",
                    data={"source": str(localized.resolve())},
                )
                assert installed.status == 201, await installed.text()
                created = await page.request.post(
                    f"{base_url}/api/v1/agents",
                    data={
                        "name": "Localized Agent",
                        "model": {"provider": "browser", "name": "browser-contract"},
                        "tools": ["terminal"],
                        "skills": ["localized-skill"],
                    },
                )
                assert created.status == 201, await created.text()
                agent_id = (await created.json())["data"]["id"]

                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.locator('[data-view="agents"]').click()
                card = page.locator(f'.agent-card[data-agent-id="{agent_id}"]')
                await expect(card).to_be_visible(timeout=30_000)
                await card.click()
                await expect(page.locator("#agent-editor-overlay")).to_be_visible()

                skill_box = page.locator(
                    '#agent-form-skills input[value="localized-skill"]'
                )
                skill_label = page.locator(
                    '#agent-form-skills input[value="localized-skill"] + span strong'
                )
                tool_box = page.locator('#agent-form-tools input[value="terminal"]')
                await expect(skill_label).to_have_text("APM 使用手册")
                await expect(tool_box).to_be_checked()
                await expect(
                    page.locator("#agent-form-tools input[value=\"terminal\"] + span strong")
                ).to_have_text("terminal")

                await page.evaluate("setLanguage('en')")
                await page.wait_for_function("() => currentLang === 'en'")
                await expect(skill_label).to_have_text("APM Agent Handbook")
                # The redraw keeps the draft selection.
                await expect(skill_box).to_be_checked()
                await expect(tool_box).to_be_checked()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_temporary_skills_hide_what_the_expert_already_runs(tmp_path: Path) -> None:
    """The Expert already runs its own Skills and every Skill of its Plugins."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                skills = [
                    {"id": "spacemit-power/markdown-maker", "plugin_id": "spacemit-power"},
                    {"id": "spacemit-power/k3-media-pipeline", "plugin_id": "spacemit-power"},
                    {"id": "bianbu.audit/audit-guide", "plugin_id": "bianbu.audit"},
                    {"id": "standalone-skill", "description": "Kept, nothing else runs it"},
                    {"id": "named-elsewhere"},
                ]
                await page.evaluate("skills => { availableSkills = skills; }", skills)
                await page.evaluate(
                    "agent => { activeAgent = agent; }",
                    {
                        "id": "com.bianbu.test",
                        "skills": ["named-elsewhere"],
                        "plugins": ["bianbu.audit"],
                    },
                )
                await page.evaluate("() => { skillPickerSearch.value = ''; renderSkillPickerItems(); }")
                assert await page.locator(".skill-picker-item-name").all_text_contents() == [
                    "spacemit-power/markdown-maker",
                    "spacemit-power/k3-media-pipeline",
                    "standalone-skill",
                ]
                # 插件成员被点名，等同于这个插件在场：名下 Skill 一起从候选里消失。
                await page.evaluate(
                    "agent => { activeAgent = agent; }",
                    {"id": "com.bianbu.test", "skills": ["spacemit-power/k3-media-pipeline"]},
                )
                await page.evaluate("() => { skillPickerSearch.value = ''; renderSkillPickerItems(); }")
                assert await page.locator(".skill-picker-item-name").all_text_contents() == [
                    "bianbu.audit/audit-guide", "standalone-skill", "named-elsewhere",
                ]
                # 斜杠菜单与面板共用同一份候选，并且都不再显示技能描述。
                # 背景刷新会重画输入区，所以渲染和读取放在同一次求值里做。
                slash_menu = await page.evaluate(
                    """() => {
                        chatInput.value = '/';
                        showSlashMenu('/');
                        return [...document.querySelectorAll('#slash-menu .slash-menu-item')]
                            .map(node => [
                                node.querySelector('.cmd').textContent,
                                node.querySelector('.desc').textContent,
                            ]);
                    }"""
                )
                assert slash_menu == [
                    ["bianbu.audit/audit-guide", ""],
                    ["standalone-skill", ""],
                    ["named-elsewhere", ""],
                ]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_skill_picker_labels_plugin_skills_with_their_plugin_prefix(tmp_path: Path) -> None:
    """`/spacemit` must find the Plugin's Skills and select their qualified id."""

    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()

                async def skill_catalog(route) -> None:
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "data": [
                                    {
                                        "id": "spacemit-power/markdown-maker",
                                        "name": "markdown-maker",
                                        "display_name": "PDF 转 Markdown",
                                        "plugin_id": "spacemit-power",
                                        "enabled": True,
                                        "assignable": True,
                                    },
                                    {
                                        "id": "receiving-code-review",
                                        "name": "receiving-code-review",
                                        "display_name": "receiving-code-review",
                                        "enabled": True,
                                        "assignable": True,
                                    },
                                ]
                            }
                        ),
                    )

                await page.route("**/api/v1/capabilities/skill", skill_catalog)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await _select_default_agent(page)
                await page.locator("#chat-input").fill("/skills")
                await page.locator("#chat-input").press("Enter")
                await expect(page.locator("#skill-picker")).to_be_visible()

                await page.locator("#skill-picker-search").fill("spacemit")
                assert await page.locator(".skill-picker-item-name").all_text_contents() == [
                    "spacemit-power:PDF 转 Markdown",
                ]
                # The picker submits the plugin-qualified handle the Run resolves,
                # while the label keeps the Plugin visible.
                await page.locator("#skill-picker .skill-picker-item").click()
                assert await page.evaluate("() => selectedSkillIds()") == [
                    "spacemit-power/markdown-maker",
                ]
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_console_has_no_retired_knowledge_view_or_memory_console_requests(tmp_path):
    async with _running_console(tmp_path) as environment:
        base_url, _ = environment
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                requested = []
                page.on('request', lambda request: requested.append(request.url))
                await page.goto(f'{base_url}/chat', wait_until='networkidle')
                await expect(page.locator('#view-chat')).to_be_visible()
                assert await page.locator('#view-knowledge, #knowledge-dialog-overlay').count() == 0
                assert await page.evaluate("typeof loadKnowledgeView") == 'undefined'
                assert not any('bianbu.memory' in url and ('console' in url or 'describe' in url) for url in requested)
                await page.evaluate("navigateTo('knowledge')")
                await expect(page.locator('#view-chat')).to_be_visible()
            finally:
                await browser.close()
