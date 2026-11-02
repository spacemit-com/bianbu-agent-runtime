from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from bianbu_agent.engines.langgraph.plugin_tools import build_plugin_tools
from bianbu_agent.engines.langgraph.engine import _cancellable_graph_stream
from bianbu_agent.plugins import ToolCallContext, ToolContribution, ToolResult, ToolSpec
from bianbu_agent.runs import time_confirmation
from bianbu_agent.runs.control import RunControl
from bianbu_agent.runs.events import RunEvent
from bianbu_agent.runs.manager import _resolve_tool_approval


def ticket():
    return {"kind": "disable_ntp_and_set_time", "id": "private-id", "token": "private-token",
            "datetime": "15:30:00", "description": "Disable NTP and set 15:30:00"}


def pending_result():
    return ToolResult(json.dumps({"ok": False, "confirmation": {
        "kind": "disable_ntp_and_set_time", "datetime": "15:30:00"}}), metadata={"mcp_error": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_cloud_tool_waits_for_real_decision(monkeypatch, decision):
    calls = []

    async def prepare(target, session_id, run_id):
        calls.append(("prepare", target, session_id, run_id))
        return ticket()

    async def complete(request, session_id, action):
        calls.append(("complete", request, session_id, action))
        return {"ok": True, "reply": "done" if action == "approve" else "cancelled"}

    monkeypatch.setattr(time_confirmation, "_prepare_cloud", prepare)
    monkeypatch.setattr(time_confirmation, "_complete", complete)
    control = RunControl(request_id="run", session_id="session")
    context = ToolCallContext(run_id="run", session_id="session", agent_id="agent")
    task = asyncio.create_task(time_confirmation.resolve_cloud_time_confirmation(
        pending_result(), target="15:30", context=context, control=control))
    event = await asyncio.wait_for(control.wait_approval_request(), 1)
    assert not task.done() and len(calls) == 1
    assert "private-token" not in json.dumps(event) and "private-id" not in json.dumps(event)
    assert event["actions"][0]["allowed_decisions"] == ["approve", "reject"]
    assert control.resolve_approval([{"type": decision}])
    result = await task
    assert calls[-1][-1] == decision
    assert json.loads(result.content)["message"] == ("done" if decision == "approve" else "cancelled")
    assert "confirmation" not in result.content
    assert not result.metadata["mcp_error"]


@pytest.mark.asyncio
async def test_cloud_cancel_rejects_ticket(monkeypatch):
    calls = []

    async def prepare(*args):
        return ticket()

    async def complete(request, session_id, decision):
        calls.append(decision)
        return {"ok": True, "reply": "cancelled"}

    monkeypatch.setattr(time_confirmation, "_prepare_cloud", prepare)
    monkeypatch.setattr(time_confirmation, "_complete", complete)
    control = RunControl(request_id="run", session_id="session")
    task = asyncio.create_task(time_confirmation.resolve_cloud_time_confirmation(
        pending_result(), target="15:30", context=SimpleNamespace(session_id="session", run_id="run"), control=control))
    await asyncio.wait_for(control.wait_approval_request(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["reject"]
    assert not control.approval_pending()


@pytest.mark.asyncio
async def test_cloud_prepare_fixed_local_endpoint(monkeypatch):
    client_type = httpx.AsyncClient

    def handle(request):
        assert request.url == "http://127.0.0.1:8765/v1/settings/time/prepare"
        assert json.loads(request.content) == {"datetime": "15:30", "session_id": "session", "run_id": "run"}
        return httpx.Response(200, json=ticket())

    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setattr(time_confirmation.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(handle), **kwargs))
    assert await time_confirmation._prepare_cloud("15:30", "session", "run") == ticket()
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", "https://example.com")
    with pytest.raises(ValueError):
        await time_confirmation._prepare_cloud("15:30", "session", "run")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["bianbu.bibit.intent", "other.plugin"])
async def test_adapter_only_resumes_trusted_time_tool(monkeypatch, owner):
    calls = []

    async def handler(context, arguments):
        return pending_result()

    async def resume(result, **kwargs):
        calls.append(kwargs)
        return ToolResult('{"ok":true,"message":"done"}')

    monkeypatch.setattr(time_confirmation, "resolve_cloud_time_confirmation", resume)
    tool = build_plugin_tools([ToolContribution(
        "mcp.bibit-control", ToolSpec("set_system_time", "Set time", {
            "type": "object", "properties": {"datetime": {"type": "string"}}, "required": ["datetime"]},
            metadata={"qualified_id": "bianbu.bibit.intent/bibit-control/set_system_time", "owner_plugin_id": owner}),
        handler)])[0]
    result = await tool.ainvoke({"datetime": "15:30"}, config={"configurable": {
        "run_id": "run", "session_id": "session", "agent_id": "agent", "account_id": "account"}})
    assert len(calls) == (1 if owner == "bianbu.bibit.intent" else 0)
    assert json.loads(result)["ok"] == (owner == "bianbu.bibit.intent")


@pytest.mark.asyncio
async def test_cloud_timeout_resolves_as_rejection(monkeypatch):
    captured = []

    async def wait(event, **kwargs):
        captured.append(kwargs["timeout_seconds"])
        return "timeout", {}

    async def publish(event):
        pass

    monkeypatch.setattr("bianbu_agent.runs.manager._wait_for_resume", wait)
    control = RunControl(request_id="run", session_id="session")
    task = asyncio.create_task(control.request_approval({"approval_id": "approval"}))
    await control.wait_approval_request()
    await _resolve_tool_approval(RunEvent("approval_required", {
        "kind": "disable_ntp_and_set_time", "actions": [{"name": "disable_ntp_and_set_time"}]}),
        decisions=asyncio.Queue(), pending={}, control=control, waiting=set(),
        handle=SimpleNamespace(publish=publish), run_id="run", session_id="session")
    assert captured == [150.0]
    assert await task == [{"type": "reject"}]


@pytest.mark.asyncio
async def test_failed_completion_is_not_retried(monkeypatch):
    calls = []

    async def prepare(*args):
        return ticket()

    async def complete(request, session_id, decision):
        calls.append(decision)
        raise TimeoutError()

    monkeypatch.setattr(time_confirmation, "_prepare_cloud", prepare)
    monkeypatch.setattr(time_confirmation, "_complete", complete)
    control = RunControl(request_id="run", session_id="session")
    task = asyncio.create_task(time_confirmation.resolve_cloud_time_confirmation(
        pending_result(), target="15:30", context=SimpleNamespace(session_id="session", run_id="run"), control=control))
    await control.wait_approval_request()
    control.resolve_approval([{"type": "approve"}])
    result = await task
    assert calls == ["approve"]
    assert result.metadata["mcp_error"]
    assert "不会自动重试" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_cloud_tool_graph_stream_and_run_manager_resume(monkeypatch, decision):
    calls = []

    async def prepare(*args):
        return ticket()

    async def complete(request, session_id, action):
        calls.append(action)
        return {"ok": True, "reply": action}

    async def handler(context, arguments):
        return pending_result()

    async def publish(event):
        assert event.kind == "approval_resolved"

    monkeypatch.setattr(time_confirmation, "_prepare_cloud", prepare)
    monkeypatch.setattr(time_confirmation, "_complete", complete)
    control = RunControl(request_id="run", session_id="session")
    tool = build_plugin_tools([ToolContribution(
        "mcp.bibit-control", ToolSpec("set_system_time", "Set time", {
            "type": "object", "properties": {"datetime": {"type": "string"}}, "required": ["datetime"]},
            metadata={"qualified_id": "bianbu.bibit.intent/bibit-control/set_system_time",
                      "owner_plugin_id": "bianbu.bibit.intent"}), handler)])[0]

    async def graph():
        yield await tool.ainvoke({"datetime": "15:30"}, config={"configurable": {
            "run_id": "run", "session_id": "session", "agent_id": "agent", "account_id": "account",
            "run_control": control}})

    stream = _cancellable_graph_stream(graph(), control)
    event = await asyncio.wait_for(anext(stream), 1)
    assert event.kind == "approval_required"
    assert not calls
    decisions = asyncio.Queue()
    decisions.put_nowait((decision, {}))
    await _resolve_tool_approval(RunEvent(event.kind, event.payload), decisions=decisions,
        pending={}, control=control, waiting=set(), handle=SimpleNamespace(publish=publish),
        run_id="run", session_id="session")
    result = await asyncio.wait_for(anext(stream), 1)
    assert json.loads(result)["message"] == decision
    assert calls == [decision]
    await stream.aclose()


@pytest.mark.asyncio
async def test_missing_control_never_prepares_or_applies(monkeypatch):
    async def forbidden(*args):
        pytest.fail("A missing RunControl must not create or resolve tickets")

    monkeypatch.setattr(time_confirmation, "_prepare_cloud", forbidden)
    monkeypatch.setattr(time_confirmation, "_complete", forbidden)
    result = await time_confirmation.resolve_cloud_time_confirmation(
        pending_result(), target="15:30", context=SimpleNamespace(session_id="session", run_id="run"), control=None)
    assert result.metadata["mcp_error"]


@pytest.mark.asyncio
async def test_changed_target_rejects_without_approval(monkeypatch):
    calls = []

    async def prepare(*args):
        return {**ticket(), "datetime": "16:30:00"}

    async def complete(request, session_id, decision):
        calls.append(decision)
        return {"ok": True, "reply": "cancelled"}

    monkeypatch.setattr(time_confirmation, "_prepare_cloud", prepare)
    monkeypatch.setattr(time_confirmation, "_complete", complete)
    control = RunControl(request_id="run", session_id="session")
    result = await time_confirmation.resolve_cloud_time_confirmation(
        pending_result(), target="15:30", context=SimpleNamespace(session_id="session", run_id="run"), control=control)
    assert calls == ["reject"] and result.metadata["mcp_error"]
    assert not control.approval_pending()
