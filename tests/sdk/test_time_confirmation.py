from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from bianbu_agent.hooks.command import _patch
from bianbu_agent.plugins import HookPoint
from bianbu_agent.runs import time_confirmation
from bianbu_agent.runs.manager import _wait_for_resume


def ticket():
    return {"kind": "disable_ntp_and_set_time", "id": "backend-id", "token": "private-token",
            "datetime": "14:30:00", "description": "Disable NTP and set time; keep NTP disabled"}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject", "timeout", "cancel"])
async def test_confirmation_lifecycle(monkeypatch, action):
    events, calls = [], []
    control = SimpleNamespace(cancelled=asyncio.Event())
    pending, waiting = {}, set()
    decisions = asyncio.Queue()

    async def publish(event):
        events.append(event)
        if event.kind == "approval_required":
            assert pending["id"] == event.payload["approval_id"]
            if action == "cancel":
                control.cancelled.set()
            elif action != "timeout":
                await decisions.put((action, {}))

    async def complete(request, session_id, decision):
        calls.append((request, session_id, decision))
        return {"ok": True, "reply": "done"}

    async def wait(event, **kwargs):
        kwargs["timeout_seconds"] = 0.01
        return await _wait_for_resume(event, **kwargs)

    monkeypatch.setattr(time_confirmation, "_complete", complete)
    reply = await time_confirmation.resolve_time_confirmation(
        ticket(), handle=SimpleNamespace(publish=publish), control=control, pending=pending,
        decisions=decisions, waiting=waiting, session_id="session", run_id="run", wait_for_resume=wait,
    )
    assert reply == "done"
    assert calls == [(ticket(), "session", "approve" if action == "approve" else "reject")]
    assert not pending and not waiting
    assert [event.kind for event in events] == ["approval_required", "approval_resolved"]
    public = json.dumps([dict(event.payload) for event in events])
    assert "private-token" not in public and "backend-id" not in public
    assert events[0].payload["actions"][0]["allowed_decisions"] == ["approve", "reject"]


def test_hook_protocol_keeps_ticket_out_of_metadata_and_repr():
    patch = _patch(json.dumps({"direct_reply": "Confirm", "approval": ticket()}), HookPoint.BEFORE_RUN)
    assert patch.approval["token"] == "private-token"
    assert not patch.metadata and "private-token" not in repr(patch)
    with pytest.raises(ValueError):
        _patch(json.dumps({"direct_reply": "Confirm", "approval": ticket()}), HookPoint.BEFORE_TOOL)
    with pytest.raises(ValueError):
        _patch(json.dumps({"approval": ticket()}), HookPoint.BEFORE_RUN)


@pytest.mark.asyncio
async def test_completion_uses_fixed_endpoint_and_private_header(monkeypatch):
    client_type = httpx.AsyncClient

    def handler(request):
        assert request.url == "http://127.0.0.1:8765/v1/settings/time/confirmation"
        assert request.headers["X-Bibit-Confirmation-Token"] == "private-token"
        assert json.loads(request.content) == {"id": "backend-id", "session_id": "session", "decision": "approve"}
        return httpx.Response(200, json={"ok": True, "reply": "done"})

    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.setattr(time_confirmation.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))
    assert (await time_confirmation._complete(ticket(), "session", "approve"))["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["https://example.com", "http://127.0.0.1:8765/other", "http://user@127.0.0.1:8765"])
async def test_completion_refuses_remote_or_ambiguous_destination(monkeypatch, base):
    monkeypatch.setenv("BIBIT_INTENT_BASE_URL", base)
    with pytest.raises(ValueError):
        await time_confirmation._complete(ticket(), "session", "approve")


@pytest.mark.asyncio
async def test_completion_failure_never_retries_or_calls_model(monkeypatch):
    calls = []

    async def failed(*args):
        calls.append(args)
        raise TimeoutError("response lost")

    async def publish(event):
        pass

    async def wait(event, **kwargs):
        return "approve", {}

    monkeypatch.setattr(time_confirmation, "_complete", failed)
    reply = await time_confirmation.resolve_time_confirmation(
        ticket(), handle=SimpleNamespace(publish=publish), control=SimpleNamespace(cancelled=asyncio.Event()),
        pending={}, waiting=set(), decisions=asyncio.Queue(), run_id="run", session_id="session", wait_for_resume=wait,
    )
    assert len(calls) == 1 and "不会自动重试" in reply
