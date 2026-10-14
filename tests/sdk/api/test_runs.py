"""Shared API fixtures and Session-owned execution integration tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import Field

from bianbu_agent import (
    AgentSpec,
    BianbuAgentRuntime,
    InMemoryAgentRegistry,
    RunEvent,
    RuntimeConfig,
)
from bianbu_agent.server.app import create_app
from bianbu_agent.sessions import SessionInboxMessage
from bianbu_agent.sessions.input import SessionInputBroker



@dataclass
class _Model:
    model: str


async def _park_approval(control, payload: dict[str, object]):
    """Ask the live Run for a decision, like the real approval middleware.

    The decision future is resolved by the Run that consumes the stream, so
    the request has to be surfaced before it can be awaited.
    """

    task = asyncio.ensure_future(control.request_approval(payload))
    surfaced = await control.wait_approval_request()
    return task, surfaced


class _RunEngine:
    async def stream(self, prompt: object, *, control, **_):
        request = (
            str(prompt[-1].content)
            if isinstance(prompt, (list, tuple)) and prompt
            else None if prompt is None else str(prompt).rsplit("\n", 1)[-1]
        )
        if request == "interactive":
            yield RunEvent("token", {"text": "first"})
            task, approval = await _park_approval(
                control,
                {
                    "approval_id": "approval-1",
                    "actions": [{"name": "write_file", "arguments": {"path": "x"}}],
                },
            )
            yield RunEvent("approval_required", dict(approval))
            await task
            yield RunEvent("token", {"text": " second"})
            yield RunEvent("done", {})
            return
        if request == "cancel":
            task, approval = await _park_approval(
                control,
                {"approval_id": "cancel-approval", "actions": []},
            )
            yield RunEvent("approval_required", dict(approval))
            await task
            return
        if request == "step-wait":
            yield RunEvent("token", {"text": "started"})
            for _attempt in range(200):
                if control.cancelled.is_set() or len(await control.load_step_input()) >= 2:
                    break
                await asyncio.sleep(0.005)
            batch = await control.capture_step_input()
            for item in batch:
                yield RunEvent("input_applied", {"message_id": item.message_id})
            if batch:
                yield RunEvent("token", {"text": "|" + "|".join(item.content for item in batch)})
            yield RunEvent("done", {})
            return
        if request == "terminal-race":
            await asyncio.sleep(0.05)
            yield RunEvent("token", {"text": "original completed"})
            yield RunEvent("done", {})
            return
        yield RunEvent("token", {"text": str(request)})
        yield RunEvent("done", {})


async def _runtime(root: Path, *, mcps: tuple[str, ...] = ()) -> BianbuAgentRuntime:
    agents = (
        AgentSpec(id="default", name="Default", mcps=mcps, plugins=("*",)),
        AgentSpec(id="writer", name="Writer"),
    )
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=root / ".bianbu-agent"),
        default_model=_Model("default"),
        agent_id="default",
        agent_registry=InMemoryAgentRegistry(agents),
        engine_builder=lambda *args: _RunEngine(),
    )
    await runtime.start()
    return runtime


@asynccontextmanager
async def _api(root: Path, *, mcps: tuple[str, ...] = ()):
    runtime = await _runtime(root, mcps=mcps)
    transport = httpx.ASGITransport(app=create_app(runtime, manage_lifespan=False))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, runtime
    finally:
        await runtime.close()


class _RecordingChatModel(FakeMessagesListChatModel):
    seen_messages: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def _generate(self, messages, *args, **kwargs):
        self.seen_messages.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


@asynccontextmanager
async def _graph_api(root: Path, *, plugins=()):
    agents = (
        AgentSpec(id="default", name="Default", plugins=plugins),
    )
    model = _RecordingChatModel(
        responses=[AIMessage(content="followup saw direct history")]
    )
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=root / ".bianbu-agent"),
        default_model=model,
        agent_id="default",
        agent_registry=InMemoryAgentRegistry(agents),
    )
    await runtime.start()
    transport = httpx.ASGITransport(app=create_app(runtime, manage_lifespan=False))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, runtime, model
    finally:
        await runtime.close()


async def _wait_terminal(runtime: BianbuAgentRuntime, run_id: str):
    for _ in range(200):
        record = await runtime.runs.get_record(run_id)
        if record is not None and record.status in {"completed", "failed", "cancelled"}:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"internal execution did not finish: {run_id}")


@pytest.mark.asyncio
async def test_public_api_is_session_owned_and_has_no_run_resources(tmp_path: Path) -> None:
    async with _api(tmp_path) as (client, runtime):
        await runtime.sessions.get_or_create(session_id="session-only", agent_id="default")
        response = await client.post(
            "/api/v1/sessions/session-only/prompt",
            json={"message_id": "message-1", "content": "hello"},
        )
        assert response.status_code == 202
        await _wait_terminal(runtime, response.json()["data"]["run_id"])
        for path in (
            "/api/v1/runs",
            "/api/v1/runs/internal",
            "/api/v1/runs/internal/cancel",
            "/api/v1/runs/internal/approvals/approval-1",
        ):
            assert (await client.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_two_steers_before_one_boundary_are_one_fifo_batch(tmp_path: Path) -> None:
    async with _api(tmp_path) as (client, runtime):
        await runtime.sessions.get_or_create(session_id="batch", agent_id="default")
        started = await client.post(
            "/api/v1/sessions/batch/prompt",
            json={"message_id": "start", "content": "step-wait"},
        )
        run_id = started.json()["data"]["run_id"]
        await asyncio.gather(
            client.post(
                "/api/v1/sessions/batch/prompt",
                json={"message_id": "steer-1", "content": "first"},
            ),
            client.post(
                "/api/v1/sessions/batch/prompt",
                json={"message_id": "steer-2", "content": "second"},
            ),
        )
        record = await _wait_terminal(runtime, run_id)
        assert record.result is not None
        assert record.result.content == "started|first|second"
        assert [
            event.payload["message_id"]
            for event in record.events
            if event.kind == "input_applied"
        ] == ["steer-1", "steer-2"]


@pytest.mark.asyncio
async def test_selected_skill_input_waits_for_a_fresh_run_boundary(tmp_path: Path) -> None:
    """A Skill override must not enter an engine built before that selection."""

    async with _api(tmp_path) as (client, runtime):
        await runtime.sessions.get_or_create(session_id="skill-boundary", agent_id="default")
        started = await client.post(
            "/api/v1/sessions/skill-boundary/prompt",
            json={"message_id": "start", "content": "step-wait"},
        )
        active_run_id = started.json()["data"]["run_id"]
        selected = await client.post(
            "/api/v1/sessions/skill-boundary/prompt",
            json={
                "message_id": "selected-skill",
                "content": "review next",
                "skill_ids": ["global/review"],
            },
        )

        assert selected.status_code == 202
        active = await _wait_terminal(runtime, active_run_id)
        assert not any(
            event.kind == "input_applied"
            and event.payload.get("message_id") == "selected-skill"
            for event in active.events
        )
        for _ in range(200):
            runs = await runtime.runs.list(session_id="skill-boundary")
            if len(runs) == 2 and runs[-1].status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("selected Skill input did not start a fresh Run")
        assert runs[-1].request["skill_ids"] == ["global/review"]


@pytest.mark.asyncio
async def test_queued_skill_inputs_each_get_their_own_run_scope(tmp_path: Path) -> None:
    """A temporary Skill must not leak into a later queued message."""

    async with _api(tmp_path) as (client, runtime):
        session_id = "queued-skill-scopes"
        await runtime.sessions.get_or_create(session_id=session_id, agent_id="default")
        started = await client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"message_id": "start", "content": "step-wait"},
        )
        active_run_id = started.json()["data"]["run_id"]
        for payload in (
            {
                "message_id": "selected-a",
                "content": "use review",
                "skill_ids": ["global/review"],
            },
            {"message_id": "ordinary", "content": "plain follow-up"},
            {
                "message_id": "selected-b",
                "content": "use security",
                "skill_ids": ["global/security"],
            },
        ):
            response = await client.post(
                f"/api/v1/sessions/{session_id}/prompt",
                json=payload,
            )
            assert response.status_code == 202

        await _wait_terminal(runtime, active_run_id)
        for _ in range(400):
            runs = await runtime.runs.list(session_id=session_id)
            if len(runs) == 4 and all(
                run.status in {"completed", "failed", "cancelled"}
                for run in runs
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("queued Skill scopes did not drain into separate Runs")

        queued = runs[1:]
        assert [
            [message["message_id"] for message in run.request["input_messages"]]
            for run in queued
        ] == [["selected-a"], ["ordinary"], ["selected-b"]]
        assert [run.request["skill_ids"] for run in queued] == [
            ["global/review"],
            [],
            ["global/security"],
        ]


@pytest.mark.asyncio
async def test_cancelled_predecessor_still_wakes_selected_skill_input(
    tmp_path: Path,
) -> None:
    """Cancelling one Run must not strand later accepted Session input."""

    async with _api(tmp_path) as (client, runtime):
        session_id = "cancel-before-skill"
        await runtime.sessions.get_or_create(session_id=session_id, agent_id="default")
        started = await client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={"message_id": "start", "content": "cancel"},
        )
        active_run_id = started.json()["data"]["run_id"]
        selected = await client.post(
            f"/api/v1/sessions/{session_id}/prompt",
            json={
                "message_id": "selected-after-cancel",
                "content": "review after cancellation",
                "skill_ids": ["global/review"],
            },
        )
        assert selected.status_code == 202
        cancelled = await client.post(f"/api/v1/sessions/{session_id}/cancel")
        assert cancelled.status_code == 200
        assert (await _wait_terminal(runtime, active_run_id)).status == "cancelled"

        for _ in range(200):
            runs = await runtime.runs.list(session_id=session_id)
            if len(runs) == 2 and runs[-1].status in {"completed", "failed"}:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("selected Skill input stayed idle after cancellation")
        assert runs[-1].request["skill_ids"] == ["global/review"]


@pytest.mark.asyncio
async def test_recovery_drains_every_temporary_skill_partition(tmp_path: Path) -> None:
    """Restart recovery must not stop after the first queued Skill scope."""

    async with _api(tmp_path) as (_client, runtime):
        session_id = "recover-skill-scopes"
        await runtime.sessions.get_or_create(session_id=session_id, agent_id="default")
        store = runtime.runs._store
        for message in (
            SessionInboxMessage(
                "recover-selected-a",
                "use review",
                skill_ids=("global/review",),
            ),
            SessionInboxMessage("recover-ordinary", "plain follow-up"),
            SessionInboxMessage(
                "recover-selected-b",
                "use security",
                skill_ids=("global/security",),
            ),
        ):
            assert await store.admit_input(session_id, message)

        recovered = SessionInputBroker(runtime)
        try:
            await recovered.recover()
            for _ in range(400):
                runs = await runtime.runs.list(session_id=session_id)
                if len(runs) == 3 and all(
                    run.status in {"completed", "failed", "cancelled"}
                    for run in runs
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("recovery stopped before all Skill scopes drained")
        finally:
            await recovered.close()

        assert [
            [message["message_id"] for message in run.request["input_messages"]]
            for run in runs
        ] == [
            ["recover-selected-a"],
            ["recover-ordinary"],
            ["recover-selected-b"],
        ]
